#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Telegram 网页版（web.telegram.org/k/）签到引擎。

不需要 api_id/api_hash：Playwright 驱动真实 Chromium，扫码登录一次，
会话保存在 data/web_profiles/<账号名>/，之后全自动签到。

用法：
    python web_engine.py login 账号名    # 打开浏览器扫码登录（账号名同 config.yaml）
    python web_engine.py test 账号名     # 打开浏览器手动检查会话是否有效
"""

import asyncio
import json
import logging
import socket
import sys
import time
import urllib.parse
import urllib.request
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from playwright.async_api import TimeoutError as PlaywrightTimeoutError, async_playwright
from core.config import safe_name
from core.logic import button_message_ref
from core.locks import FileLock
from core.storage import atomic_write
from core.paths import BASE_DIR, account_lock_path, configure_browser_path, visible_browser_available
from core.timeutil import timezone_name

log = logging.getLogger("checkin.web")

PROFILES_DIR = BASE_DIR / "data" / "web_profiles"
configure_browser_path()
BASE_URL = "https://web.telegram.org/k/"

# K 版前端的 class 名会随版本变化，每类元素给多个候选，按序尝试
# （2026-09-27 用已登录会话实测过：sidebar/chatlist-chat/peer-title/bubble/btn-send 均正确）
SEL_COMPOSER = [
    "div[contenteditable='true'].input-message-input",   # 真输入框（页面还有一个 fake 元素要避开）
    "div[contenteditable='true'][data-peer-id]",
]
SEL_SEND = [".btn-send", "button.send"]
SEL_FILE_INPUT = ["input[type='file']", ".input-file"]
SEL_BTN_ROW = [".reply-markup-button", ".btn-reply-markup", ".reply-markup .btn-item"]
SEL_CHAT_LIST = [".sidebar", ".chatlist-container", ".dialog-list"]
SEL_AUTH = [".auth", ".login-page", "#auth"]
SEL_SEARCH = ["input.input-search-input", ".sidebar-search-input",
              "input[placeholder*='搜索']", "input[placeholder*='Search']"]

COMMON_PROXY_PORTS = [
    ("http", 7890),    # Clash 混合端口
    ("http", 7897),    # Clash Verge 默认
    ("http", 10809),   # v2rayN HTTP
    ("socks5", 10808),  # v2rayN SOCKS
    ("socks5", 1080),   # 通用 SOCKS
]


def _build_proxy_dict(raw):
    value = str(raw or "").strip()
    if not value:
        return None
    if value.startswith("//"):
        value = "http:" + value
    if "://" not in value:
        value = "http://" + value
    parsed = urllib.parse.urlsplit(value)
    if parsed.username is not None:
        host = parsed.hostname or ""
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        netloc = f"{host}:{parsed.port}" if parsed.port else host
        server = urllib.parse.urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, ""))
        result = {"server": server, "username": urllib.parse.unquote(parsed.username)}
        if parsed.password is not None:
            result["password"] = urllib.parse.unquote(parsed.password)
        return result
    return {"server": value}


def resolve_proxy(cfg):
    """网页引擎的代理：配置项 web_proxy 优先，其次系统代理，再探测常见本地端口。"""
    explicit = str(cfg.get("web_proxy") or "").strip()
    if explicit.lower() in ("direct", "none"):
        return None
    if explicit:
        return _build_proxy_dict(explicit)
    proxies = urllib.request.getproxies()
    sys_proxy = proxies.get("https") or proxies.get("http") or proxies.get("socks")
    if sys_proxy:
        if not proxies.get("https") and not proxies.get("http") and "://" not in sys_proxy:
            sys_proxy = "socks5://" + sys_proxy
        return _build_proxy_dict(sys_proxy)
    for scheme, port in COMMON_PROXY_PORTS:
        host = "127.0.0.1"
        if cfg.get("web_proxy_host"):
            host = str(cfg.get("web_proxy_host"))
        try:
            with socket.create_connection((host, port), timeout=0.4):
                return {"server": f"{scheme}://{host}:{port}"}
        except OSError:
            continue
    return None


class TopicSelectionError(RuntimeError):
    """An ambiguous destination must stop navigation instead of trying other routes."""


class WebEngine:
    def __init__(self, acc, cfg):
        self.name = acc["name"]
        self.cfg = cfg
        self.profile = PROFILES_DIR / safe_name(self.name)
        self.headless = bool(cfg.get("web_headless", False)) or not visible_browser_available()
        self.send_started_at = None
        self.send_started_monotonic = None
        self.before_send = None
        self._media_preview = None
        self._media_upload_started = False
        self._media_dispatch_attempted = False
        self.pw = None
        self.browser = None
        self.page = None
        # 本会话内「我发出去的消息」的 data-mid 登记表：bot 气泡上的
        # data-reply-to-mid 与其中之一相等 = 这条回复明确是回给我的，
        # 群里别人发同样签到词得到的结果不再混进判定池
        self.last_sent_mids = set()
        self._mids_chat = None
        self._expected_peer = None
        self._expected_topic = None
        self._expected_topic_id = None
        self._configured_topic_id = None
        self._topic_ids = {}
        self.last_topic_selection = None
        self.last_draft_warning = None
        self._automation_text = None
        self._capture_by_chat = {}
        self._sent_by_chat = {}
        self._resolved_peers = {}
        self.last_replied = []
        self.last_evidence = []
        self._active_mids = None
        self._profile_lock = None
        self._login_verified = False
        self._self_peer = None
        self._last_send_confirmed_by_reply = False

    # ---------- 基础 ----------

    async def launch(self):
        self._profile_lock = FileLock(account_lock_path(PROFILES_DIR.parent.parent, safe_name(self.name)))
        self._profile_lock.acquire()
        try:
            self.profile.mkdir(parents=True, exist_ok=True)
            try:
                cache = json.loads((self.profile / 'checkin-topics.json').read_text(encoding='utf-8'))
                if isinstance(cache, dict) and all(isinstance(k, str) and isinstance(v, dict)
                    and all(isinstance(t, str) and isinstance(i, str) and i.isdigit() and int(i)>0 for t, i in v.items()) for k, v in cache.items()):
                    self._topic_ids = cache
            except (OSError, ValueError):
                pass  # A disposable navigation hint; never an authority for sending.
            proxy = await asyncio.to_thread(resolve_proxy, self.cfg)
            self.pw = await async_playwright().start()
            options = {"headless": self.headless, "proxy": proxy,
                       "viewport": {"width": 1280, "height": 800},
                       "args": ["--disable-blink-features=AutomationControlled"]}
            if self.cfg.get("web_browser_channel"):
                options["channel"] = self.cfg["web_browser_channel"]
            self.browser = await self.pw.chromium.launch_persistent_context(str(self.profile), **options)
            self.page = self.browser.pages[0] if self.browser.pages else await self.browser.new_page()
            log.info('[%s] 浏览器已启动：%s', self.name, '后台运行' if self.headless else '显示签到过程')
            if not self.headless:
                await self.page.bring_to_front()
            return self
        except BaseException:
            await self.close()
            raise

    async def close(self):
        async def _teardown():
            try:
                if self.browser:
                    browser, self.browser = self.browser, None
                    try:
                        await asyncio.wait_for(browser.close(), timeout=15)
                    except Exception:
                        pass
            finally:
                try:
                    if self.pw:
                        pw, self.pw = self.pw, None
                        try:
                            await asyncio.wait_for(pw.stop(), timeout=10)
                        except Exception:
                            pass
                finally:
                    self.page = None
                    if self._profile_lock:
                        self._profile_lock.release()
                        self._profile_lock = None
        task = asyncio.create_task(_teardown())
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise

    def marker(self):
        return self.profile.parent / (self.profile.name + ".loggedin")

    async def _first(self, selectors, timeout=15000):
        """All candidates share one deadline; cached hidden nodes never mask visible ones."""
        deadline = time.monotonic() + timeout / 1000
        while time.monotonic() < deadline:
            for selector in selectors:
                matches = self.page.locator(selector)
                for index in range(min(await matches.count(), 20)):
                    match = matches.nth(index)
                    if await match.is_visible():
                        return match
            await asyncio.sleep(min(.1, max(0, deadline-time.monotonic())))
        raise RuntimeError(f"页面元素没找到（{selectors[0]} 等）")

    # ---------- 登录 ----------

    async def _looks_logged_in_now(self):
        """登录判定：localStorage 里有 user_auth 或左侧会话栏可见。"""
        for selector in SEL_AUTH:
            if await self._visible([selector]):
                return False
        for sel in SEL_CHAT_LIST:
            try:
                if await self.page.locator(sel).first.is_visible():
                    return True
            except Exception:
                continue
        return False

    async def is_logged_in(self):
        for attempt in range(3):
            try:
                await self.page.goto(BASE_URL, wait_until="domcontentloaded", timeout=15000)
                break
            except Exception as error:
                transient = any(code in str(error) for code in (
                    'ERR_CONNECTION_CLOSED', 'ERR_CONNECTION_RESET', 'ERR_TIMED_OUT',
                    'ERR_NETWORK_CHANGED', 'Timeout'))
                if not transient or attempt == 2:
                    raise RuntimeError(f"打不开 web.telegram.org：{error}\n"
                                       "请检查网络及已配置的代理；浏览器尚未进入会话，本次未发送。") from error
                log.warning('[%s] Telegram 连接暂时中断，重试打开网页（%d/2）', self.name, attempt + 1)
                await asyncio.sleep(.5 * (attempt + 1))
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            if await self._looks_logged_in_now():
                return True
            await asyncio.sleep(.25)
        self.marker().unlink(missing_ok=True)
        return False

    async def login_interactive(self):
        """打开可见浏览器等用户扫码，登录成功自动检测并保存标记（无需按回车）。"""
        if not visible_browser_available():
            raise RuntimeError('当前服务器没有可视桌面，请通过桌面或 X11 转发完成首次扫码登录')
        self.headless = False
        await self.launch()
        try:
            if await self.is_logged_in():
                print("✅ 该账号已是登录状态，无需扫码")
                self.marker().write_text("ok", encoding="utf-8")
                return True
            print("请在打开的浏览器窗口中扫码登录（Settings → Devices → Link Desktop Device）")
            print("登录成功后这里会自动检测到，无需任何按键（最长等 5 分钟）……")
            deadline = time.monotonic() + 300
            while time.monotonic() < deadline:
                await asyncio.sleep(4)
                try:
                    if await self._looks_logged_in_now():
                        self.marker().write_text("ok", encoding="utf-8")
                        print("✅ 登录成功，会话已保存，浏览器窗口将自动关闭")
                        return True
                except Exception:
                    pass  # 页面跳转瞬间可能读不到，下一轮再查
            print("⚠ 等待超时。如果你其实已经扫码成功，重新运行本命令即可自动确认")
            return False
        finally:
            await self.close()

    async def ensure_login(self):
        if self._login_verified and await self._looks_logged_in_now():
            return True
        if not await self.is_logged_in():
            self.marker().unlink(missing_ok=True)
            raise RuntimeError("网页版未登录（运行 python web_engine.py login 账号名 扫码）")
        self._login_verified = True
        return True

    # ---------- 签到动作 ----------

    def chat_url(self, chat_ref):
        # K 版 peer id 原样用作 URL hash（实测 href 形如 #-3830659441 / #@user）
        ref = str(chat_ref).strip().lstrip("#")
        return BASE_URL + "#" + ref

    _CHAT_VIEW_JS = r"""(options = {}) => {
            options ||= {};
            const visible = el => el && el.checkVisibility() && !el.closest('.bubbles, .chatlist-container, ul.chatlist, .sidebar-right, .topics-slider');
            const titles = [...document.querySelectorAll('.chat-info .peer-title, .topbar .peer-title')].filter(visible);
            const title = titles.find(el => el.hasAttribute('data-thread-id')) || titles.find(el => el.getAttribute('data-peer-id')) || titles[0];
            const inputs = [...document.querySelectorAll('[contenteditable="true"][data-peer-id]')].filter(visible);
            const composer = inputs[0];
            const pid = title?.getAttribute('data-peer-id') || composer?.getAttribute('data-peer-id');
            const inputPid = composer && composer.getAttribute('data-peer-id');
            const thread = title?.getAttribute('data-thread-id');
            const inputThread = composer?.getAttribute('data-thread-id');
            const normThread = v => (v && v !== '0') ? v : null;
            const info = !title ? {} : {pid: inputPid && inputPid !== pid ? null : pid,
                title: title.textContent.trim(), username: title.getAttribute('data-username') || '',
                composer_peer: inputPid, topic_id: normThread(thread), composer_topic_id: normThread(inputThread)};
            if (normThread(inputThread) && normThread(thread) !== normThread(inputThread)) info.pid = null;
            if (!options.peer) return info;
            const rows = [];
            for (const pane of document.querySelectorAll('.topics-slider .topics-container')) {
                if (!pane.checkVisibility()) continue;
                const header = pane.querySelector('.sidebar-header__title .peer-title[data-peer-id], .sidebar-header .peer-title[data-peer-id]');
                if (header?.getAttribute('data-peer-id') !== options.peer) continue;
                for (const row of pane.querySelectorAll('a[data-peer-id][data-thread-id]')) {
                    if (!row.checkVisibility() || row.getAttribute('data-peer-id') !== options.peer) continue;
                    if (row.getAttribute('data-thread-id') === '0') continue;
                    const name = row.querySelector('.row-title .peer-title')?.textContent.trim();
                    if (name === options.title) rows.push(row);
                }
            }
            const matches = [...new Set(rows.map(row => row.getAttribute('data-thread-id')))];
            return {info, matches};
    }"""

    async def opened_chat_info(self):
        """Visible peer, composer and topic ID; cached headers never establish identity."""
        return await self.page.evaluate(self._CHAT_VIEW_JS)

    async def _opened_peer(self):
        return (await self.opened_chat_info()).get("pid")

    async def _wait_opened(self, ref, timeout=6):
        ref = str(ref).strip().lstrip("#")
        expected = ref if ref.lstrip("-").isdigit() else self._resolved_peers.get(ref.lower())
        if not expected:
            return False
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if str(await self._opened_peer()) == str(expected):
                return True
            await asyncio.sleep(min(.15, max(0, deadline - time.monotonic())))
        return False

    async def assert_destination(self):
        info = await self.opened_chat_info()
        actual = info.get('pid')
        self._last_actual_peer = actual
        if not self._expected_peer or str(actual) != str(self._expected_peer):
            raise RuntimeError(f"目标会话核对失败：配置 {self._expected_peer or self._mids_chat}，当前 {actual or '无'}；未发送")
        if self._expected_topic:
            actual_topic = info.get('title', '')
            if actual_topic.strip() != self._expected_topic.strip():
                raise RuntimeError(f"话题核对失败：配置「{self._expected_topic}」，当前「{actual_topic}」；未发送")
            if not self._expected_topic_id or info.get('topic_id') != self._expected_topic_id or info.get('composer_peer') != str(self._expected_peer):
                raise RuntimeError(f"话题 ID 核对失败：配置 {self._expected_topic_id or '未解析'}，当前 {info.get('topic_id') or '无'}；未发送")
        return actual

    _SCROLL_FIND_JS = r"""(ref) => {
        const refN = ref.toLowerCase();
        const uls = [...document.querySelectorAll('ul.chatlist')];
        let target = null, bestH = 0;
        for (const ul of uls) {
            const tab = ul.closest('.tabs-tab');
            const active = !tab || tab.className.includes('active');
            const r = ul.getBoundingClientRect();
            if (ul.checkVisibility() && !ul.closest('.topics-slider') && r.height > 0 && active && r.height > bestH) { target = ul; bestH = r.height; }
        }
        if (!target) return {found: false, moved: false};
        for (const it of target.querySelectorAll('.chatlist-chat')) {
            const pid = (it.getAttribute('data-peer-id') || '').toLowerCase();
            const href = (it.getAttribute('href') || '').replace(/^#/, '').toLowerCase();
            if (refN === pid || refN === href) {
                it.scrollIntoView({block: 'center'});
                return {found: true, moved: false};
            }
        }
        let sc = target;
        while (sc && !(sc.classList && sc.classList.contains('scrollable-y')))
            sc = sc.parentElement;
        if (sc) {
            const before = sc.scrollTop;
            sc.scrollTop = Math.min(sc.scrollTop + sc.clientHeight * 0.8, sc.scrollHeight);
            return {found: false, moved: sc.scrollTop !== before};
        }
        return {found: false, moved: false};
    }"""

    _SCROLL_TOP_JS = r"""() => {
        const uls = [...document.querySelectorAll('ul.chatlist')];
        for (const ul of uls) {
            const r = ul.getBoundingClientRect();
            if (r.height > 0) {
                let sc = ul;
                while (sc && !(sc.classList && sc.classList.contains('scrollable-y')))
                    sc = sc.parentElement;
                if (sc) sc.scrollTop = 0;
            }
        }
    }"""

    async def _scroll_find(self, ref, deadline):
        """在当前可见面板内滚动查找目标会话（含一次回顶重扫）。

        找到后滚到可见处并返回元素句柄；点击由调用方执行（避免双击）。
        """
        wrapped = False
        rounds = 0
        while time.monotonic() < deadline:
            rounds += 1
            res = await self.page.evaluate(self._SCROLL_FIND_JS, str(ref).strip().lstrip("#"))
            log.debug("scroll_find %s round %d: found=%s moved=%s",
                      ref, rounds, res.get("found"), res.get("moved"))
            if res.get("found"):
                r = str(ref).strip().lstrip("#")
                candidates = self.page.locator(
                    f'.chatlist-chat[data-peer-id="{r}"], '
                    f'.chatlist-chat[href="#{r}"]')
                for index in range(await candidates.count()):
                    item = candidates.nth(index)
                    if await item.is_visible() and not await item.evaluate("el=>!!el.closest('.topics-slider')"):
                        return item
                return None
            if not res.get("moved"):
                if not wrapped:
                    wrapped = True
                    log.debug("scroll_find %s: 到底，回顶重扫", ref)
                    try:
                        await self.page.evaluate(self._SCROLL_TOP_JS)
                        await asyncio.sleep(0.5)
                        continue
                    except Exception:
                        return None
                log.debug("scroll_find %s: 二次到底仍未找到", ref)
                return None
            await asyncio.sleep(0.35)
        log.debug("scroll_find %s: 超时（%d 轮）", ref, rounds)
        return None

    async def _list_folders_raw(self):
        try:
            return await self.page.evaluate("""() => {
                const bar = document.querySelector('.folders-tabs-scrollable');
                if (!bar) return [];
                const names = [], seen = [];
                for (const el of bar.querySelectorAll('*')) {
                    let direct = '';
                    for (const n of el.childNodes)
                        if (n.nodeType === 3) direct += n.textContent;
                    direct = direct.trim();
                    if (!direct || direct.length > 16) continue;
                    const name = direct.replace(/\\s*\\d+$/, '').trim();
                    if (!name || seen.includes(name)) continue;
                    seen.push(name);
                    names.push(name);
                }
                return names;
            }""")
        except Exception:
            return []





    async def _open_via_folder(self, chat_ref, folder_name, topic=None):
        """One exact folder, one group row, one topic row; wait only for actual readiness."""
        ref = str(chat_ref).strip().lstrip('#')
        try:
            folder = await self.page.evaluate_handle(r"""name => {
                const items = [...document.querySelectorAll('.menu-horizontal-div-item')].filter(e=>e.checkVisibility());
                const label = el => {
                    const clone = el.cloneNode(true);
                    clone.querySelectorAll('.badge, .badge-unread, .unread-count').forEach(e=>e.remove());
                    return clone.textContent.trim();
                };
                const target = items.find(e=>label(e)===name) || items.find(e=>e.textContent.trim().replace(/\s*\d+$/, '')===name);
                return target || null;
            }""", str(folder_name))
            tab = folder.as_element()
            if tab is None:
                await folder.dispose()
                return False
            try:
                if 'active' not in (await tab.get_attribute('class') or '').split():
                    await tab.click(timeout=2000)
            finally:
                await folder.dispose()
            # Exclude topic rows and hidden virtual-list copies of the same peer.
            pick = r"""options => {
                const rows = [...document.querySelectorAll('.chatlist-chat[data-peer-id]')];
                const row = rows.find(e=>e.getAttribute('data-peer-id')===options.peer && e.checkVisibility() && !e.closest('.topics-slider') &&
                    (!e.closest('.tabs-tab') || e.closest('.tabs-tab').classList.contains('active')));
                if (!row) return false;
                return true;
            }"""
            handle = await self.page.wait_for_function(pick, arg={'peer':ref}, timeout=3500, polling='raf')
            await handle.dispose()
            if topic:
                state = await self.page.evaluate(self._CHAT_VIEW_JS, {'peer':ref,'title':str(topic).strip()})
                if state['matches']:
                    return await self._enter_topic(str(topic), timeout=5)
            row = self.page.locator(f'.chatlist-chat[data-peer-id="{ref}"]:not(.topics-slider *):visible').first
            await row.click(timeout=2500)
            if topic:
                return await self._enter_topic(str(topic), timeout=5)
            return await self._wait_opened(ref, timeout=3)
        except TopicSelectionError:
            raise
        except Exception as error:
            log.debug('文件夹直达未完成：%s', error)
            return False

    async def _close_topic_panel(self):
        """Close only the frontmost forum drawer, ignoring cached offscreen panels."""
        target_js = r"""() => [...document.querySelectorAll('.topics-slider .sidebar-close-button')].find(b=>{
            const r=b.getBoundingClientRect(),x=r.left+r.width/2,y=r.top+r.height/2;
            if(!b.checkVisibility()||!r.width||!r.height||x<0||y<0||x>=innerWidth||y>=innerHeight)return false;
            const top=document.elementFromPoint(x,y);return top===b||b.contains(top);
        }) || null"""
        for _ in range(3):
            handle=await self.page.evaluate_handle(target_js)
            button=handle.as_element()
            if button is None:
                await handle.dispose()
                return
            try:
                await button.click(timeout=1200)
            except Exception:
                pass  # The drawer may have removed itself during the click.
            finally:
                await handle.dispose()
            await asyncio.sleep(.15)
        handle=await self.page.evaluate_handle(target_js)
        try:
            if handle.as_element() is not None:
                raise RuntimeError('话题面板仍在切换，未能可靠关闭；未发送')
        finally:
            await handle.dispose()

    async def _open_by_search(self, name, want_num=None):
        """Search by title, but select only a visible result with the exact peer ID."""
        if want_num is None:
            return False
        await self._close_topic_panel()
        ref = str(want_num)
        bare = str(name).replace('【', '').replace('】', '').strip()
        queries = list(dict.fromkeys([bare, bare[-6:]]))
        for query in queries:
            try:
                box = await self._first(SEL_SEARCH, timeout=1500)
                await box.click(timeout=1500)
                await box.fill('',timeout=1500)
                await box.type(query, delay=30,timeout=2000)
                await box.press('Enter',timeout=1500)
                deadline = time.monotonic() + 4
                while time.monotonic() < deadline:
                    candidates = self.page.locator('.search-super .row, .search-super a, .chatlist-chat')
                    for index in range(await candidates.count()):
                        row = candidates.nth(index)
                        if not await row.is_visible():
                            continue
                        if await row.get_attribute('data-peer-id') != ref and await row.get_attribute('href') != '#' + ref:
                            continue
                        await row.click(timeout=1500)
                        if await self._wait_opened(ref, timeout=2):
                            return True
                    await asyncio.sleep(.2)
            except Exception as error:
                log.debug('搜索定位 %s 失败：%s', ref, error)
        return False

    def _remember_topic(self, peer, topic, thread):
        previous = self._topic_ids.setdefault(peer, {}).get(topic)
        self._topic_ids[peer][topic] = thread
        self._expected_topic_id = thread
        if previous != thread and self.browser is not None:
            try:
                atomic_write(self.profile / 'checkin-topics.json', json.dumps(self._topic_ids, ensure_ascii=False))
            except OSError as error:
                log.debug('话题导航缓存未保存：%s', error)

    async def _enter_topic(self, topic, timeout=5):
        """Resolve within the exact forum, click once by thread ID, verify live chrome."""
        peer, topic = str(self._expected_peer), str(topic).strip()
        deadline, started = time.monotonic() + timeout, time.monotonic()
        configured = self._configured_topic_id
        cached = configured or self._topic_ids.get(peer, {}).get(topic)
        options = {'peer':peer,'title':topic,'id':cached}
        state_js = """options => {
            const state = (""" + self._CHAT_VIEW_JS + """)(options);
            const info = state.info;
            const opened = info.pid===options.peer && info.composer_peer===options.peer && info.title===options.title &&
                info.topic_id && (!options.id || info.topic_id===options.id);
            const pane = [...document.querySelectorAll('.topics-slider .topics-container')].find(p => p.checkVisibility() &&
                p.querySelector('.sidebar-header__title .peer-title[data-peer-id], .sidebar-header .peer-title[data-peer-id]')?.getAttribute('data-peer-id') === options.peer);
            return opened || state.matches.length || pane ? {...state, has_pane: !!pane} : false;
        }"""
        opened_js = """options => {
            const info = (""" + self._CHAT_VIEW_JS + """)();
            return info.pid===options.peer && info.composer_peer===options.peer && info.title===options.title &&
                info.topic_id===options.id ? info : false;
        }"""
        scroll_topic_js = r"""({peer, title, configured, reset=false}) => {
            const pane = [...document.querySelectorAll('.topics-slider .topics-container')].find(p => p.checkVisibility() &&
                p.querySelector('.sidebar-header__title .peer-title[data-peer-id], .sidebar-header .peer-title[data-peer-id]')?.getAttribute('data-peer-id') === peer);
            if (!pane) return null;
            const sc = pane.querySelector('.scrollable-y');
            if (sc && reset) sc.scrollTop = 0;
            const matches = [];
            for (const row of pane.querySelectorAll('a[data-peer-id][data-thread-id]')) {
                if (!row.checkVisibility() || row.getAttribute('data-peer-id') !== peer) continue;
                const tid = row.getAttribute('data-thread-id');
                if (!tid || tid === '0') continue;
                const name = row.querySelector('.row-title .peer-title')?.textContent.trim();
                if (name === title) matches.push(tid);
            }
            let moved = false;
            if (sc && !(configured && matches.includes(configured))) {
                const before = sc.scrollTop;
                sc.scrollTop += Math.max(120, Math.floor(sc.clientHeight * 0.8));
                moved = sc.scrollTop > before;
            }
            return {matches: [...new Set(matches)], moved};
        }"""
        try:
            handle = await self.page.wait_for_function(state_js, arg=options, timeout=max(1,int(timeout*1000)), polling='raf')
            state = await handle.json_value()
            await handle.dispose()
            info = state['info']
            if (info.get('pid') == peer and info.get('composer_peer') == peer and info.get('title') == topic
                    and info.get('topic_id') and (not cached or info['topic_id'] == cached)):
                self._remember_topic(peer, topic, info['topic_id'])
                via = 'already_open'
            else:
                all_matches = list(dict.fromkeys(state.get('matches', [])))
                if state.get('has_pane') and (not all_matches or (configured and configured not in all_matches)):
                    steps = 0
                    while time.monotonic() < deadline and steps < 40:
                        scan = await self.page.evaluate(scroll_topic_js, {'peer': peer, 'title': topic, 'configured': configured, 'reset': steps == 0})
                        if not scan:
                            break
                        for tid in scan['matches']:
                            if tid not in all_matches:
                                all_matches.append(tid)
                        if configured and configured in all_matches:
                            break
                        if not scan['moved']:
                            break
                        steps += 1
                        await asyncio.sleep(0.08)
                if configured:
                    all_matches = [thread for thread in all_matches if thread == configured]
                    if not all_matches and info.get('topic_id') != configured:
                        raise TopicSelectionError('所选话题 ID 已不可用或名称已改变，请在规则中重新选择话题；未发送')
                if len(all_matches) > 1:
                    raise TopicSelectionError(f'目标群内存在多个同名话题「{topic}」，已停止选择以避免误发')
                if not all_matches:
                    return False
                thread = all_matches[0]
                # Re-resolve the same identity if Telegram re-renders its virtual list.
                row = self.page.locator(f'.topics-slider .topics-container a[data-peer-id="{peer}"][data-thread-id="{thread}"]:visible').first
                if not await row.count():
                    await self.page.evaluate(scroll_topic_js, {'peer': peer, 'title': topic, 'configured': thread, 'reset': True})
                    steps = 0
                    while time.monotonic() < deadline and steps < 40 and not await row.count():
                        scan = await self.page.evaluate(scroll_topic_js, {'peer': peer, 'title': topic, 'configured': thread, 'reset': False})
                        if not scan or not scan['moved']:
                            break
                        steps += 1
                        await asyncio.sleep(0.08)
                await row.click(timeout=max(1,int((deadline-time.monotonic())*1000)))
                remaining = max(1,int((deadline-time.monotonic())*1000))
                handle = await self.page.wait_for_function(opened_js, arg={**options,'id':thread}, timeout=remaining, polling='raf')
                await handle.dispose()
                self._remember_topic(peer, topic, thread)
                via = 'exact_thread'
            self.last_topic_selection = {'peer':peer,'topic':topic,'topic_id':self._expected_topic_id,
                                         'via':via,'seconds':round(time.monotonic()-started,3)}
            log.info('话题已核对「%s」ID=%s，%.3f 秒', topic, self._expected_topic_id, time.monotonic()-started)
            return True
        except TopicSelectionError:
            raise
        except Exception as error:
            log.debug('话题「%s」在预算内未就绪：%s', topic, error)
            return False

    async def _current_topic(self):
        return (await self.opened_chat_info()).get('title', '')

    async def _open_visible_chat(self, peer):
        """Use an exact row in the active chat list before trying slower fallbacks."""
        handle = await self.page.evaluate_handle(r'''peer => [...document.querySelectorAll('.chatlist-chat[data-peer-id]')].find(el=>{
            if(el.getAttribute('data-peer-id')!==peer||!el.checkVisibility()||el.closest('.topics-slider'))return false;
            const pane=el.closest('.tabs-tab');return !pane||pane.classList.contains('active');
        }) || null''',str(peer))
        try:
            row = handle.as_element()
            if row is None:
                return False
            await row.click(timeout=1200)
            return True
        except Exception:
            return False
        finally:
            await handle.dispose()

    async def open_chat(self, chat_ref, title=None, topic=None, folder=None, topic_id=None, require_composer=True):
        await self._assert_media_clear()
        ref = str(chat_ref).strip().lstrip("#")
        self._mids_chat = ref
        self.last_sent_mids = self._sent_by_chat.setdefault(ref, set())
        self._expected_peer = ref if ref.lstrip("-").isdigit() else self._resolved_peers.get(ref.lower())
        self._last_actual_peer = None
        self._expected_topic = str(topic) if topic else None
        self._expected_topic_id = None
        self._configured_topic_id = str(topic_id) if topic_id else None
        try:
            await asyncio.wait_for(self._navigate(ref, title, topic, folder, require_composer=require_composer), timeout=45)
            await self.assert_destination()
        except asyncio.TimeoutError as error:
            raise RuntimeError(f"打开 {title or ref} 超过 45 秒；未发送，请检查会话是否仍可访问") from error

    async def _navigate(self, ref, title, topic, folder, require_composer=True):
        async def ready(timeout=3):
            if topic:
                if not await self._enter_topic(str(topic), timeout=timeout):
                    return False
            elif not await self._wait_opened(ref, timeout=timeout):
                return False
            try:
                if require_composer:
                    await self._first(SEL_COMPOSER, timeout=1500)
                await self.assert_destination()
                return True
            except RuntimeError:
                return False

        if ref.startswith("@") and not self._expected_peer:
            # Resolve a username from an actual matching search result, never its hash.
            await self._close_topic_panel()
            box = await self._first(SEL_SEARCH, timeout=1500)
            await box.fill("")
            await box.type(ref, delay=30)
            await asyncio.sleep(1.5)
            candidates = self.page.locator('.search-super .row, .chatlist-chat')
            for index in range(await candidates.count()):
                candidate = candidates.nth(index)
                if not await candidate.is_visible():
                    continue
                username = await candidate.get_attribute('data-username') or ''
                text = await candidate.inner_text()
                if username.lstrip('@').lower() == ref[1:].lower() or ref.lower() in text.lower().split():
                    pid = await candidate.get_attribute('data-peer-id')
                    if pid and pid.lstrip('-').isdigit():
                        self._resolved_peers[ref.lower()] = pid
                        self._expected_peer = pid
                        await candidate.click()
                        if await ready():
                            return
            raise RuntimeError("无法可靠解析 @用户名，请从会话列表选用数字 ID；未发送")
        target_ref = str(self._expected_peer or ref)
        if topic:
            info = await self.opened_chat_info()
            if info.get('pid') == self._expected_peer and info.get('title') == str(topic).strip() and info.get('topic_id'):
                if await ready(.2):
                    return
            state = await self.page.evaluate(self._CHAT_VIEW_JS, {'peer':self._expected_peer,'title':str(topic).strip()})
            if state['matches'] and await ready(5):
                return
        elif str(await self._opened_peer()) == str(self._expected_peer):
            if await ready(.2):
                return
        await self._close_topic_panel()
        if not topic and await self._open_visible_chat(self._expected_peer) and await ready(2):
            return
        if topic and folder and await self._open_via_folder(target_ref, folder, topic):
            await self.assert_destination()
            return
        await self.page.evaluate("h => { location.hash = h; }", target_ref)
        if await ready(2):
            return
        if folder and not topic and await self._open_via_folder(target_ref, folder, topic) and await ready(2):
            return
        item = await self._scroll_find(target_ref, time.monotonic() + 5)
        if item is not None:
            try:
                await self._close_topic_panel()
                await item.click(timeout=2500)
                if await ready(3):
                    return
            except Exception as error:
                log.debug('列表定位失败，改用搜索：%s', error)
        if title and self._expected_peer and await self._open_by_search(str(title), int(self._expected_peer)) and await ready(3):
            return
        await self.page.reload(wait_until="domcontentloaded", timeout=12000)
        if await ready(5):
            return
        if title and self._expected_peer and await self._open_by_search(str(title), int(self._expected_peer)) and await ready(3):
            return
        # A route can finish rendering just after a fallback exhausts its budget.
        # Recheck both the composer and destination before reporting a failure.
        if await ready(.5):
            return
        actual = await self._opened_peer()
        self._last_actual_peer = actual
        if str(actual) == str(self._expected_peer) and not topic:
            raise RuntimeError(f"目标会话 {title or ref} 已打开，但发送输入框尚未就绪或不可用；本次未发送。请先只读体检确认输入框可用后再重试")
        raise RuntimeError(f"未能打开 {title or ref}：目标 {self._expected_peer}，当前 {actual or '无'}；未发送。请从会话列表重新选择目标")

    async def probe_chat(self, chat_ref, title=None, topic=None, folder=None, method=None, topic_id=None):
        started = time.monotonic()
        result = {"chat": str(chat_ref), "name": title or str(chat_ref), "ok": False,
                  "via": "verified", "composer": False, "send_btn": False, "error": None}
        try:
            require_composer = method != "button_only"
            await self.open_chat(chat_ref, title, topic, folder, topic_id, require_composer=require_composer)
            result["composer"] = await self._visible(SEL_COMPOSER)
            result["send_btn"] = await self._visible(SEL_SEND)
            if method in ("image_text", "image_only"):
                result["attachment"] = await self._visible(['attach-menu-button', '.attach-file', "[aria-label='Chat.Input.Attach']"])
                if not result["attachment"]:
                    raise RuntimeError("目标已核对，但附件入口不可用")
            result["ok"] = True if method == "button_only" else result["composer"]
        except Exception as error:
            result["error"] = str(error)
        result["elapsed"] = round(time.monotonic() - started, 2)
        return result


    async def _composer_text(self):
        try:
            return await self.page.evaluate("""() => {
                const el = [...document.querySelectorAll(
                    "div[contenteditable='true'].input-message-input, div[contenteditable='true'][data-peer-id]")].find(e=>e.checkVisibility());
                return el ? el.textContent.trim() : '';
            }""")
        except Exception:
            return ""

    @asynccontextmanager
    async def preserve_draft(self):
        """Suspend this peer/topic's draft, then restore it without sending it."""
        self.last_draft_warning = None
        self._automation_text = None
        await self.assert_destination()
        composer = await self._first(SEL_COMPOSER)
        text, html = await composer.inner_text(), await composer.inner_html()
        has_atoms = await composer.locator('img,custom-emoji-element,[data-type="mention"]').count()
        if not text.strip() and not has_atoms:
            yield
            return
        record = {'peer':str(self._expected_peer),'topic':self._expected_topic,
                  'topic_id':self._expected_topic_id,'text':text,'html':html,'has_atoms':bool(has_atoms),'created':time.time()}
        backup = self.profile / 'checkin-drafts' / (uuid.uuid4().hex + '.json')
        atomic_write(backup, json.dumps(record, ensure_ascii=False, indent=2))
        try:
            await composer.click()
            await composer.press('ControlOrMeta+A')
            await composer.press('Backspace')
            remain_atoms = await composer.locator('img,custom-emoji-element,[data-type="mention"]').count()
            if (await composer.inner_text()).strip() or remain_atoms:
                raise RuntimeError('未能暂存输入框草稿，已停止发送')
            yield
        finally:
            task=asyncio.create_task(self._restore_draft(record, backup))
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                await task
                raise

    async def _restore_draft(self, record, backup):
        try:
            await self.assert_destination()
            if str(self._expected_peer)!=record['peer'] or self._expected_topic_id!=record['topic_id']:
                raise RuntimeError('当前会话或话题已改变')
            composer=await self._first(SEL_COMPOSER,timeout=2000)
            current=await composer.inner_text()
            current_atoms = await composer.locator('img,custom-emoji-element,[data-type="mention"]').count()
            if current.strip() or current_atoms:
                if await composer.inner_html()==record['html']:
                    backup.unlink(missing_ok=True)
                    return
                auto = str(self._automation_text or '').strip()
                cur = current.strip()
                is_auto_residue = bool(auto and cur and not current_atoms and (cur == auto or auto.startswith(cur)))
                if not is_auto_residue:
                    raise RuntimeError('输入框出现了新的草稿，已保留现有内容')
                await composer.click();await composer.press('ControlOrMeta+A');await composer.press('Backspace')
            await composer.click()
            # Let Telegram's editor parse the same HTML/plain-text clipboard payload.
            await composer.evaluate("""(el,record)=>{
                const data=new DataTransfer();data.setData('text/html',record.html);data.setData('text/plain',record.text);
                el.dispatchEvent(new ClipboardEvent('paste',{bubbles:true,cancelable:true,clipboardData:data}));
            }""",record)
            await asyncio.sleep(.1)
            fallback=False
            if not (await composer.inner_text()).strip() and record['text'].strip():
                await self.page.keyboard.insert_text(record['text'])
                fallback=True
            restored_text = (await composer.inner_text()).strip()
            restored_atoms = await composer.locator('img,custom-emoji-element,[data-type="mention"]').count()
            if restored_text != record['text'].strip():
                raise RuntimeError('编辑器恢复后的内容与原草稿不一致')
            if record.get('has_atoms') and not restored_atoms:
                raise RuntimeError('原草稿包含表情或富文本元素，未能确认完整恢复')
            if fallback and any(tag in record['html'].lower() for tag in ('<strong','<em>','<a ','<code','custom-emoji','data-type="mention"')):
                raise RuntimeError('草稿文字已恢复，富文本格式需要核对')
            await asyncio.sleep(.25)  # Editor input events allow Telegram to sync its draft.
            backup.unlink(missing_ok=True)
            log.info('[%s] 已恢复原会话草稿',self.name)
        except Exception as error:
            self.last_draft_warning=f'原草稿恢复需核对：{error}；备份保留于 {backup}'
            log.error('[%s] %s',self.name,self.last_draft_warning)

    async def _last_out_mid(self):
        """当前会话里我最后一条发出消息的 data-mid（没有则为 None）。"""
        try:
            return await self.page.evaluate(
                "() => { const bs = [...document.querySelectorAll("
                "'.bubbles .bubble.is-out')].filter(b=>b.checkVisibility());"
                " return bs.length ? bs[bs.length - 1].getAttribute('data-mid')"
                " : null; }")
        except Exception:
            return None

    async def _track_sent_mid(self, pre_mid):
        """发送完成后登记这条消息的 data-mid 到 last_sent_mids。

        之后凡是气泡上 data-reply-to-mid 命中的 is-in 消息，都是
        **明确回复给我这条消息**的——判定签到结果只采信它们，
        群里别人发同样签到词得到的结果、群友互相回复的闲聊都不再混入。
        bot 随后删除我的消息也不影响：mid 在发出的瞬间就登记好了。
        """
        boundary = self._capture_by_chat.get(self._mids_chat, {})
        deadline = time.monotonic() + 12
        existing = set(self.last_sent_mids)
        observed_ids = set()
        confirmed_aliases = set()
        self._last_send_confirmed_by_reply = False
        peer = str(boundary.get('peer') or '')
        private_chat = peer.isdigit()
        seen = set(str(m) for m in boundary.get('seen', ()))
        # Topic views may rebuild after Enter. Observe until an ID arrives, without
        # resending; the old three-second deadline ended while the view was loading.
        while time.monotonic() < deadline:
            if peer:
                observed = await self.page.evaluate(self._IN_COLLECT_JS, {'peer': peer, 'reset': False})
                cutoff = int(boundary.get('cutoff', time.time())) - 5
                items = observed.get('items', [])
                candidates = [record for record in items
                              if record.get('out') and str(record['mid']) not in seen
                              and str(record['mid']) not in existing
                              and record.get('ts', 0) >= cutoff]
                if candidates:
                    observed_ids.update(str(record['mid']) for record in candidates)
                    self.last_sent_mids.update(observed_ids)
                for old, new in observed.get('aliases', []):
                    old_s, new_s = str(old), str(new)
                    if old_s in observed_ids:
                        observed_ids.add(new_s)
                        self.last_sent_mids.add(new_s)
                        if '.' not in new_s:
                            confirmed_aliases.add(new_s)
                if any(not record.get('pending') for record in candidates) or confirmed_aliases:
                    return
                if private_chat and observed_ids and any(
                    not record.get('out') and not record.get('pending')
                    and str(record.get('mid', '')) not in seen
                    and record.get('ts', 0) >= cutoff
                    for record in items
                ):
                    # Bots in private chats frequently auto-delete our command in the same
                    # update batch as their reply before Web K upgrades the temporary mid.
                    self._last_send_confirmed_by_reply = True
                    return
            mid = await self._last_out_mid()
            if not peer and mid and str(mid) != str(pre_mid):
                self.last_sent_mids.add(str(mid))
                return
            await asyncio.sleep(0.12)
        error = RuntimeError("仅捕获临时消息 ID，尚未确认服务器接收；请核对 Telegram，不要直接重发" if observed_ids else "未能登记新消息 ID，发送状态不明；请核对 Telegram，不要直接重发")
        error.sent = True
        error.uncertain = True
        raise error

    def _start_send_clock(self, capture_since=None):
        self.send_started_at = time.time()
        self.send_started_monotonic = time.monotonic()
        boundary = self._capture_by_chat.get(self._mids_chat)
        if boundary is not None:
            boundary.update(cutoff=self.send_started_at if capture_since is None else capture_since, sent_at=self.send_started_at)

    async def send_text(self, text):
        await self._assert_media_clear()
        await self.assert_destination()
        if not text or not str(text).strip():
            raise ValueError("签到文字不能为空")
        if await self._composer_text():
            raise RuntimeError("目标输入框已有草稿，已保留草稿并停止发送")
        self._automation_text = str(text)
        composer = await self._first(SEL_COMPOSER)
        await composer.click()
        pre_mid = await self._last_out_mid()
        for index, line in enumerate(str(text).split("\n")):
            if index:
                await self.page.keyboard.press("Shift+Enter")
            await self.page.keyboard.type(line, delay=random_delay(30, 80))
        await self.assert_destination()
        if self.before_send:
            await self.before_send()
        capture_since = time.time()
        try:
            await self.page.keyboard.press("Enter")
        except Exception as original:
            error = RuntimeError('发送按键结果待核对，请先核对消息，不要直接重发')
            error.sent = error.uncertain = True
            raise error from original
        self._start_send_clock(capture_since)
        for _ in range(30):
            await asyncio.sleep(.1)
            if not await self._composer_text():
                await self._track_sent_mid(pre_mid)
                return
        error = RuntimeError("发送状态不明，输入框尚未清空；请先核对消息，勿直接重复发送")
        error.sent = error.uncertain = True
        raise error

    MEDIA_PREVIEW_SELECTOR = '.popup.popup-new-media:visible, .popup.popup-send-photo:visible'

    async def _assert_media_clear(self):
        if await self.page.locator(self.MEDIA_PREVIEW_SELECTOR).count():
            raise RuntimeError('存在未关闭的图片预览，请先在 Telegram 处理；未执行新的发送或会话切换')

    async def _media_control(self, preview, selectors, label, timeout=2000):
        deadline = time.monotonic() + timeout / 1000
        while time.monotonic() < deadline:
            for selector in selectors:
                controls = await preview.query_selector_all(selector)
                found = None
                for control in controls:
                    if found is None and await control.is_visible() and await control.is_enabled():
                        found = control
                    else:
                        await control.dispose()
                if found is not None:
                    return found
            await asyncio.sleep(min(.1, max(0, deadline - time.monotonic())))
        raise RuntimeError(f'图片预览的{label}不可用，未执行发送')

    async def _dismiss_media_preview(self):
        """Dismiss only the preview opened by this image attempt, before draft restore."""
        preview = self._media_preview
        close = None
        try:
            if preview is None and self._media_upload_started:
                popup = self.page.locator(self.MEDIA_PREVIEW_SELECTOR)
                if await popup.count() == 1:
                    preview = await popup.element_handle()
            if preview is None or not await preview.is_visible():
                return
            close = await preview.query_selector('button.popup-close, .popup-close')
            if close is None:
                raise RuntimeError('图片预览缺少关闭控件')
            await close.click(timeout=1500)
            await preview.wait_for_element_state('hidden',timeout=2000)
        except Exception:
            warning = '本轮图片预览未能关闭，请在 Telegram 核对；已停止后续会话切换'
            self.last_draft_warning = '；'.join(filter(None,(self.last_draft_warning,warning)))
        finally:
            if close is not None:
                try:
                    await close.dispose()
                except Exception:
                    pass
            if preview is not None:
                try:
                    await preview.dispose()
                except Exception:
                    pass
            self._media_preview = None
            self._media_upload_started = False

    async def _open_attach(self):
        """点开附件菜单（K 版发图要先展开它）。"""
        try:
            el = await self._first(['attach-menu-button','.attach-file',"[aria-label='Chat.Input.Attach']"],timeout=2000)
            await el.click(timeout=1500)
            return True
        except Exception:
            return False

    async def send_image(self, image_path, caption):
        await self._assert_media_clear()
        self._media_preview = None
        self._media_upload_started = self._media_dispatch_attempted = False
        try:
            return await self._send_image(image_path,caption)
        except Exception as error:
            if self._media_dispatch_attempted:
                error.sent = error.uncertain = True
            raise
        finally:
            task = asyncio.create_task(self._dismiss_media_preview())
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                await task
                raise

    async def _send_image(self, image_path, caption):
        """发图片（可带文字）。

        K 版**不能**直接 set_input_files —— 实测初始 input[type=file] 存在但传文件
        不产生任何预览。必须走：点附件按钮 → 点「图片或视频」→ 捕获文件选择框 →
        在预览层的「添加说明」里填字 → 点预览层的发送按钮。
        """
        await self.assert_destination()
        import pathlib
        path = pathlib.Path(str(image_path))
        if not path.is_absolute():
            path = BASE_DIR / path
        if not path.is_file():
            raise FileNotFoundError(f"图片不存在：{path}")
        pre_mid = await self._last_out_mid()

        # 1) 展开附件菜单 + 选中「图片或视频」，捕获文件选择框
        try:
            async with self.page.expect_file_chooser(timeout=10000) as fc_info:
                if not await self._open_attach():
                    raise RuntimeError("找不到附件按钮")
                menu = await self._first([
                    ".btn-menu-item:has-text('图片或视频')",
                    ".btn-menu-item:has-text('相片或影片')",
                    ".btn-menu-item:has-text('圖片或影片')",
                    ".btn-menu-item:has-text('Photo or Video')",
                ], timeout=2500)
                await menu.click(timeout=1500)
            chooser = await fc_info.value
            self._media_upload_started = True
            await chooser.set_files(str(path))
        except Exception as e:
            raise RuntimeError(f"选择图片失败：{type(e).__name__}: {e}")

        # 2) 等预览层出现（这是「图片真的进来了」的唯一可靠信号）
        popup = await self._first([self.MEDIA_PREVIEW_SELECTOR],timeout=7000)
        preview = self._media_preview = await popup.element_handle()
        if preview is None:
            raise RuntimeError('图片预览已关闭，未执行发送')

        # 3) 说明文字填到预览层的输入框里（主输入框此时不是发送目标）
        if caption:
            el = await self._media_control(preview,['[contenteditable="true"]','textarea','input[placeholder]'],'说明输入框')
            try:
                await el.click(timeout=1500)
                for index, line in enumerate(str(caption).split("\n")):
                    if index:
                        await self.page.keyboard.press("Shift+Enter")
                    await self.page.keyboard.type(line, delay=random_delay(30, 80))
            finally:
                await el.dispose()

        await self.assert_destination()
        # 4) 点预览层的发送按钮（Enter 在预览层不保证触发）
        btn = await self._media_control(preview,['.simple-message-input-confirm','.btn-send'],'发送按钮')
        try:
            if self.before_send:
                await self.before_send()
            capture_since = time.time()
            self._media_dispatch_attempted = True
            await btn.click(timeout=2000)
        finally:
            await btn.dispose()
        self._start_send_clock(capture_since)

        # 5) 验证预览层消失 = 已发出
        try:
            await preview.wait_for_element_state('hidden',timeout=4000)
        except Exception as error:
            raise RuntimeError('图片发送结果待核对，请先核对消息，不要直接重发') from error
        await self._track_sent_mid(pre_mid)

    _CLICK_BUTTON_JS = r"""({allowed, privateChat, cutoff, seen, textPart, selectors}) => {
        const normalize = v => String(v || '').replace(/[\u200b-\u200f\uFEFF\uFE0E\uFE0F]/g, '').replace(/\s+/g, ' ').trim();
        const needle = normalize(textPart);
        const allowedSet = new Set(allowed.map(String));
        const seenSet = new Set(seen.map(String));
        const bubbles = [...document.querySelectorAll('.bubbles .bubble.is-in')];
        for (let i = bubbles.length - 1; i >= 0; i--) {
            const b = bubbles[i];
            const mid = b.getAttribute('data-mid');
            const stamp = Number(b.getAttribute('data-timestamp') || 0);
            const replyTo = b.getAttribute('data-reply-to-mid');
            const related = (replyTo && allowedSet.has(String(replyTo))) || (privateChat && stamp >= Math.floor(cutoff) - 5);
            if (!related || (mid && seenSet.has(String(mid)))) continue;
            const btns = [...b.querySelectorAll(selectors)].filter(el => el.checkVisibility() && !el.parentElement?.closest(selectors));
            for (const btn of btns) {
                const clone = (btn.querySelector('.reply-markup-button-text') || btn).cloneNode(true);
                clone.querySelectorAll('img[alt]').forEach(img => img.replaceWith(img.alt));
                if (normalize(clone.textContent).includes(needle)) {
                    const isLink = !!btn.getAttribute('href') || !!btn.querySelector('a[href], [href]');
                    return {el: btn, mid: mid ? String(mid) : null, isLink};
                }
            }
        }
        return null;
    }"""

    async def click_button(self, text_part, timeout=25):
        await self.assert_destination()
        boundary = self._capture_by_chat.get(self._mids_chat, {})
        cutoff = boundary.get("cutoff", time.time())
        deadline = time.monotonic() + timeout
        selectors = ", ".join(SEL_BTN_ROW)
        while time.monotonic() < deadline:
            await self.assert_destination()
            observed = await self.page.evaluate(self._IN_COLLECT_JS, {"peer": str(self._expected_peer), "reset": False})
            for old, new in observed.get("aliases", []):
                if str(old) in self.last_sent_mids:
                    self.last_sent_mids.add(str(new))
                if self._active_mids is not None and str(old) in self._active_mids:
                    self._active_mids.add(str(new))
            private = bool(self._expected_peer and str(self._expected_peer).isdigit())
            allowed = self._active_mids if self._active_mids is not None else self.last_sent_mids
            handle = await self.page.evaluate_handle(self._CLICK_BUTTON_JS, {
                "allowed": sorted(str(x) for x in allowed),
                "privateChat": private,
                "cutoff": cutoff,
                "seen": sorted(str(x) for x in boundary.get("seen", ())),
                "textPart": text_part,
                "selectors": selectors,
            })
            try:
                is_null = await handle.evaluate("v => !v")
                if not is_null:
                    meta = await handle.evaluate("v => ({mid: v.mid, isLink: v.isLink})")
                    if meta.get("isLink"):
                        raise RuntimeError("匹配到网址按钮，已停止自动点击")
                    el_handle = await handle.get_property("el")
                    try:
                        button = el_handle.as_element()
                        if button:
                            await self.assert_destination()
                            await button.click(timeout=max(100, int((deadline - time.monotonic()) * 1000)))
                            if meta.get("mid") and self._active_mids is not None:
                                self._active_mids.add(meta["mid"])
                            return True
                    finally:
                        await el_handle.dispose()
            finally:
                await handle.dispose()
            await asyncio.sleep(min(.25, max(0, deadline - time.monotonic())))
        return False

    _EXISTING_BUTTON_JS = r"""({peer, mid, label, earliest, selectors}) => {
        const normalize = value => String(value || '').replace(/[\u200b-\u200f\uFEFF\uFE0E\uFE0F]/g,'').replace(/\s+/g,' ').trim();
        const isSafeButton = el => el.matches('button:not([href], [disabled], [aria-disabled="true"], .is-disabled)') &&
            !el.querySelector('.reply-markup-button-icon, a, [href]');
        const matches = rows => rows.filter(b => b.dataset.peerId === peer && b.checkVisibility() &&
            (mid ? b.dataset.mid === mid : Number(b.dataset.timestamp) >= earliest - 5))
            .flatMap(b => [...b.querySelectorAll(selectors)].filter(el => {
                if (!el.checkVisibility() || el.parentElement?.closest(selectors) || !isSafeButton(el)) return false;
                const text = (el.querySelector('.reply-markup-button-text') || el).cloneNode(true);
                text.querySelectorAll('img[alt]').forEach(img => img.replaceWith(img.alt));
                return normalize(text.textContent) === normalize(label);
            }).map(el => ({el, mid:Number(b.dataset.mid), ts:Number(b.dataset.timestamp || 0)})));
        const rows = [...document.querySelectorAll('.bubbles .bubble.is-in[data-mid]')];
        const found = matches(rows).sort((a,b) => b.ts - a.ts || b.mid - a.mid);
        if (!found.length || found[1]?.mid === found[0].mid) return null;
        return found[0].el;
    }"""

    _EXISTING_BUTTON_DIAG_JS = r"""({peer, mid, label, earliest, selectors}) => {
        const normalize = value => String(value || '').replace(/[\u200b-\u200f\uFEFF\uFE0E\uFE0F]/g,'').replace(/\s+/g,' ').trim();
        const isSafeButton = el => el.matches('button:not([href], [disabled], [aria-disabled="true"], .is-disabled)') &&
            !el.querySelector('.reply-markup-button-icon, a, [href]');
        const needle = normalize(label);
        const rows = [...document.querySelectorAll('.bubbles .bubble.is-in[data-mid]')].filter(b => b.dataset.peerId === peer && b.checkVisibility());
        const inspect = bubble => [...bubble.querySelectorAll(selectors)].filter(el => {
            if (!el.checkVisibility() || el.parentElement?.closest(selectors)) return false;
            const text = (el.querySelector('.reply-markup-button-text') || el).cloneNode(true);
            text.querySelectorAll('img[alt]').forEach(img => img.replaceWith(img.alt));
            return normalize(text.textContent) === needle;
        }).map(el => ({safe: isSafeButton(el), mid: bubble.dataset.mid, ts: Number(bubble.dataset.timestamp || 0)}));
        const inScope = rows.filter(b => mid ? b.dataset.mid === mid : Number(b.dataset.timestamp) >= earliest - 5);
        const scopeMatches = inScope.flatMap(inspect);
        const safeInScope = scopeMatches.filter(m => m.safe);
        const unsafeInScope = scopeMatches.filter(m => !m.safe);
        const olderSafe = mid ? [] : rows.filter(b => Number(b.dataset.timestamp) < earliest - 5).flatMap(inspect).filter(m => m.safe);
        olderSafe.sort((a, b) => b.ts - a.ts);
        return {
            midFound: mid ? inScope.length > 0 : null,
            duplicateSafe: safeInScope.length >= 2,
            unsafeCount: unsafeInScope.length,
            latestOlderTs: olderSafe.length ? olderSafe[0].ts : null,
        };
    }"""

    async def click_existing_button(self, label, reference, earliest):
        """Click one incoming message's inline button within the verified conversation."""
        await self.assert_destination()
        self._last_button_initial_text = None
        peer, mid = str(self._expected_peer), None
        if reference:
            mid, linked_peer, linked_topic = button_message_ref(reference)
            if linked_peer:
                info = await self.opened_chat_info()
                opened_user = ('@' + info.get('username', '').lstrip('@').lower()) if info.get('username') else ''
                resolved_match = self._resolved_peers.get(linked_peer) == peer
                if linked_peer not in (peer, str(self._mids_chat).lower(), opened_user) and not resolved_match:
                    raise ValueError("按钮消息链接与已核对的会话不符，请使用当前会话内的链接或数字消息 ID")
            if linked_topic and linked_topic != self._expected_topic_id:
                raise ValueError("按钮消息链接与所选话题不符")
            route = f'{peer}?message={mid}'
            if self._expected_topic_id:
                route += f'&thread={self._expected_topic_id}'
            await self.page.evaluate('route => { location.hash = route; }', route)
        elif not peer.isdigit():
            raise ValueError("群聊的仅按钮签到需要指定消息链接或数字消息 ID")
        args = {'peer':peer, 'mid':mid, 'label':label, 'earliest':earliest, 'selectors':', '.join(SEL_BTN_ROW)}
        try:
            handle = await self.page.wait_for_function(self._EXISTING_BUTTON_JS, arg=args, timeout=5000)
        except PlaywrightTimeoutError as error:
            reason = f"没有找到唯一可点的签到按钮「{label}」，请核对消息位置和按钮完整文字"
            try:
                diag = await self.page.evaluate(self._EXISTING_BUTTON_DIAG_JS, args)
                if mid and diag.get('midFound') is False:
                    reason = f"未找到指定的按钮消息（ID {mid}），请核对按钮消息链接或数字消息 ID"
                elif diag.get('duplicateSafe'):
                    reason = f"同一条消息中存在多个同名按钮「{label}」，为防误点已停止"
                elif diag.get('unsafeCount'):
                    reason = f"找到按钮「{label}」，但它是网址或不可点击按钮，已跳过"
                elif diag.get('latestOlderTs'):
                    when = datetime.fromtimestamp(diag['latestOlderTs'], ZoneInfo(timezone_name(self.cfg))).strftime('%Y-%m-%d %H:%M')
                    reason = f"今天尚未收到包含按钮「{label}」的新消息（最近匹配消息时间 {when}），未点击历史按钮；若需点击固定旧消息，请在规则中填写按钮消息链接或 ID"
            except Exception:
                pass
            raise RuntimeError(reason) from error
        try:
            button = handle.as_element()
            await self.assert_destination()
            if await self.page.locator('.popup-confirmation:visible').count():
                raise RuntimeError("当前已有 Telegram 弹窗，请先处理后再运行签到")
            meta = await button.evaluate(r"""el => {
                const b = el.closest('.bubble');
                const message = b?.querySelector('.message');
                const body = (message?.querySelector('[data-message-text-source]') || message)?.cloneNode(true);
                if (body) {
                    body.querySelectorAll('img[alt]').forEach(img => img.replaceWith(img.alt));
                    body.querySelectorAll('.time, .reactions, .reply-markup').forEach(n => n.remove());
                }
                return {mid: b?.dataset.mid || null, text: (body ? body.textContent : '').trim()};
            }""")
            origin = meta['mid']
            self._last_button_initial_text = meta['text']
            if self.before_send:
                await self.before_send()
            capture_since = time.time()
            try:
                await button.click(timeout=2000)
            except Exception as original:
                error = RuntimeError("按钮点击结果待核对，请检查 Telegram，勿重复点击")
                error.sent = error.uncertain = True
                raise error from original
            self._start_send_clock(capture_since)
            return origin
        finally:
            await handle.dispose()

    async def read_button_popup(self, seconds, *, poll_timeout=None):
        """Read the new Telegram callback alert or toast before dismissing its sole OK button."""
        base_remaining = (self.send_started_monotonic + seconds - time.monotonic()) if self.send_started_monotonic is not None else seconds
        remaining = min(base_remaining, poll_timeout) if poll_timeout is not None else base_remaining
        if remaining <= 0:
            return ''
        try:
            handle = await self.page.wait_for_function(r"""() => {
                const visible = el => el.checkVisibility() && el.getBoundingClientRect().height > 0;
                const dialogs = [...document.querySelectorAll('.popup-confirmation')].filter(visible);
                if (dialogs.length === 1) {
                    const popup = dialogs[0];
                    const buttons = [...popup.querySelectorAll('.popup-buttons button, .popup-footer button')].filter(visible);
                    const okBtn = buttons.length === 1 && !popup.querySelector('input, textarea, select, a[href]');
                    const desc = popup.querySelector('.popup-description')?.textContent.trim();
                    if (okBtn && desc) return {kind: 'popup', el: popup, text: desc};
                }
                const toasts = [...document.querySelectorAll('.toast')].filter(visible);
                if (toasts.length === 1) {
                    const text = toasts[0].innerText.trim();
                    if (text) return {kind: 'toast', el: toasts[0], text};
                }
                return null;
            }""", timeout=max(1, int(remaining*1000)))
        except PlaywrightTimeoutError:
            return ''
        try:
            info = await handle.evaluate("v => ({kind: v.kind, text: v.text})")
            if info['kind'] == 'toast':
                await self.assert_destination()
                return info['text']
            el_handle = await handle.get_property('el')
            try:
                popup = el_handle.as_element()
                await self.assert_destination()
                text = await popup.eval_on_selector('.popup-description', 'el => el.innerText.trim()')
                button = await popup.query_selector('.popup-buttons button:visible, .popup-footer button:visible')
                try:
                    if button:
                        await button.click(timeout=1500)
                except Exception:
                    log.warning('已读到机器人弹窗，关闭按钮未就绪')
                finally:
                    if button:
                        try:
                            await button.dispose()
                        except Exception:
                            pass
                return text
            finally:
                await el_handle.dispose()
        finally:
            await handle.dispose()

    _LATEST_VIEW_JS = r"""({peer, element=false, align=false}) => {
        const visible=(el,empty=false)=>{
            if(!el?.checkVisibility())return false;
            for(let p=el;p&&p!==document.body;p=p.parentElement){
                if(Number(getComputedStyle(p).opacity)<.05)return false;
            }
            const r=el.getBoundingClientRect();
            return r.width>0&&(empty||r.height>0)&&r.left<innerWidth&&r.right>0&&(empty||r.top<innerHeight&&r.bottom>0);
        };
        const button=[...document.querySelectorAll('button.bubbles-go-down')].find(b=>{
            if(!visible(b)||b.disabled)return false;
            const r=b.getBoundingClientRect(),top=document.elementFromPoint(r.left+r.width/2,r.top+r.height/2);
            return top===b||b.contains(top);
        });
        if(element)return button||null;
        const pane=[...document.querySelectorAll('.bubbles')].find(e=>visible(e,true));
        const owner=button?.closest('.chat')||pane?.closest('.chat');
        const pending=owner?owner.classList.contains('is-go-down-visible'):!!button;
        const bubble=[...document.querySelectorAll('.bubbles .bubble[data-mid]')].find(b=>b.dataset.peerId===peer&&visible(b));
        const empty=[...document.querySelectorAll('.bubbles-inner.no-messages')].some(e=>visible(e,true));
        let scrolled=false;
        const viewport=pane?.querySelector('.bubbles-scrollable');
        const initial_loading=!!viewport&&!bubble&&!empty&&viewport.scrollHeight<=viewport.clientHeight+10;
        if(align&&!bubble&&viewport&&viewport.scrollTop>=0&&viewport.scrollTop+viewport.clientHeight<viewport.scrollHeight-20){
            const before=viewport.scrollTop;viewport.scrollTop=viewport.scrollHeight;scrolled=Math.abs(viewport.scrollTop-before)>1;
        }
        if(!owner&&!pending&&bubble){
            for(let el=bubble.parentElement;el&&el!==document.body;el=el.parentElement){
                if(el.scrollHeight>el.clientHeight+10&&el.scrollTop>=0&&el.scrollTop+el.clientHeight<el.scrollHeight-20){
                    const before=el.scrollTop;el.scrollTop=el.scrollHeight;scrolled||=Math.abs(el.scrollTop-before)>1;
                }
            }
        }
        return {ready:!!bubble||empty,has_jump:pending,clickable:!!button,scrolled,initial_loading};
    }"""

    async def _scroll_chat_bottom(self):
        await self.assert_destination()
        deadline=time.monotonic()+18
        attempts=0;last_click=0;settled=None
        while time.monotonic()<deadline:
            current=time.monotonic()
            state=await self.page.evaluate(self._LATEST_VIEW_JS,{'peer':str(self._expected_peer),'align':attempts>0})
            if state['has_jump'] and state['clickable'] and not state['initial_loading'] and attempts<6 and current-last_click>=(.8 if state['ready'] else 2):
                handle=await self.page.evaluate_handle(self._LATEST_VIEW_JS,{'peer':str(self._expected_peer),'element':True})
                try:
                    button=handle.as_element()
                    if button:
                        attempts+=1;last_click=current
                        try:await button.click(timeout=800)
                        except Exception:pass  # The navigation control can disappear during a valid jump.
                finally:await handle.dispose()
            if (state['ready'] or str(self._expected_peer).isdigit()) and not state['initial_loading'] and not state['has_jump'] and not state['scrolled']:
                settled=settled or current
                if current-settled>=.15:
                    await self.assert_destination()
                    return
            else:settled=None
            await asyncio.sleep(.08)
        raise RuntimeError('无法同步到最新消息，已停止发送以避免漏抓反馈；请在 Telegram 核对')


    _IN_COLLECT_JS = r"""(options) => {
        let capture = window.__checkinCapture;
        if (!capture || capture.peer !== options.peer || options.reset) {
            if (capture) capture.observer.disconnect();
            capture = {peer:options.peer, records:new Map(), ids:new WeakMap(), aliases:[], aliasKeys:new Set(), observer:null};
            capture.read = b => {
                    if (!b?.matches?.('.bubble[data-mid]') || b.getAttribute('data-peer-id') !== options.peer) return;
                    const mid=b.getAttribute('data-mid'), previous=capture.ids.get(b);
                    if(previous && previous!==mid){
                        const k = previous + '->' + mid;
                        if(!capture.aliasKeys.has(k)){
                            capture.aliasKeys.add(k);
                            capture.aliases.push([previous,mid]);
                            if(capture.aliases.length > 200) capture.aliases.shift();
                        }
                    }
                    capture.ids.set(b,mid);
                    const message=b.querySelector('.message');
                    const rawBody=message?.querySelector('[data-message-text-source]')||message;
                    let text='';
                    if(rawBody){
                        const clone=rawBody.cloneNode(true);
                        clone.querySelectorAll('img[alt]').forEach(img=>img.replaceWith(img.alt));
                        clone.querySelectorAll('.time, .reactions, .reply-markup').forEach(n=>n.remove());
                        text=clone.textContent||'';
                    }else{
                        text=b.textContent||'';
                    }
                    const t=text.trim().replace(/(?:\s*(?:edited|已编辑))?(?:\s*\d{1,2}:\d{2}(?:\s*[AP]M)?){1,2}\s*$/i, '').trim();
                    const mentions=[...new Set([...rawBody?.querySelectorAll('[data-follow]')||[]].map(e=>e.getAttribute('data-follow')).filter(id=>/^\d+$/.test(id)))];
                    const record={mid,t,rm:b.getAttribute('data-reply-to-mid'),ts:Number(b.getAttribute('data-timestamp')||0),
                                  peer:options.peer,mentions,out:b.classList.contains('is-out'),pending:b.classList.contains('is-sending')||mid.includes('.')};
                    capture.records.set(mid,record);
                    if(capture.records.size>1000)capture.records.delete(capture.records.keys().next().value);
            };
            capture.scan = () => document.querySelectorAll('.bubbles .bubble[data-mid]').forEach(capture.read);
            capture.observer=new MutationObserver(mutations => {
                // Bots can remove a command before the next poll, or even in the same DOM batch.
                for (const mutation of mutations) {
                    for (const node of [...mutation.addedNodes,...mutation.removedNodes]) {
                        if (node.nodeType !== 1) continue;
                        capture.read(node);
                        if (node.querySelectorAll) node.querySelectorAll('.bubble[data-mid]').forEach(capture.read);
                    }
                    const target = mutation.target.nodeType === 1 ? mutation.target : mutation.target.parentElement;
                    if (target) capture.read(target.closest('.bubble[data-mid]'));
                }
            });
            capture.observer.observe(document.body,{subtree:true,childList:true,characterData:true,attributes:true,attributeFilter:['data-mid','data-reply-to-mid','data-peer-id','data-timestamp','class']});
            window.__checkinCapture=capture;
        }
        capture.scan();
        return {items:[...capture.records.values()],aliases:[...capture.aliases]};
    }"""

    async def current_user_peer(self):
        """Read the active account's ID from its account-menu avatar, never storage."""
        if self._self_peer:
            return self._self_peer
        toggle = self.page.locator('.sidebar-tools-button:visible').first
        if not await toggle.count():
            return None
        opened = False
        try:
            if await toggle.get_attribute('aria-expanded') != 'true':
                await toggle.click(timeout=1200)
                opened = True
            avatar = self.page.locator('[role="menu"]:visible .avatar.active[data-peer-id]')
            await avatar.first.wait_for(state='visible', timeout=1200)
            peers = await avatar.evaluate_all('(items)=>[...new Set(items.map(e=>e.dataset.peerId))]')
            if len(peers) == 1 and str(peers[0]).isdigit():
                self._self_peer = str(peers[0])
        except Exception:
            return None
        finally:
            if opened:
                try:
                    if await self.page.locator('[role="menu"]:visible').count():
                        await self.page.keyboard.press('Escape')
                except Exception:
                    pass
        return self._self_peer

    async def collect_feedback_history(self, known, seconds=8):
        """Load the original send window by scrolling history; never edit or send."""
        if not isinstance(known, dict) or not known.get('peer') or not known.get('cutoff'):
            return {'items': [], 'aliases': []}
        await self.assert_destination()
        deadline, aliases = time.monotonic() + seconds, []
        data = {'items': []}
        stalled = 0
        while True:
            data = await self.page.evaluate(self._IN_COLLECT_JS, {'peer':known['peer'], 'reset':False})
            aliases.extend(data.get('aliases', []))
            stamps = [r['ts'] for r in data['items'] if r.get('ts')]
            if stamps and min(stamps) < known['cutoff'] - 2 or time.monotonic() >= deadline:
                break
            previous = await self.page.evaluate('''peer => [...document.querySelectorAll('.bubbles .bubble[data-mid]')].filter(b=>b.dataset.peerId===peer).map(b=>b.dataset.mid).join(',')''', known['peer'])
            moved = await self.page.evaluate('''() => {
                const scroller=[...document.querySelectorAll('.bubbles .bubbles-scrollable')].find(e=>e.checkVisibility());
                if(!scroller)return {ok:false, atTop:true, scrollable:false};
                const scrollable = scroller.scrollHeight > scroller.clientHeight + 10;
                const atTop = scroller.scrollTop <= 1;
                scroller.scrollTop=0;scroller.dispatchEvent(new Event('scroll'));
                return {ok:true, atTop, scrollable};
            }''')
            if not moved.get('ok'):
                break
            try:
                await self.page.wait_for_function('''({peer,previous}) => [...document.querySelectorAll('.bubbles .bubble[data-mid]')].filter(b=>b.dataset.peerId===peer).map(b=>b.dataset.mid).join(',')!==previous''',
                    arg={'peer':known['peer'], 'previous':previous}, timeout=max(1,min(700,int((deadline-time.monotonic())*1000))))
                stalled = 0
            except Exception:
                if self.page.is_closed():
                    raise
                stalled += 1
                if moved.get('atTop') and (not moved.get('scrollable') or stalled >= 2):
                    break
        return {**data, 'aliases':aliases}

    async def recover_sent_mids(self, known, sent_text=None):
        """Recover only a unique own message or a reply explicitly naming this account."""
        empty = {'sent_mids': [], 'evidence': []}
        if not isinstance(known, dict) or not known.get('peer') or not isinstance(known.get('cutoff'), (int,float)):
            return empty
        await self.assert_destination()
        data = await self.page.evaluate(self._IN_COLLECT_JS, {'peer':known['peer'], 'reset':False})
        seen = {str(mid) for mid in known.get('seen', [])}
        window = [r for r in data['items'] if not r.get('pending') and (str(r['mid']) not in seen or str(r['mid']) in known.get('versions', {}))
                  and int(known['cutoff']) - 5 <= r.get('ts',0) <= known['cutoff'] + 30]
        outgoing = [r for r in window if r.get('out') and sent_text is not None and r['t'] == str(sent_text).strip()]
        if len(outgoing) == 1:
            return {'sent_mids':[str(outgoing[0]['mid'])], 'evidence':outgoing, 'source':'own_outgoing_history'}
        if len(outgoing) > 1:
            return empty
        replies = [r for r in window if not r.get('out') and r.get('rm') and str(r['rm']) not in seen
                   and len(r.get('mentions', [])) == 1]
        if not replies:
            return empty
        own = await self.current_user_peer()
        await self.assert_destination()
        replies = [r for r in replies if r['mentions'] == [own]] if own else []
        ids = {str(r['rm']) for r in replies}
        if len(ids) != 1:
            return empty
        return {'sent_mids':sorted(ids), 'evidence':replies, 'source':'account_mention_reply', 'account_peer':own}

    async def snapshot_incoming(self, mode="after"):
        self.send_started_at = None
        self.send_started_monotonic = None
        await self.assert_destination()
        await self._scroll_chat_bottom()
        await asyncio.sleep(.12)
        data = await self.page.evaluate(self._IN_COLLECT_JS, {"peer": str(self._expected_peer), "reset": True})
        baseline = {"seen": sorted({str(it["mid"]) for it in data.get("items", [])}), "versions": {},
                    "cutoff": time.time(), "peer": str(self._expected_peer)}
        self._capture_by_chat[self._mids_chat] = baseline
        return baseline

    async def watch_new_incoming(self, known, seconds, mode="after", idle_after=2.0, sent_mids=None, started_at=None, is_complete=None, *, watch_button_mid=None, button_initial_text=None, check_popup=False):
        if not isinstance(known, dict) or not known.get("peer"):
            raise ValueError("缺少本轮消息 ID/时间边界，不能安全补查旧记录")
        seen = set(known.get("seen", []))
        known["seen"] = sorted(seen)
        known.setdefault("versions", {})
        relations = known.setdefault('relations', {})
        self._capture_by_chat[self._mids_chat] = known
        deadline = (started_at if started_at is not None else time.monotonic()) + max(0, seconds)
        found, replied, evidence = [], [], []
        allowed = set(sent_mids) if sent_mids is not None else self.last_sent_mids
        last_related = None
        first_read = True
        button_mid_str = str(watch_button_mid) if watch_button_mid else None
        while first_read or time.monotonic() < deadline:
            first_read = False
            if check_popup:
                popup_text = await self.read_button_popup(seconds, poll_timeout=0.05)
                if popup_text:
                    rec = {'source': 'button_popup', 't': popup_text, 'mid': button_mid_str, 'ts': time.time()}
                    found.append(popup_text)
                    replied.append(popup_text)
                    evidence.append(rec)
                    break
            remaining = deadline - time.monotonic()
            remaining = remaining if remaining > 0 else .1
            try:
                data = await asyncio.wait_for(self.page.evaluate(self._IN_COLLECT_JS, {"peer": known["peer"], "reset": False}), timeout=remaining)
            except asyncio.TimeoutError:
                break
            for old, new in data.get("aliases", []):
                if str(old) in self.last_sent_mids:
                    self.last_sent_mids.add(str(new))
                if str(old) in allowed:
                    allowed.add(str(new))
            if not allowed and known.get('sent_at') and not known.get('button_only'):
                recovered = await self.recover_sent_mids(known, self._automation_text)
                allowed.update(recovered['sent_mids'])
                self.last_sent_mids.update(allowed)
            for record in data.get("items", []):
                mid = str(record["mid"])
                if record.get("out") or not record.get("t"):
                    continue
                exact = str(record.get("rm")) in allowed
                edited_button = bool(button_mid_str and mid == button_mid_str and button_initial_text is not None and record['t'] != button_initial_text)
                if not exact and not edited_button and record.get("ts", 0) < int(known["cutoff"]) - 5:
                    continue
                private = str(known["peer"]).isdigit()
                relation = 'button_edit' if edited_button else ('reply:' + str(record.get('rm')) if exact else 'private' if private else 'unrelated')
                if not edited_button and mid in seen and (mid not in known['versions'] or known['versions'][mid] == record['t'] and relations.get(mid, 'unrelated') == relation):
                    continue
                if edited_button and known['versions'].get(mid) == record['t'] and relations.get(mid) == 'button_edit':
                    continue
                if not exact and not private and not edited_button and mode == "reply_only":
                    continue
                seen.add(mid)
                known["seen"] = sorted(seen)
                known["versions"][mid] = record["t"]
                relations[mid] = relation
                found.append(record["t"])
                source = "button_popup" if edited_button else ("reply_to_sent" if exact else "private_chat" if private else "unrelated")
                record = {**record, "source": source}
                evidence.append(record)
                if exact or private or edited_button:
                    replied.append(record["t"])
                    last_related = time.monotonic()
            if last_related is not None and time.monotonic() - last_related >= idle_after and (is_complete is None or is_complete(replied)):
                break
            await asyncio.sleep(min(.2, max(0, deadline - time.monotonic())))
        self.last_replied, self.last_evidence = replied, evidence
        self.last_allowed_mids = sorted(allowed)
        return found

    async def _visible(self, selectors):
        try:
            await self._first(selectors, timeout=150)
            return True
        except RuntimeError:
            return False


    async def list_folders(self):
        """列出 Telegram 的会话分组（文件夹）名称。只读，不发送任何消息。"""
        await self.ensure_login()
        await self._wait_dialog_list()
        return await self._list_folders_raw()

    async def _open_folder(self, name):
        """切换到指定分组标签（界面导航，不发送任何消息）。"""
        try:
            ok = await self.page.evaluate("""(name) => {
                const bar = document.querySelector('.folders-tabs-scrollable');
                if (!bar) return false;
                let target = null;
                for (const el of bar.querySelectorAll('*')) {
                    let direct = '';
                    for (const n of el.childNodes)
                        if (n.nodeType === 3) direct += n.textContent;
                    direct = direct.trim();
                    if (!direct) continue;
                    const base = direct.replace(/\\s*\\d+$/, '').trim();
                    if (base === name) { target = el; break; }
                }
                if (!target) return false;
                target.click();  // 事件冒泡到 K 的分组切换处理器
                return true;
            }""", name)
            if not ok:
                return False
            await self.page.wait_for_timeout(1500)
            return True
        except Exception:
            return False

    _TOPICS_JS = r"""({peer,scroll=false}) => {
        const visible=e=>{if(!e?.checkVisibility())return false;const r=e.getBoundingClientRect();return r.right>0&&r.left<innerWidth&&r.bottom>0&&r.top<innerHeight};
        const pane=[...document.querySelectorAll('.topics-slider .topics-container')].find(p=>visible(p)&&
            p.querySelector('.sidebar-header .peer-title[data-peer-id], .sidebar-header__title .peer-title[data-peer-id]')?.getAttribute('data-peer-id')===peer);
        if(!pane)return null;
        const items=[...pane.querySelectorAll('a[data-peer-id][data-thread-id]')].filter(e=>e.dataset.peerId===peer&&/^[1-9]\d*$/.test(e.dataset.threadId)).map(e=>({
            id:e.dataset.threadId,title:e.querySelector('.row-title .peer-title')?.textContent.trim()||'',
            closed:!!e.querySelector('.topic-closed, .tgico-lock')
        })).filter(e=>e.title);
        const sc=pane.querySelector('.scrollable-y');let moved=false;
        if(sc&&scroll){const before=sc.scrollTop;sc.scrollTop+=Math.max(100,sc.clientHeight*.8);moved=sc.scrollTop>before;}
        return {is_forum:true,items,moved};
    }"""

    async def list_topics(self, chat_ref, title=None, folder=None):
        """Identify the exact peer and read its topic drawer; never touch a composer."""
        peer=str(chat_ref)
        if not peer.lstrip('-').isdigit():
            raise ValueError('请从会话列表选择数字 ID，再读取话题')
        if not peer.startswith('-'):
            return {'is_forum':False,'topics':[]}
        await self.ensure_login()
        current=await self.page.evaluate(self._TOPICS_JS,{'peer':peer})
        if not current:
            await self._close_topic_panel()
            await self.page.evaluate('peer=>{location.hash=peer}',peer)
            ready_js="""peer=>{
                const topics=("""+self._TOPICS_JS+""")({peer});
                if(topics)return topics;
                const info=("""+self._CHAT_VIEW_JS+""")();
                const row=[...document.querySelectorAll('.chatlist-chat')].find(e=>e.dataset.peerId===peer&&!e.closest('.topics-slider'));
                if(info.pid===peer&&!info.topic_id&&!row?.querySelector('.is-forum'))return {is_forum:false};
                return false;
            }"""
            try:
                handle=await self.page.wait_for_function(ready_js,arg=peer,timeout=3500)
            except Exception:
                if folder:await self._open_folder(folder)
                row=await self._scroll_find(peer,time.monotonic()+5)
                if row is not None:await row.click(timeout=2500)
                elif title:await self._open_by_search(str(title),int(peer))
                handle=await self.page.wait_for_function(ready_js,arg=peer,timeout=6000)
            current=await handle.json_value();await handle.dispose()
        if not current['is_forum']:
            return {'is_forum':False,'topics':[]}
        found={};stable=0;deadline=time.monotonic()+15
        while time.monotonic()<deadline:
            data=await self.page.evaluate(self._TOPICS_JS,{'peer':peer,'scroll':True})
            if not data:raise RuntimeError('话题面板已改变，请重新读取')
            before=len(found)
            for item in data['items']:found[item['id']]=item
            stable=stable+1 if not data['moved'] and len(found)==before else 0
            if stable>=3:break
            await asyncio.sleep(.2)
        if stable<3:raise RuntimeError('话题列表尚未加载完整，请刷新话题')
        if not found:raise RuntimeError('已识别论坛群，但未读取到话题，请刷新后选择')
        return {'is_forum':True,'topics':list(found.values())}

    _DIALOG_LIST_JS = r"""({position=0, advance=false, readyOnly=false} = {}) => {
        const visible = el => el?.checkVisibility() && el.getBoundingClientRect().width > 0;
        const main = document.querySelector('#chatlist-container') || document;
        const selected = document.querySelector('.folders-tabs-scrollable [aria-selected="true"][aria-controls]');
        const pane = selected && document.getElementById(selected.getAttribute('aria-controls'));
        const lists = [...(pane || main).querySelectorAll('ul.chatlist')].filter(list => {
            const scroller = list.closest('.scrollable-y');
            const tab = list.closest('.tabs-tab');
            return visible(list) && visible(scroller || list) && !list.closest('.topics-slider') &&
                (!tab || tab.classList.contains('active'));
        });
        const populated = lists.find(el => el.querySelector('.chatlist-chat[data-peer-id]'));
        const empty = [...(pane || main).querySelectorAll('.chatlist-parts.active[data-placeholder-type] > .empty-placeholder')].some(visible);
        if (readyOnly) return !!populated || empty;
        const list = populated || lists[0];
        if (!list) return {items: [], next: position, empty, atEnd: false};
        const scroller = list.closest('.scrollable-y');
        const bottom = scroller ? Math.max(0, Math.floor(scroller.scrollHeight - scroller.clientHeight)) : 0;
        const current = Math.min(Math.floor(position), bottom);
        if (scroller && Math.abs(scroller.scrollTop - current) > 3) {
            // Repainting the list can move the scroll bar independently of the scan.
            scroller.scrollTop = current;
            const settled = Math.abs(scroller.scrollTop - current) > 3 ? Math.floor(scroller.scrollTop) : current;
            return {items: [], next: settled, empty: false, atEnd: false};
        }
        const items = lists.filter(el => el.closest('.scrollable-y') === scroller)
            .flatMap(el => [...el.querySelectorAll('.chatlist-chat[data-peer-id]')]).map(row => ({
            pid: row.dataset.peerId,
            is_forum: row.classList.contains('is-forum') || !!row.querySelector('.is-forum'),
            title: row.querySelector('.user-title, .peer-title')?.textContent.trim() || ''
        }));
        const atEnd = current >= bottom - 2;
        const next = advance && scroller && items.length ? Math.min(bottom, current + Math.max(1, Math.floor(scroller.clientHeight * .8))) : current;
        if (scroller && next > current) scroller.scrollTop = next;
        return {items, next, empty: !items.length && empty, atEnd};
    }"""

    async def _wait_dialog_list(self, timeout=18000):
        try:
            handle = await self.page.wait_for_function(self._DIALOG_LIST_JS, arg={'readyOnly': True}, timeout=timeout)
            await handle.dispose()
        except PlaywrightTimeoutError as error:
            raise RuntimeError('Telegram 未返回会话列表。请刷新重试，或返回后输入会话 ID / @用户名。') from error

    async def list_chats(self, folder=None, on_progress=None):
        """Wait for the active chat list before reading its virtualized rows."""
        await self.ensure_login()
        await self._wait_dialog_list()
        if folder:
            if not await self._open_folder(folder):
                raise RuntimeError(f'文件夹「{folder}」不存在或无法打开')
            await self._wait_dialog_list()
        else:
            all_chats = self.page.locator('.folders-tabs-scrollable [data-filter-id="0"]:visible').first
            if await all_chats.count() and await all_chats.get_attribute('aria-selected') != 'true':
                await all_chats.click(timeout=2500)
                await all_chats.and_(self.page.locator('[aria-selected="true"]')).wait_for(timeout=3500)
                await self._wait_dialog_list()
        initial = await self.page.evaluate(self._DIALOG_LIST_JS, {})
        if initial.get('empty'):
            return []
        await asyncio.sleep(.15)
        result, seen = [], set()
        position = 0
        end_since = None
        complete = False
        loop = asyncio.get_running_loop()
        for _ in range(250):
            data = await self.page.evaluate(self._DIALOG_LIST_JS, {'position': position, 'advance': True})
            position = data['next']
            added = 0
            for it in data.get("items", []):
                pid = it.get("pid")
                if pid and pid not in seen:
                    seen.add(pid)
                    added += 1
                    result.append({"id": pid, "title": it.get("title") or "", "is_forum": bool(it.get('is_forum')),
                                   "type": "论坛群 · 含话题" if it.get('is_forum') else "私聊/机器人" if not str(pid).startswith("-")
                                   else "群组/频道"})
            if on_progress:
                try:
                    on_progress(len(result))
                except Exception:
                    pass
            if on_progress is None and added and \
                    (len(result) // 50) > ((len(result) - added) // 50):
                log.info("已读取 %d 个会话…", len(result))
            if data['atEnd'] and not added:
                if end_since is None:
                    end_since = loop.time()
                # Telegram can fetch another page asynchronously at the current bottom.
                if loop.time() - end_since >= 3:
                    complete = True
                    break
            else:
                end_since = None
            await asyncio.sleep(0.3)
        # 回到列表顶部，避免影响后续操作
        try:
            await self.page.evaluate(self._DIALOG_LIST_JS, {})
        except Exception:
            pass
        if not result or not complete:
            raise RuntimeError('Telegram 会话列表尚未加载完成，请刷新重试。')
        return result



def random_delay(lo, hi):
    import random
    return random.randint(lo, hi)


# ---------- 单命令入口 ----------

def load_cfg():
    import yaml
    path = BASE_DIR / "config.yaml"
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def find_account(cfg, name):
    for acc in cfg.get("accounts") or []:
        if acc.get("name") == name:
            return acc
    sys.exit(f"config.yaml 里找不到账号：{name}")


async def cmd_login(name):
    cfg = load_cfg()
    acc = find_account(cfg, name)
    if not await WebEngine(acc, cfg).login_interactive():
        raise SystemExit("登录未完成")


async def cmd_test(name):
    cfg = load_cfg()
    acc = find_account(cfg, name)
    eng = WebEngine(acc, cfg)
    await eng.launch()
    try:
        ok = await eng.is_logged_in()
        print("登录状态：", "✅ 有效" if ok else "❌ 失效（需要重新 login）")
        if ok:
            eng.marker().write_text("ok", encoding="utf-8")
    finally:
        await eng.close()


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    if len(sys.argv) < 3 or sys.argv[1] not in ("login", "test"):
        print(__doc__)
        sys.exit(1)
    asyncio.run({"login": cmd_login, "test": cmd_test}[sys.argv[1]](sys.argv[2]))

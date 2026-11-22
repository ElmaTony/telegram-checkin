#!/usr/bin/env python3
"""Local control panel: atomic jobs, full evidence, safe configuration and sessions."""
import asyncio
import copy
import csv
import hashlib
import io
import json
import logging
import os
import secrets
import shutil
import subprocess
import threading
import uuid
import webbrowser
from collections import Counter, deque
from datetime import datetime, timedelta
from pathlib import Path

import yaml
from flask import Flask, Response, jsonify, request

from core import BASE_DIR
from core.paths import RESOURCE_DIR, account_lock_path, login_command, visible_browser_available
from core.config import CONFIG_PATH, load_config, safe_name, save_config, validate_config
from core.history import HISTORY_DIR
from core.locks import FileLock
from core.log import LOG_PATH, setup_logging
from core.logic import (group_actions, infer_method, judge_replies, feedback_hint, recheck_eligible, _kw_hit,
                        DEFAULT_SUCCESS_KW, DEFAULT_FAIL_KW)
from core.runner import ACTIVITY, PROBE, run_probe_round, run_web_round
from core.state import load_state, save_state
from core.storage import atomic_write
from core.timeutil import now as local_now
from core import scheduler, notifications

HOST = '127.0.0.1'
GUI_HTML = RESOURCE_DIR / 'gui.html'
app = Flask(__name__, static_folder=str(RESOURCE_DIR/'static'))
app.config['MAX_CONTENT_LENGTH'] = 2 * 1024 * 1024
log = logging.getLogger('checkin')
BOT = {'loop': None, 'busy': False, 'scanning': False, 'cfg_error': None, 'task': None,
       'cancel_requested': False, 'login_process': None, 'job': None}
TRIGGER = {'event': threading.Event(), 'only': None, 'chats': None, 'mode': 'checkin'}
_lock = threading.RLock()
_test_push_lock = threading.Lock()
SCHEDULER = None
_RUNTIME_LISTEN = {'host': None, 'token': ''}


class _LoopNotReadyError(RuntimeError):
    pass


def _safe_now(cfg=None):
    try:
        return local_now(cfg if cfg is not None else read_cfg())
    except Exception:
        return local_now()


def read_cfg():
    if not CONFIG_PATH.exists():
        return {'accounts': []}
    try:
        value = yaml.safe_load(CONFIG_PATH.read_text(encoding='utf-8')) or {}
        if not isinstance(value, dict):
            raise ValueError('配置必须是对象')
        return value
    except (OSError, ValueError, yaml.YAMLError) as error:
        raise ValueError(f'无法读取配置：{error}') from error


def revision(cfg):
    return hashlib.sha256(json.dumps(cfg, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def body():
    value = request.get_json(silent=True)
    if not isinstance(value, dict):
        raise ValueError('请求正文必须是 JSON 对象')
    return value


def _busy_msg():
    return '当前任务正在运行或排队，请等待结束或取消任务'


def _refresh_login():
    process = BOT.get('login_process')
    if process is not None and process.poll() is not None:
        returncode = process.returncode
        BOT.update(busy=False, scanning=False, login_process=None)
        if BOT.get('job'):
            BOT['job'].update(status='completed' if returncode == 0 else 'failed',
                              error='' if returncode == 0 else '登录未完成，请重试扫码', ended=f'{_safe_now():%H:%M:%S}')


def _reserve(mode, scanning=False, show_browser=True):
    _refresh_login()
    if BOT['busy'] or BOT['scanning']:
        raise RuntimeError(_busy_msg())
    if mode == 'login' and not visible_browser_available():
        raise ValueError('当前服务器没有可视桌面，请在服务器桌面或 X11 转发环境中完成首次扫码登录')
    show_browser = show_browser and visible_browser_available()
    job = {'id': uuid.uuid4().hex, 'mode': mode, 'status': 'queued',
           'created': _safe_now().isoformat(timespec='seconds'), 'error': '', 'retry_map': {},
           'show_browser': show_browser}
    ACTIVITY.update(act='', accounts={})
    BOT.update(busy=True, scanning=scanning, job=job, cancel_requested=False)
    return job['id']


def _release(job_id, status='completed', error=''):
    with _lock:
        job = BOT.get('job')
        if job and job['id'] == job_id and (BOT['busy'] or job.get('status') in ('queued', 'running', 'cancelling')):
            BOT.update(busy=False, scanning=False, task=None)
            job.update(status=status, error=error, ended=f'{_safe_now():%H:%M:%S}')
            if job.get('scheduled'):
                try:
                    scheduler.complete(job_id, status, error, read_cfg())
                except (RuntimeError, OSError, ValueError):
                    log.exception('无法保存定时任务结束状态，请核对后再操作')


async def _submit_scheduled(cfg, current):
    with _lock:
        _refresh_login()
        if BOT['busy'] or BOT['scanning']:
            return False
        try:
            round_lock = FileLock(BASE_DIR / 'data' / 'locks' / 'round.lock').acquire()
        except RuntimeError:
            return False
        job_id = None
        try:
            schedule = cfg.get('schedule') if isinstance(cfg.get('schedule'), dict) else {}
            job_id = _reserve('checkin', show_browser=not schedule.get('headless', True))
            BOT['job']['scheduled'] = True
            if not scheduler.claim(cfg, job_id, current):
                BOT['job']['scheduled'] = False
                _release(job_id, 'cancelled')
                return False
            TRIGGER.update(only=None, chats=None, mode='checkin', scheduled_accounts=schedule.get('accounts', []))
            TRIGGER['event'].set()
        except BaseException:
            if job_id is not None:
                if BOT.get('job') and BOT['job'].get('id') == job_id:
                    BOT['job']['scheduled'] = False
                _release(job_id, 'failed', '定时任务未能持久化，未执行')
            raise
        finally:
            round_lock.release()
    return True


def call(coro, timeout=240):
    if not BOT['loop'] or not BOT['loop'].is_running():
        coro.close()
        raise _LoopNotReadyError('后台尚未就绪，请稍后重试')
    future = asyncio.run_coroutine_threadsafe(coro, BOT['loop'])
    try:
        return future.result(timeout=timeout)
    except TimeoutError:
        future.cancel()  # The coroutine finally owns release; never unlock a live browser early.
        raise TimeoutError('读取超时，已请求停止；浏览器清理结束前保持占用')


async def bot_loop():
    while not BOT.get('stopping'):
        while not TRIGGER['event'].is_set():
            if BOT.get('stopping'):
                return
            await asyncio.sleep(.15)
        with _lock:
            if not TRIGGER['event'].is_set():
                continue
            only, chats, mode = TRIGGER['only'], TRIGGER['chats'], TRIGGER['mode']
            job_id = BOT['job']['id']
            scheduled = bool(BOT['job'].get('scheduled'))
            scheduled_accounts = list(TRIGGER.get('scheduled_accounts') or []) if scheduled else []
            TRIGGER['event'].clear()
            BOT['job']['status'] = 'running'
        source = '每日定时签到' if scheduled else '手动签到'
        cfg = None
        round_date = None
        try:
            cfg = load_config()
            round_date = f'{local_now(cfg):%Y-%m-%d}'
            if scheduled:
                scheduler.complete(job_id, 'running', cfg=cfg)
            # Manual tasks explicitly choose a visible browser; never inherit an audit's mode.
            cfg['web_headless'] = not BOT['job']['show_browser']
            BOT['cfg_error'] = None
            operation = run_probe_round(cfg, only, chats) if mode == 'probe' else run_web_round(cfg, load_state(), only, chats, recheck=mode == 'recheck', accounts=scheduled_accounts)
            task = asyncio.create_task(operation)
            with _lock:
                BOT['task'] = task
                if BOT['cancel_requested']:
                    task.cancel()
            result = await task
            if mode != 'probe':
                with _lock:
                    BOT['job']['retry_map'] = result
            if mode == 'checkin':
                notification = await _notify_job(cfg, only=only, chats=chats, accounts=scheduled_accounts, source=source, today=round_date)
                with _lock:
                    BOT['job']['notification'] = notification
            _release(job_id)
        except asyncio.CancelledError:
            if cfg is not None and mode == 'checkin' and not BOT.get('stopping'):
                notification = await _notify_job(cfg, only=only, chats=chats, accounts=scheduled_accounts, cancelled=True, source=source, today=round_date)
                with _lock:
                    BOT['job']['notification'] = notification
            _release(job_id, 'cancelled')
        except (Exception, SystemExit) as error:
            BOT['cfg_error'] = str(error) if isinstance(error, SystemExit) else None
            log.exception('任务执行失败')
            if cfg is not None and mode == 'checkin':
                notification = await _notify_job(cfg, only=only, chats=chats, accounts=scheduled_accounts, error=str(error), source=source, today=round_date)
                with _lock:
                    BOT['job']['notification'] = notification
            _release(job_id, 'failed', str(error))


async def _notify_job(cfg, **kwargs):
    try:
        return await notifications.notify_round(cfg, load_state(), **kwargs)
    except Exception:
        return {'ok': False, 'error': '通知未完成，签到结果请在控制台核对'}


@app.errorhandler(ValueError)
def invalid_input(error):
    return jsonify(ok=False, error=str(error)), 400


@app.errorhandler(RuntimeError)
def conflict(error):
    return jsonify(ok=False, error=str(error)), 409


@app.errorhandler(TimeoutError)
def timed_out(error):
    return jsonify(ok=False, error=str(error)), 408


@app.errorhandler(OSError)
def file_error(error):
    log.exception('本地文件操作失败')
    return jsonify(ok=False, error=f'本地文件操作未完成：{error}'), 503


@app.get('/')
def index():
    return Response(GUI_HTML.read_bytes(), mimetype='text/html')


@app.get('/favicon.ico')
def favicon():
    icon_path = RESOURCE_DIR / 'static' / 'icon.svg'
    if icon_path.is_file():
        return Response(icon_path.read_bytes(), mimetype='image/svg+xml')
    return Response(status=204)


_STATUS_ACTION_KEYS = ('method', 'confirm', 'sent', 'clicked', 'buttons_complete',
                       'sent_mids', 'confirmed_by_reply', 'error', 'warning')


def _slim_actions(actions):
    return [{k: a[k] for k in _STATUS_ACTION_KEYS if k in a}
            for a in (actions or []) if isinstance(a, dict)]


@app.get('/api/status')
def api_status():
    from web_engine import WebEngine
    with _lock:
        _refresh_login()
    cfg, state = read_cfg(), load_state()
    today = f'{local_now(cfg):%Y-%m-%d}'
    accounts = []
    for account in cfg.get('accounts', []):
        groups = []
        chat_cache = state.get('_chat_cache', {}).get(account['name'], {})
        forum_ids = {str(item['id']) for dialogs in chat_cache.get('lists', {}).values() for item in dialogs if item.get('is_forum')}
        forum_ids.update(chat for chat, item in chat_cache.get('topics', {}).items() if item.get('is_forum'))
        for group in account.get('groups', []):
            chat = str(group['chat'])
            latest = state.get('_last', {}).get(account['name'], {}).get(chat, {})
            if latest.get('date') != today:
                latest = {}
            done = state.get(account['name'], {}).get(chat) == today
            actions = group_actions(group)
            candidates=[item for item in latest.get('evidence',[]) if item.get('source')=='unrelated'
                        and any(word in item.get('t','') for word in ('签到','打卡','积分'))
                        and judge_replies({},cfg,[item.get('t','')])!='unconfirmed']
            groups.append({**latest, 'actions': _slim_actions(latest.get('actions')), 'chat': chat,
                           'candidate_replies': candidates, 'feedback_hint': feedback_hint(group, cfg, latest),
                           'name': group.get('title') or state.get('_titles', {}).get(chat) or chat,
                           'method': infer_method(actions[0]), 'methods': [infer_method(action) for action in actions], 'action_count': len(actions),
                           'share_group': group.get('share_group', ''), 'topic': group.get('topic', ''),
                           'topic_id': group.get('topic_id'), 'is_forum': bool(group.get('is_forum') or group.get('topic') or chat in forum_ids),
                           'source': latest.get('source') or ('legacy' if latest or done else 'unknown'),
                           'done': done, 'confirm': latest.get('confirm') or ('success' if done else None),
                           'can_recheck': recheck_eligible(latest, today)})
        accounts.append({'name': account['name'], 'status': '上次已登录' if WebEngine(account, cfg).marker().exists() else '待扫码',
                         'done': sum(g['confirm'] == 'success' for g in groups), 'total': len(groups), 'groups': groups})
    with _lock:
        _refresh_login()
        busy = BOT['busy']
        job_snapshot = copy.deepcopy(BOT['job'])
        cfg_error = BOT['cfg_error']
        activity_act = ACTIVITY.get('act', '')
        progress_snapshot = copy.deepcopy(ACTIVITY.get('accounts', {}))
        probe_snapshot = copy.deepcopy(PROBE)
    return jsonify(accounts=accounts, today=today, busy=busy, activity=activity_act,
                   browser={'visible_available': visible_browser_available()}, server_time=local_now(cfg).timestamp(),
                   progress=progress_snapshot, job=job_snapshot, cfg_error=cfg_error, probe=probe_snapshot,
                   automation={**scheduler.describe(cfg), 'active': bool(SCHEDULER and SCHEDULER.active),
                               'service_error': SCHEDULER.error if SCHEDULER else ''}, notification=notifications.last_status())


@app.get('/api/automation')
def api_automation_get():
    cfg = read_cfg()
    return jsonify(ok=True, **scheduler.describe(cfg), notifications=cfg.get('notifications', {}),
                   key=notifications.key_status(), notification=notifications.last_status(), revision=revision(cfg),
                   active=bool(SCHEDULER and SCHEDULER.active), service_error=SCHEDULER.error if SCHEDULER else '')


@app.post('/api/automation')
def api_automation_save():
    data = body()
    if 'sendkey' in data and not isinstance(data['sendkey'], str):
        raise ValueError('SendKey 必须是字符串')
    key = data.get('sendkey', '').strip()
    if key:
        notifications.validate_key(key)
    if 'clear_sendkey' in data and not isinstance(data['clear_sendkey'], bool):
        raise ValueError('clear_sendkey 必须是布尔值')
    if key and data.get('clear_sendkey'):
        raise ValueError('不能同时替换和删除 SendKey')
    with _lock, FileLock(BASE_DIR / 'data' / 'locks' / 'round.lock'):
        _refresh_login()
        if BOT['busy']:
            raise RuntimeError(_busy_msg())
        cfg = read_cfg()
        _verify_revision(data, cfg)
        cfg.update(schedule=data.get('schedule', cfg.get('schedule', {})), notifications=data.get('notifications', cfg.get('notifications', {})))
        error = validate_config(cfg)
        if error:
            raise ValueError(error)
        key_change = bool(key or data.get('clear_sendkey'))
        previous_secret_raw = notifications.SECRETS_PATH.read_text(encoding='utf-8') if key_change and notifications.SECRETS_PATH.exists() else None
        try:
            if key_change:
                notifications.save_key(key)
            if (cfg.get('notifications') or {}).get('enabled') and not notifications.key_status()['configured']:
                raise ValueError('启用微信推送前请先保存有效 SendKey')
            save_config(cfg)
        except BaseException:
            if key_change:
                if previous_secret_raw is not None:
                    atomic_write(notifications.SECRETS_PATH, previous_secret_raw)
                elif notifications.SECRETS_PATH.exists():
                    try:
                        notifications.SECRETS_PATH.unlink()
                    except OSError:
                        pass
            raise
    return api_automation_get()


@app.post('/api/notifications/test')
def api_notification_test():
    body()
    # Limit accidental double-clicks; test and production delivery each use one request.
    if not _test_push_lock.acquire(blocking=False):
        raise RuntimeError('测试通知正在发送，请等待结果')
    try:
        previous = notifications.last_status()
        if previous.get('kind') == 'test' and previous.get('time'):
            try:
                if (local_now(read_cfg()) - datetime.fromisoformat(previous['time'])).total_seconds() < 30:
                    raise RuntimeError('请等待 30 秒再测试，避免重复占用推送额度')
            except ValueError:
                pass
        result = notifications.send_preview(read_cfg(), load_state())
    finally:
        _test_push_lock.release()
    return jsonify(**result), 200 if result['ok'] else 502


@app.get('/api/config')
def api_config_get():
    cfg = read_cfg()
    rev = revision(cfg)
    public_cfg = {k: v for k, v in cfg.items() if k != 'gui_token'}
    return jsonify(**public_cfg, _revision=rev)


def _verify_revision(data, current):
    if data.get('revision') and data['revision'] != revision(current):
        raise RuntimeError('配置已被其他窗口修改，请重新载入并合并你的草稿')


@app.post('/api/config')
def api_config_save():
    data = body()
    cfg = data.get('cfg', data)
    if not isinstance(cfg, dict):
        raise ValueError('配置必须是对象')
    cfg = copy.deepcopy(cfg)
    cfg.pop('_revision', None)
    renames = data.get('renames', {})
    if not isinstance(renames, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in renames.items()):
        raise ValueError('renames 必须是账号名映射')
    active_renames = {old: new for old, new in renames.items() if old != new}
    if active_renames and isinstance(cfg.get('schedule'), dict) and isinstance(cfg['schedule'].get('accounts'), list):
        cfg['schedule']['accounts'] = [active_renames.get(name, name) for name in cfg['schedule']['accounts']]
    from web_engine import PROFILES_DIR
    with _lock, FileLock(BASE_DIR / 'data' / 'locks' / 'round.lock'):
        _refresh_login()
        if BOT['busy']:
            raise RuntimeError(_busy_msg())
        current = read_cfg()
        _verify_revision(data, current)
        if 'gui_token' not in cfg and 'gui_token' in current:
            cfg['gui_token'] = current['gui_token']
        listen_host = _RUNTIME_LISTEN.get('host')
        if (listen_host and listen_host not in ('127.0.0.1', 'localhost', '::1')
                and not os.environ.get('CHECKIN_GUI_TOKEN', '').strip()
                and not str(cfg.get('gui_token') or '').strip()):
            raise ValueError('监听非本机地址时不能清空 gui_token')
        error = validate_config(cfg)
        if error:
            raise ValueError(error)
        old_names = {a['name'] for a in current.get('accounts', [])}
        new_names = {a['name'] for a in cfg.get('accounts', [])}
        for old, new in active_renames.items():
            if old not in old_names or new not in new_names:
                raise ValueError('改名映射中的账号不存在')
        state = load_state()
        locks, staged_moves, final_moves = [], [], []
        staging_dir = None
        original_state = copy.deepcopy(state)
        original_text = CONFIG_PATH.read_text(encoding='utf-8') if CONFIG_PATH.exists() else None
        config_written = False
        try:
            if active_renames:
                lock_names = sorted({safe_name(name).casefold() for pair in active_renames.items() for name in pair})
                for key in lock_names:
                    locks.append(FileLock(account_lock_path(PROFILES_DIR.parent.parent, key)).acquire())
                moving_sources = {safe_name(old).casefold() for old in active_renames}
                pairs = []
                for old, new in active_renames.items():
                    old_profile, new_profile = safe_name(old), safe_name(new)
                    src, dst = PROFILES_DIR / old_profile, PROFILES_DIR / new_profile
                    if dst.exists() and new_profile.casefold() not in moving_sources and src.resolve() != dst.resolve():
                        raise RuntimeError('新账号名对应的登录会话已存在，请更换名称')
                    pairs.append((old_profile, new_profile))
                staging_root = PROFILES_DIR.parent / 'session-rename'
                staging_dir = staging_root / uuid.uuid4().hex
                staging_dir.mkdir(parents=True, exist_ok=True)
                for idx, (old_profile, new_profile) in enumerate(pairs):
                    for suffix in ('', '.loggedin'):
                        source = PROFILES_DIR / (old_profile + suffix)
                        target = PROFILES_DIR / (new_profile + suffix)
                        if source.exists() and source != target:
                            temp = staging_dir / f'{idx}{suffix or ".dir"}'
                            source.rename(temp)
                            staged_moves.append((source, temp, target))
                for source, temp, target in staged_moves:
                    temp.rename(target)
                    final_moves.append((source, target))
                popped_top = {old: state.pop(old) for old in active_renames if old in state}
                for old, val in popped_top.items():
                    state[active_renames[old]] = val
                for key in ('_last', '_chat_cache'):
                    bucket = state.get(key)
                    if isinstance(bucket, dict):
                        popped = {old: bucket.pop(old) for old in active_renames if old in bucket}
                        for old, val in popped.items():
                            bucket[active_renames[old]] = val
            save_config(cfg)
            config_written = True
            if active_renames:
                save_state(state)
        except BaseException:
            if final_moves:
                rollback_dir = PROFILES_DIR.parent / 'session-rename' / ('rb-' + uuid.uuid4().hex)
                rollback_dir.mkdir(parents=True, exist_ok=True)
                rb_temp = []
                for idx, (source, target) in enumerate(reversed(final_moves)):
                    if target.exists():
                        tmp = rollback_dir / str(idx)
                        target.rename(tmp)
                        rb_temp.append((source, tmp))
                for source, tmp in rb_temp:
                    tmp.rename(source)
                shutil.rmtree(rollback_dir, ignore_errors=True)
            else:
                for source, temp, _ in reversed(staged_moves):
                    if temp.exists():
                        temp.rename(source)
            if config_written and original_text is not None:
                atomic_write(CONFIG_PATH, original_text)
                save_state(original_state)
            raise
        finally:
            if staging_dir and staging_dir.exists():
                shutil.rmtree(staging_dir, ignore_errors=True)
            for lock in locks:
                lock.release()
    return jsonify(ok=True, warnings=[], revision=revision(cfg))


def _queue(mode):
    data = body()
    show_browser = data.get('show_browser', visible_browser_available())
    if not isinstance(show_browser, bool):
        raise ValueError('show_browser 必须是布尔值')
    if data.get('account') is not None and not isinstance(data['account'], str):
        raise ValueError('account 必须是账号名字符串')
    if isinstance(data.get('account'), str) and not data['account'].strip():
        raise ValueError('指定账号不能为空；执行全部账号时请省略 account')
    account = data.get('account') or None
    chats = data.get('chats')
    if chats is not None and (not isinstance(chats, list) or not chats or len(chats)>1000 or any(not isinstance(c, str) for c in chats)):
        raise ValueError('chats 必须是非空字符串数组，最多 1000 项')
    with _lock:
        cfg = read_cfg()
        error = validate_config(cfg)
        if error:
            raise ValueError(error)
        if account is not None and account not in {a['name'] for a in cfg.get('accounts', [])}:
            raise ValueError('找不到指定账号')
        chosen = [a for a in cfg.get('accounts', []) if not account or a['name'] == account]
        if not any(a.get('groups') for a in chosen):
            raise ValueError('没有可执行的签到会话，请先添加账号和群')
        if chats and not set(chats).issubset({str(g['chat']) for a in chosen for g in a.get('groups', [])}):
            raise ValueError('指定会话不在当前账号配置中')
        if mode == 'recheck' and (not account or not chats):
            raise ValueError('补查必须指定账号与会话')
        if mode == 'recheck':
            latest = load_state().get('_last', {}).get(account, {})
            if any(not recheck_eligible(latest.get(chat, {}), f'{local_now(cfg):%Y-%m-%d}') for chat in chats):
                raise ValueError('只能补查今天且保留发送消息 ID 或可靠发送边界的结果；旧记录请先在 Telegram 核对')
        job_id = _reserve(mode, show_browser=show_browser)
        TRIGGER.update(only=account, chats=chats, mode=mode, scheduled_accounts=[])
        TRIGGER['event'].set()
    return jsonify(ok=True, job_id=job_id), 202


@app.post('/api/checkin_now')
def api_checkin_now():
    return _queue('checkin')


@app.post('/api/recheck')
def api_recheck():
    return _queue('recheck')


@app.post('/api/probe')
def api_probe():
    return _queue('probe')


@app.post('/api/cancel')
def api_cancel():
    body()
    with _lock:
        if BOT.get('login_process'):
            BOT['login_process'].terminate()
            return jsonify(ok=True)
        if not BOT['busy']:
            return jsonify(ok=True)
        BOT['cancel_requested'] = True
        BOT['job']['status'] = 'cancelling'
        if BOT['task']:
            BOT['loop'].call_soon_threadsafe(BOT['task'].cancel)
        elif TRIGGER['event'].is_set():
            TRIGGER['event'].clear()
            _release(BOT['job']['id'], 'cancelled')
    return jsonify(ok=True)


@app.post('/api/web_login')
def api_web_login():
    data = body()
    account = data.get('account')
    with _lock:
        if not isinstance(account, str) or account not in {a['name'] for a in read_cfg().get('accounts', [])}:
            raise ValueError('找不到指定账号，请先保存配置')
        job_id = _reserve('login')
        try:
            process = subprocess.Popen(login_command(account),
                                       cwd=BASE_DIR, creationflags=0x08000000 if os.name == 'nt' else 0)
            BOT.update(login_process=process)
            BOT['job'].update(status='running', account=account)
        except Exception:
            _release(job_id, 'failed', '登录浏览器启动失败')
            raise
    return jsonify(ok=True, job_id=job_id), 202


def _scan(account_name, folder='', refresh=False, folders_only=False, topic_chat=None, title=''):
    cfg = read_cfg()
    account = next((a for a in cfg.get('accounts', []) if a['name'] == account_name), None)
    if not account:
        raise ValueError('找不到指定账号')
    if topic_chat is not None and (not isinstance(topic_chat,str) or not topic_chat.lstrip('-').isdigit()):
        raise ValueError('请先选择数字会话 ID')
    cache = load_state().get('_chat_cache', {}).get(account_name, {})
    topic_cache = cache.get('topics', {}).get(topic_chat, {})
    # Windows clocks can round datetime.now() slightly behind time.time().
    # Accept subsecond skew without treating a freshly written cache as stale.
    if topic_chat is not None and not refresh and topic_cache and -1 <= local_now().timestamp()-topic_cache.get('checked_at', 0) < 300:
        return {'ok':True, **topic_cache, 'cached':True}
    cached_dialogs = cache.get('lists', {}).get(folder)
    if topic_chat is None and not refresh and not folders_only and cached_dialogs and all('is_forum' in item for item in cached_dialogs):
        return {'ok': True, 'dialogs': cached_dialogs, 'folders': cache.get('folders', []), 'cached': True, 'updated': cache.get('updated')}
    with _lock:
        job_id = _reserve('scan', scanning=True, show_browser=False)
    async def scan():
        from web_engine import WebEngine
        engine = WebEngine(account, {**cfg, 'web_headless': True})
        failed = None
        round_lock = None
        try:
            with _lock:
                BOT['task'] = asyncio.current_task()
                if BOT['cancel_requested']:
                    raise asyncio.CancelledError()
                BOT['job']['status'] = 'running'
            round_lock = FileLock(BASE_DIR / 'data' / 'locks' / 'round.lock').acquire()
            await engine.launch()
            if topic_chat is not None:
                result = await engine.list_topics(topic_chat, title, folder or None)
                state = load_state()
                saved = {**result, 'chat':topic_chat, 'checked_at':local_now().timestamp()}
                cache = state.setdefault('_chat_cache', {}).setdefault(account_name, {})
                cache.setdefault('topics', {})[topic_chat] = saved
                for dialogs in cache.get('lists', {}).values():
                    for dialog in dialogs:
                        if str(dialog.get('id')) == topic_chat:
                            dialog['is_forum'] = result['is_forum']
                            if result['is_forum']:dialog['type'] = '论坛群 · 含话题'
                save_state(state)
                return {'ok':True, **saved, 'cached':False}
            if folders_only:
                return {'ok': True, 'folders': await engine.list_folders()}
            dialogs = await engine.list_chats(folder=folder or None)
            folders = await engine.list_folders()
            if not dialogs:
                return {'ok': True, 'dialogs': [], 'folders': folders, 'cached': False}
            state = load_state()  # Do not overwrite with the snapshot from before scanning.
            cache = state.setdefault('_chat_cache', {}).setdefault(account_name, {})
            cache.setdefault('lists', {})[folder] = dialogs
            cache.update(folders=folders, updated=f'{local_now(read_cfg()):%Y-%m-%d %H:%M}')
            for dialog in dialogs:
                if dialog.get('id') and dialog.get('title'):
                    state.setdefault('_titles', {})[str(dialog['id'])] = dialog['title']
            save_state(state)
            return {'ok': True, 'dialogs': dialogs, 'folders': folders, 'cached': False, 'updated': cache['updated']}
        except BaseException as error:
            failed = error
            raise
        finally:
            try:
                await engine.close()
            finally:
                if round_lock:
                    round_lock.release()
                _release(job_id, 'failed' if failed else 'completed', str(failed or ''))
    try:
        return call(scan(), timeout=300)
    except _LoopNotReadyError:
        _release(job_id, 'failed', '后台尚未就绪')
        raise


@app.get('/api/dialogs')
def api_dialogs():
    return jsonify(_scan(request.args.get('account'), request.args.get('folder', ''), request.args.get('refresh') == '1'))


@app.get('/api/folders')
def api_folders():
    return jsonify(_scan(request.args.get('account'), folders_only=True))


@app.get('/api/topics')
def api_topics():
    return jsonify(_scan(request.args.get('account'), folder=request.args.get('folder', ''),
                         refresh=request.args.get('refresh') == '1', topic_chat=request.args.get('chat', ''),
                         title=request.args.get('title', '')[:512]))


def _session_paths(name):
    from web_engine import PROFILES_DIR
    root = PROFILES_DIR.resolve()
    profile = (root / safe_name(name)).resolve()
    marker = (root / (safe_name(name) + '.loggedin')).resolve()
    if profile.parent != root or marker.parent != root or profile == root:
        raise ValueError('会话路径越界，已拒绝删除')
    return profile, marker


def _delete_session(name):
    from web_engine import PROFILES_DIR
    profile, marker = _session_paths(name)
    with FileLock(account_lock_path(PROFILES_DIR.parent.parent, safe_name(name))):
        if profile.exists():
            shutil.rmtree(profile)
        marker.unlink(missing_ok=True)


@app.post('/api/delete_session')
def api_delete_session():
    data = body()
    name = data.get('account')
    with _lock, FileLock(BASE_DIR / 'data' / 'locks' / 'round.lock'):
        _refresh_login()
        if BOT['busy']:
            raise RuntimeError(_busy_msg())
        if not isinstance(name, str) or name not in {a['name'] for a in read_cfg().get('accounts', [])}:
            raise ValueError('找不到指定账号，未删除任何会话')
        _delete_session(name)
    return jsonify(ok=True)


@app.post('/api/delete_account')
def api_delete_account():
    data = body()
    name = data.get('account')
    with _lock, FileLock(BASE_DIR / 'data' / 'locks' / 'round.lock'):
        _refresh_login()
        if BOT['busy']:
            raise RuntimeError(_busy_msg())
        cfg = read_cfg()
        _verify_revision(data, cfg)
        if not isinstance(name, str) or name not in {a['name'] for a in cfg.get('accounts', [])}:
            raise ValueError('找不到指定账号')
        from web_engine import PROFILES_DIR
        profile, marker = _session_paths(name)
        # Stage the explicit deletion on the same volume. A failed config write can roll it back.
        trash_root = (PROFILES_DIR.parent / 'session-trash').resolve()
        trash = trash_root / uuid.uuid4().hex
        if trash.parent != trash_root:
            raise ValueError('删除暂存路径无效')
        staged = []
        original_text = CONFIG_PATH.read_text(encoding='utf-8') if CONFIG_PATH.exists() else None
        state = load_state()
        original_state = copy.deepcopy(state)
        state.pop(name, None)
        for key in ('_last', '_chat_cache'):
            if isinstance(state.get(key), dict):
                state[key].pop(name, None)
        warnings = []
        with FileLock(account_lock_path(PROFILES_DIR.parent.parent, safe_name(name))):
            trash.mkdir(parents=True)
            committed = False
            config_written = False
            try:
                cfg['accounts'] = [a for a in cfg['accounts'] if a['name'] != name]
                if isinstance(cfg.get('schedule'), dict) and isinstance(cfg['schedule'].get('accounts'), list):
                    cfg['schedule']['accounts'] = [acc for acc in cfg['schedule']['accounts'] if acc != name]
                    if cfg['schedule'].get('enabled') and not any(
                        a.get('groups') for a in cfg['accounts']
                        if not cfg['schedule']['accounts'] or a['name'] in cfg['schedule']['accounts']
                    ):
                        cfg['schedule']['enabled'] = False
                        warnings.append('已自动关闭定时计划（删除账号后已无定时签到会话）')
                error = validate_config(cfg)
                if error:
                    raise ValueError(error)
                for source in (profile, marker):
                    if source.exists():
                        target = trash / source.name
                        source.rename(target)
                        staged.append((source, target))
                save_config(cfg)
                config_written = True
                save_state(state)
                committed = True
            except BaseException:
                for source, target in reversed(staged):
                    if target.exists():
                        target.rename(source)
                if config_written and original_text is not None:
                    atomic_write(CONFIG_PATH, original_text)
                    save_state(original_state)
                raise
            finally:
                # trash is an exact generated child of the verified task-owned trash root.
                if committed and trash.exists():
                    try:
                        shutil.rmtree(trash)
                    except OSError:
                        warnings.append(f'账号配置已移除；会话暂存目录仍被占用，关闭相关浏览器后清理：{trash}')
                elif trash.exists() and not any(trash.iterdir()):
                    trash.rmdir()
    return jsonify(ok=True, revision=revision(cfg), warnings=warnings)


@app.post('/api/judge')
def api_judge():
    data = body()
    text, group = data.get('text'), data.get('group', {})
    if not isinstance(text, str) or not isinstance(group, dict):
        raise ValueError('请输入反馈文本和群规则')
    for key in ('success_keywords', 'fail_keywords'):
        if key in group and (not isinstance(group[key], list) or any(not isinstance(w, str) for w in group[key])):
            raise ValueError('判定词必须是字符串数组')
    cfg = read_cfg()
    settings = data.get('settings', {})
    if not isinstance(settings, dict):
        raise ValueError('测试设置必须是对象')
    for key in ('success_keywords', 'fail_keywords'):
        if key in settings:
            if not isinstance(settings[key], list) or any(not isinstance(word, str) for word in settings[key]):
                raise ValueError('判定词必须是字符串数组')
            cfg[key] = settings[key]
    confirm = judge_replies(group, cfg, [text], [text])
    keys = ('success_keywords', 'fail_keywords')
    defaults = {'success_keywords': DEFAULT_SUCCESS_KW, 'fail_keywords': DEFAULT_FAIL_KW}
    hits = {key: [word for word in (group.get(key) or cfg.get(key) or defaults[key]) if _kw_hit(text, word)] for key in keys}
    return jsonify(ok=True, confirm=confirm, hits=hits, note='仅测试话术匹配，不验证消息归属，也不发送消息')


def _history_rows():
    rows = []
    for path in sorted(HISTORY_DIR.glob('*.csv'), reverse=True):
        with path.open(encoding='utf-8-sig', newline='') as stream:
            reader = csv.reader(stream)
            next(reader, None)
            for row in reader:
                if len(row) < 5:
                    continue
                item = dict(zip(('time', 'account', 'group', 'status', 'detail'), row[:5]))
                if request.args.get('account') and item['account'] != request.args['account']:
                    continue
                if request.args.get('status') and item['status'] != request.args['status']:
                    continue
                if request.args.get('q', '').lower() not in ' '.join(row).lower():
                    continue
                if request.args.get('from') and item['time'][:10] < request.args['from']:
                    continue
                if request.args.get('to') and item['time'][:10] > request.args['to']:
                    continue
                rows.append(item)
    return sorted(rows, key=lambda row: row['time'], reverse=True)


def _parse_int_arg(name, default, lo, hi):
    raw = request.args.get(name)
    if raw is None or raw == '':
        return default
    try:
        value = int(raw)
    except ValueError as error:
        raise ValueError(f'参数 {name} 必须是整数') from error
    return max(lo, min(hi, value))


@app.get('/api/history')
def api_history():
    page = _parse_int_arg('page', 1, 1, 100000)
    limit = _parse_int_arg('limit', 100, 1, 1000)
    rows = _history_rows()
    counts = Counter(row['status'] for row in rows)
    return jsonify(ok=True, rows=rows[(page-1)*limit:page*limit], total=len(rows), counts=counts, page=page, limit=limit)


@app.get('/api/history/download')
def api_history_download():
    stream = io.StringIO()
    writer = csv.writer(stream)
    writer.writerow(['时间', '账号', '群聊', '状态', '详情'])
    for row in _history_rows():
        writer.writerow([
            ("'" + value) if (value.lstrip().startswith(('=', '+', '-', '@')) or value.startswith(('\t', '\r'))) else value
            for value in row.values()
        ])
    return Response('\ufeff' + stream.getvalue(), mimetype='text/csv', headers={'Content-Disposition': 'attachment; filename=checkin-history.csv'})


@app.get('/api/stats')
def api_stats():
    days = _parse_int_arg('days', 30, 1, 365)
    now_dt = local_now(read_cfg())
    today_str = f'{now_dt:%Y-%m-%d}'
    since = f'{now_dt - timedelta(days=days-1):%Y-%m-%d}'
    keys = ('成功', '未确认', '失败')
    total, by_day, by_account = dict.fromkeys(keys, 0), {}, {}
    for row in _history_rows():
        if not since <= row['time'][:10] <= today_str:
            continue
        day, account, status = row['time'][:10], row['account'], row['status']
        if status not in keys:
            status = '失败'
        by_day.setdefault(day, dict.fromkeys(keys, 0))[status] += 1
        by_account.setdefault(account, dict.fromkeys(keys, 0))[status] += 1
        total[status] += 1
    return jsonify(ok=True, days=days, total=total, per_day=[{'date': key, **value} for key, value in sorted(by_day.items())], per_acc=[{'account': key, **value} for key, value in by_account.items()], basis='执行记录（含手动重签）')


@app.get('/api/logs')
def api_logs():
    level, query = request.args.get('level', ''), request.args.get('q', '').lower()
    if level not in ('', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'):
        raise ValueError('日志级别无效')
    lines = deque(maxlen=500)
    if LOG_PATH.exists():
        with LOG_PATH.open(encoding='utf-8', errors='replace') as stream:
            for line in stream:
                if (not level or f'[{level}]' in line) and query in line.lower():
                    lines.append(line)
    return jsonify(ok=True, text=''.join(lines))


@app.get('/api/logs/download')
def api_logs_download():
    return Response(LOG_PATH.read_bytes() if LOG_PATH.exists() else b'', mimetype='text/plain', headers={'Content-Disposition': 'attachment; filename=checkin.log'})


def _gui_listen():
    cfg = read_cfg()
    return str(os.environ.get('CHECKIN_GUI_HOST') or cfg.get('gui_host') or HOST), int(os.environ.get('CHECKIN_GUI_PORT') or cfg.get('gui_port') or 8577), str(os.environ.get('CHECKIN_GUI_TOKEN') or cfg.get('gui_token') or '').strip()


def _current_token():
    env_token = os.environ.get('CHECKIN_GUI_TOKEN', '').strip()
    if env_token:
        return env_token
    try:
        cfg_token = str(read_cfg().get('gui_token') or '').strip()
        if cfg_token:
            _RUNTIME_LISTEN['token'] = cfg_token
        return cfg_token
    except ValueError:
        return _RUNTIME_LISTEN.get('token', '')


@app.before_request
def _require_token():
    if not request.path.startswith('/api/'):
        return None
    token = _current_token()
    if token and not secrets.compare_digest(request.headers.get('X-Token', ''), token):
        return jsonify(ok=False, error='需要访问令牌'), 401


@app.after_request
def _security_headers(response):
    response.headers.setdefault('X-Content-Type-Options', 'nosniff')
    response.headers.setdefault('X-Frame-Options', 'DENY')
    response.headers.setdefault('Referrer-Policy', 'no-referrer')
    if request.path.startswith('/api/') or request.path == '/':
        response.headers['Cache-Control'] = 'no-store'
    return response


def run():
    global SCHEDULER
    setup_logging()
    logging.getLogger('werkzeug').setLevel(logging.ERROR)
    host, port, token = _gui_listen()
    if host not in ('127.0.0.1', 'localhost', '::1') and not token:
        raise ValueError('监听非本机地址前必须设置 CHECKIN_GUI_TOKEN 或 gui_token')
    _RUNTIME_LISTEN.update(host=host, token=token)
    BOT['loop'] = asyncio.new_event_loop()
    BOT['stopping'] = False
    SCHEDULER = scheduler.DailyScheduler(read_cfg, _submit_scheduled)
    workers = []
    def background():
        asyncio.set_event_loop(BOT['loop'])
        workers.extend([BOT['loop'].create_task(SCHEDULER.run()), BOT['loop'].create_task(bot_loop())])
        BOT['loop'].run_forever()
    threading.Thread(target=background, daemon=True).start()
    url = f'http://{host}:{port}'
    log.info('控制台地址：%s', url)
    if os.environ.get('CHECKIN_NO_BROWSER') != '1':
        threading.Timer(1, lambda: webbrowser.open(url)).start()
    from waitress import serve
    try:
        serve(app, host=host, port=port, threads=8)
    finally:
        with _lock:
            proc = BOT.get('login_process')
            if proc is not None and proc.poll() is None:
                try:
                    proc.terminate()
                    proc.wait(timeout=5)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                BOT['login_process'] = None
        async def shutdown():
            BOT['stopping'] = True
            if BOT.get('task') and not BOT['task'].done():
                BOT['task'].cancel()
            if TRIGGER['event'].is_set() and BOT.get('job'):
                TRIGGER['event'].clear()
                _release(BOT['job']['id'], 'cancelled')
            if workers:
                workers[0].cancel()
            await asyncio.gather(*workers, return_exceptions=True)
        try:
            asyncio.run_coroutine_threadsafe(shutdown(), BOT['loop']).result(timeout=60)
        finally:
            BOT['loop'].call_soon_threadsafe(BOT['loop'].stop)


if __name__ == '__main__':
    run()

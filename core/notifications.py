"""ServerChan: one bounded request per round; credentials never enter public config."""
import asyncio
import html
import http.client
import json
import logging
import os
import re
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

from . import BASE_DIR
from .storage import atomic_write
from .logic import judge_replies
from .timeutil import now

SECRETS_PATH = BASE_DIR / 'data' / 'secrets.json'
STATUS_PATH = BASE_DIR / 'data' / 'notification-status.json'
log = logging.getLogger('checkin')


def validate_key(key):
    if not isinstance(key, str) or not re.fullmatch(r'(?:SCT[A-Za-z0-9]{10,190}|sctp\d+t[A-Za-z0-9]{10,190})', key):
        raise ValueError('SendKey 格式无效，请从 Server酱复制 SCT 或 sctp 开头的完整密钥')
    return key


def saved_key():
    if not SECRETS_PATH.exists():
        return ''
    try:
        data = json.loads(SECRETS_PATH.read_text(encoding='utf-8'))
        return data.get('serverchan_sendkey', '')
    except (OSError, ValueError, AttributeError) as error:
        raise ValueError('通知私密配置无法读取，请重新保存 SendKey') from error


def get_key():
    return os.environ.get('SERVERCHAN_SENDKEY', '').strip() or saved_key()


def save_key(key):
    if key:
        validate_key(key)
    atomic_write(SECRETS_PATH, json.dumps({'serverchan_sendkey': key}))
    if os.name != 'nt':
        SECRETS_PATH.chmod(0o600)


def key_status():
    try:
        key = get_key()
        if key:
            validate_key(key)
        return {'configured': bool(key), 'source': 'environment' if os.environ.get('SERVERCHAN_SENDKEY', '').strip() else 'local' if key else '', 'error': ''}
    except ValueError as error:
        return {'configured': False, 'source': '', 'error': str(error)}


def last_status():
    try:
        value = json.loads(STATUS_PATH.read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _truncate_utf8(text, max_bytes=28000):
    encoded = str(text or '').encode('utf-8')
    if len(encoded) <= max_bytes:
        return str(text or '')
    return encoded[:max_bytes].decode('utf-8', errors='ignore')


def _error_kind(error):
    cause = getattr(error, 'reason', error)
    if isinstance(error, json.JSONDecodeError):
        return '响应格式无效'
    if isinstance(error, http.client.HTTPException):
        return f'HTTP 传输异常（{type(error).__name__}）'
    if isinstance(cause, (TimeoutError, socket.timeout)):
        return '连接超时'
    if isinstance(cause, ssl.SSLError):
        return 'TLS 握手或证书异常'
    if isinstance(cause, socket.gaierror):
        return '域名解析失败'
    if isinstance(cause, ConnectionRefusedError):
        return '连接被拒绝（请检查本机代理）'
    if isinstance(cause, ConnectionResetError):
        return '连接被重置'
    return type(cause).__name__


def _is_pre_http_handshake_error(error, *, has_http_proxy=False):
    cause = getattr(error, 'reason', error)
    if isinstance(cause, ssl.SSLError):
        return True
    if 'tunnel connection failed' in str(cause).lower():
        return True
    if isinstance(cause, (TimeoutError, socket.timeout)) and 'handshake' in str(cause).lower():
        return True
    if has_http_proxy and isinstance(error, urllib.error.URLError) and isinstance(
        cause, (TimeoutError, socket.timeout, ConnectionRefusedError, ConnectionResetError, socket.gaierror)
    ):
        return True
    return False


def _post(key, title, description, *, short=''):
    validate_key(key)
    match = re.fullmatch(r'sctp(\d+)t[A-Za-z0-9]+', key)
    url = f'https://{match[1]}.push.ft07.com/send/{key}.send' if match else f'https://sctapi.ftqq.com/{key}.send'
    clean_title = re.sub(r'[\r\n]+', ' ', str(title or ''))[:32]
    fields = {'title': clean_title, 'desp': _truncate_utf8(description, 28000), 'noip': '1'}
    if short:
        fields['short'] = re.sub(r'[\r\n]+', ' ', str(short))[:64]
    payload = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(url, data=payload, headers={'Content-Type': 'application/x-www-form-urlencoded', 'User-Agent': 'telegram-checkin'})
    proxies = urllib.request.getproxies()
    has_http_proxy = any(str(proxies.get(k, '')).lower().startswith(('http://', 'https://')) for k in ('https', 'http'))
    has_socks_only = any(str(v).lower().startswith('socks') for v in proxies.values()) and not has_http_proxy
    try:
        if has_socks_only:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            try:
                cm = opener.open(req, timeout=10)
            except (ssl.SSLError, TimeoutError, socket.timeout, urllib.error.URLError) as err:
                if _is_pre_http_handshake_error(err):
                    time.sleep(0.4)
                    cm = opener.open(req, timeout=10)
                else:
                    raise
        else:
            try:
                cm = urllib.request.urlopen(req, timeout=10)
            except (ssl.SSLError, TimeoutError, socket.timeout, urllib.error.URLError) as err:
                if _is_pre_http_handshake_error(err, has_http_proxy=has_http_proxy):
                    if has_http_proxy:
                        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                        cm = opener.open(req, timeout=10)
                    else:
                        time.sleep(0.4)
                        cm = urllib.request.urlopen(req, timeout=10)
                else:
                    raise
        with cm as response:
            data = json.loads(response.read(65536))
        if not isinstance(data, dict) or type(data.get('code')) is not int or data['code'] != 0:
            code = data.get('code') if isinstance(data, dict) else None
            return {'ok': False, 'error': f'Server酱未接受推送（code={code if isinstance(code, int) else "未知"}），请检查通道及额度'}
        return {'ok': True, 'error': '', 'message': 'Server酱已接受，微信送达请以接收端为准'}
    except urllib.error.HTTPError as error:
        return {'ok': False, 'error': f'Server酱 HTTP {error.code}，请检查密钥、额度和服务状态'}
    except (OSError, ValueError, urllib.error.URLError, http.client.HTTPException) as error:
        # Exception strings may contain a request URL, which contains the secret.
        kind = _error_kind(error)
        return {'ok': False, 'error': f'推送网络请求失败（{kind}），请检查服务器网络或代理设置'}


def send(title, description, *, kind='round', cfg=None, short=''):
    try:
        key = get_key()
        result = _post(key, title, description, short=short) if key else {'ok': False, 'error': '尚未配置 Server酱 SendKey'}
    except ValueError as error:
        result = {'ok': False, 'error': str(error)}
    result.update(time=now(cfg).isoformat(timespec='seconds'), kind=kind)
    try:
        atomic_write(STATUS_PATH, json.dumps(result, ensure_ascii=False, indent=2))
    except OSError:
        log.warning('无法保存最近推送状态')
    log.info('微信推送：%s', result.get('message') or result.get('error'))
    return result


def _md(value, limit=100):
    """Names are plain text, even when a Telegram title looks like Markdown/HTML."""
    text = ' '.join(str(value or '').split())
    if len(text) > limit:
        text = text[:limit-1] + '…'
    return re.sub(r'([\\`*_\[\]#|~])', r'\\\1', html.escape(text, quote=False))


def _reason(result, cfg):
    """Human-facing causes without raw exceptions, URLs, paths or bot chatter."""
    actions = result.get('actions') or []
    if result['confirm'] == 'unconfirmed':
        uncertain = result.get('sent') is None or any(
            not a.get('button_only') and a.get('method') != 'button_only' and not a.get('confirmed_by_reply')
            and (
                a.get('sent') is None
                or (
                    a.get('sent')
                    and not any('.' not in str(m) for m in (a.get('sent_mids') or []))
                    and (a.get('warning') or a.get('sent_mids'))
                )
            )
            for a in actions
        )
        if uncertain or result.get('sent') is not True:
            return '发送状态待核对；请先查看 Telegram，勿直接重发。'
        candidate = result.get('candidate_replies') or any(
            item.get('source') == 'unrelated'
            and any(word in item.get('t', '') for word in ('签到', '打卡', '积分'))
            and judge_replies({}, cfg, [item.get('t', '')]) != 'unconfirmed'
            for item in result.get('evidence') or [])
        if candidate:
            return '已发出；捕获到候选反馈，归属尚未确认。'
        return '已发出；尚无可信的签到反馈。'
    error = str(result.get('error') or ' '.join(str(a.get('error') or '') for a in actions))
    if any(word in error for word in ('未登录', '登录失效', '扫码', '未能登录')):
        cause = '登录未就绪，请重新扫码。'
    elif any(word in error for word in ('禁言', '被禁', '发言权限')):
        cause = '发言受限，请检查群内权限。'
    elif '按钮' in error:
        cause = '必需按钮未完成，请检查签到规则。'
    elif '话题' in error:
        cause = '话题定位未完成，请检查话题设置。'
    elif any(word in error for word in ('目标会话', '核验', '核对失败', 'peer')):
        cause = '会话核验未通过，请先只读体检。'
    elif any(word in error for word in ('图片', '上传')):
        cause = '图片动作未完成，请检查图片与上传。'
    elif any(word in error.lower() for word in ('timeout', '超时', '加载', '同步', '网络')):
        cause = '页面加载或网络未就绪，可先只读体检。'
    else:
        cause = '请在会话详情核对失败原因和机器人反馈。'
    delivery = '未发送' if result.get('sent') is False else '已发送后失败' if result.get('sent') is True else '发送情况未记录'
    return f'{delivery}；{cause}'


def _category(lines, heading, rows, detail):
    if not rows:
        return
    lines.extend(['', f'## {heading}（{len(rows)}）', ''])
    account = None
    for row in rows:
        if row['account'] != account:
            account = row['account']
            lines.extend([f'### {_md(account, 50)}', ''])
        suffix = detail(row)
        lines.append(f'- **{_md(row["name"])}**' + (f' — {suffix}' if suffix else ''))


def _fit_description(lines, max_bytes=24000):
    notice = '> 明细较长，后续内容已省略；完整结果请在控制台查看。'
    budget = max_bytes - len(('\n\n'+notice).encode('utf-8'))
    output, used = [], 0
    for line in lines:
        size = len((line+'\n').encode('utf-8'))
        if used + size > budget:
            while output and (not output[-1] or output[-1].startswith('#')):
                output.pop()
            return '\n'.join([*output, '', notice])
        output.append(line)
        used += size
    return '\n'.join(output)


def _card_summary(description):
    return next((line.removeprefix('概览：') for line in description.splitlines() if line.startswith('概览：')), '')


def round_message(cfg, state, *, only=None, chats=None, accounts=None, error='', cancelled=False, source='手动签到', today=None, prefix=''):
    current = now(cfg)
    date = today or f'{current:%Y-%m-%d}'
    counts = {'success': 0, 'unconfirmed': 0, 'failed': 0, 'pending': 0}
    rows, per_account = [], {}
    for account in cfg.get('accounts', []):
        if only and account['name'] != only:
            continue
        if accounts and account['name'] not in accounts:
            continue
        for group in account.get('groups', []):
            chat = str(group['chat'])
            if chats is not None and chat not in chats:
                continue
            result = state.get('_last', {}).get(account['name'], {}).get(chat, {})
            if not isinstance(result, dict) or result.get('date') != date:
                result = {}
            confirm = result.get('confirm') or ('success' if state.get(account['name'], {}).get(chat) == date else 'pending')
            confirm = confirm if confirm in counts else 'failed'
            counts[confirm] += 1
            acc_counts = per_account.setdefault(account['name'], dict.fromkeys(counts, 0))
            acc_counts[confirm] += 1
            name = str(group.get('title') or chat)
            if group.get('topic'):
                name += ' / ' + group['topic']
            rows.append({**result, 'account': account['name'], 'name': name, 'chat': chat, 'confirm': confirm})
    drafts = [row for row in rows if row.get('draft_warning')]
    shared = sum(row['confirm'] == 'success' and row.get('source') == 'shared' for row in rows)
    issues = bool(counts['unconfirmed'] or counts['failed'] or counts['pending'] or drafts or error or cancelled)
    if error:
        title = '❌ 签到任务中断'
    elif cancelled:
        title = '⏹ 签到任务已取消'
    elif drafts:
        title = f'📝 签到需处理：{len(drafts)}份草稿异常'
    elif counts['failed']:
        title = f'❌ 签到结果：{counts["failed"]}失败 · {counts["unconfirmed"]}待核对'
    elif counts['unconfirmed']:
        title = f'⚠️ 签到结果：{counts["unconfirmed"]}项待核对'
    elif counts['pending']:
        title = f'⏸ 签到未完成：{counts["pending"]}项未执行'
    else:
        title = f'✅ 签到完成：{counts["success"]}/{len(rows)}' if rows else '📋 签到结果：当前范围暂无会话'
    overview = f'概览：✅ 成功 {counts["success"]} · ⚠️ 待核对 {counts["unconfirmed"]} · ❌ 失败 {counts["failed"]} · ⏸ 未执行 {counts["pending"]}'
    description = ([prefix, ''] if prefix else []) + [
        '## 📋 签到结果', '', f'{current:%Y-%m-%d %H:%M:%S %Z} · {_md(source, 60)}',
        '', f'**共 {len(rows)} 个会话 · {len(per_account)} 个账号**', '', overview]
    if shared:
        description.append(f'> 成功中含 {shared} 项共享完成，不重复计数。')
    if drafts:
        description.append(f'> 📝 {len(drafts)} 份原草稿恢复需要处理。')
    if cancelled:
        description.extend(['', '> ⏹ 任务已取消，后续会话未继续执行；已发出的消息请先核对。'])
    if error:
        description.extend(['', '> ❌ 任务异常中断，未执行项列在下方；请在控制台查看运行日志。'])
    if per_account:
        description.extend(['', '## 👤 账号概览', ''])
        icons = {'success': '✅', 'unconfirmed': '⚠️', 'failed': '❌', 'pending': '⏸'}
        for account, acc_counts in per_account.items():
            summary = ' · '.join(f'{icons[key]} {number}' for key, number in acc_counts.items() if number)
            description.append(f'- **{_md(account, 50)}**（{sum(acc_counts.values())}）：{summary}')
    _category(description, '📝 草稿恢复需处理', drafts, lambda row: '原草稿备份已保留；请到原账号和会话核对恢复。')
    _category(description, '❌ 执行失败', [row for row in rows if row['confirm'] == 'failed'], lambda row: _reason(row, cfg))
    uncertain = [row for row in rows if row['confirm'] == 'unconfirmed']
    _category(description, '⚠️ 待核对', uncertain, lambda row: _reason(row, cfg))
    if uncertain:
        description.extend(['', '> 有关联消息证据时可先“仅补查反馈”；发送状态不明时先查看 Telegram。待核对项请勿直接重发。'])
    _category(description, '⏸ 未执行', [row for row in rows if row['confirm'] == 'pending'], lambda row: '当前没有当日执行结果。')

    def success_detail(row):
        if row.get('source') == 'shared':
            origin = (row.get('shared_from') or {}).get('name')
            return '共享完成' + (f'，来源：{_md(origin, 70)}' if origin else '')
        if row.get('source') == 'legacy' or not row.get('date'):
            return '当日完成标记，未留存反馈证据'
        return ''

    _category(description, '✅ 已完成', [row for row in rows if row['confirm'] == 'success'], success_detail)
    if not rows:
        description.extend(['', '所选范围内没有签到会话。'])
    return title, _fit_description(description), issues


def send_preview(cfg, state):
    """Preview uses saved results and the configured account scope, never Telegram."""
    schedule = cfg.get('schedule') if isinstance(cfg.get('schedule'), dict) else {}
    title, description, _ = round_message(
        cfg, state, accounts=schedule.get('accounts'), source='推送样式预览',
        prefix='> 此为样式预览，使用当前已保存结果，不触发签到。',
    )
    return send('【预览】'+title, description, kind='test', cfg=cfg, short=_card_summary(description))


async def notify_round(cfg, state, **kwargs):
    options = cfg.get('notifications') if isinstance(cfg.get('notifications'), dict) else {}
    if not options.get('enabled', False):
        return {'skipped': True}
    try:
        title, description, issues = round_message(cfg, state, **kwargs)
        if options.get('only_on_issues', False) and not issues:
            return {'skipped': True}
        return await asyncio.to_thread(send, title, description, cfg=cfg, short=_card_summary(description))
    except Exception:
        log.warning('通知未完成，请查看定时与通知页面；签到结果已保留')
        return {'ok': False, 'error': '通知未完成，签到结果已保留'}

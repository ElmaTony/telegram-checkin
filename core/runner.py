"""Manual, cancellable check-in rounds with per-action evidence and reliable failures."""
import asyncio
import copy
import inspect
import json
import logging
import random
import re
import time
from contextvars import ContextVar
from datetime import datetime

from . import BASE_DIR
from .history import append_history
from .locks import FileLock
from .logic import (click_steps, delay_range, group_actions, infer_method,
                    judge_replies, merge_action_results, pick, pick_result_reply, status_of, recheck_eligible)
from .state import load_state, prune_state, record_last, record_title
from .storage import atomic_write
from .timeutil import now as local_now, set_timezone, reset_timezone

log = logging.getLogger('checkin')
ACTIVITY = {'act': '', 'accounts': {}}
CAPTURE = {'items': [], 'window': 0}
PROBE = {'running': False, 'progress': '', 'result': None, 'started': ''}
_TITLE_JUNK = re.compile(r'[\uE000-\uF8FF\u200b-\u200f\uFEFF]')
_LAUNCH_SEM = ContextVar('checkin_launch_sem', default=None)


def clean_title(value):
    return ' '.join(_TITLE_JUNK.sub('', value or '').split()).strip()


def order_chain_members(members):
    return sorted(members, key=lambda group: 0 if group.get('primary') else 1)


def _jsonable(value):
    if isinstance(value, set):
        return sorted(value)
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    return value


def save_capture():
    if not CAPTURE['items']:
        return None
    path = BASE_DIR / 'logs' / f'capture-{local_now():%Y%m%d-%H%M%S-%f}.json'
    items = list({(r['account'], r['chat']): r for r in CAPTURE['items']}.values())
    payload = {'time': f'{local_now():%Y-%m-%d %H:%M:%S}',
               'window_seconds': CAPTURE['window'], 'total_groups': len(items),
               'items': items}
    atomic_write(path, json.dumps(_jsonable(payload), ensure_ascii=False, indent=2))
    return path


def _progress(name, stage, group=None, **extra):
    item = {'stage': stage, 'chat': str((group or {}).get('chat', '')),
            'name': (group or {}).get('title') or str((group or {}).get('chat', '')), **extra}
    ACTIVITY['accounts'][name] = item
    ACTIVITY['act'] = f'[{name}] {stage}：{item["name"]}'


def share_result_to_members(state, name, today, system, members, winner_chat):
    shared = {}
    winner = state.get('_last', {}).get(name, {}).get(str(winner_chat), {})
    winner_name = winner.get('name') or next((g.get('title') for g in members if str(g['chat']) == str(winner_chat)), None) or str(winner_chat)
    for group in members:
        chat = str(group.get('chat'))
        previous = state.get('_last', {}).get(name, {}).get(chat, {})
        if chat == str(winner_chat) or (state.get(name, {}).get(chat) == today and previous.get('confirm') == 'success'):
            continue
        result = {'name': group.get('title') or chat, 'chat': chat, 'ok': True,
                  'confirm': 'success', 'clicked': False, 'with_image': False,
                  'error': None, 'sent': bool(previous.get('sent')), 'stage': 'completed', 'source': 'shared',
                  'shared_from': {'chat': str(winner_chat), 'name': winner_name},
                  'replies': [f'共用系统「{system}」已由「{winner_name}」完成签到', *winner.get('replies', [])],
                  'evidence': winner.get('evidence', [])}
        record_last(state, name, chat, result, mark_date=today, today=today)
        append_history(name, result)
        CAPTURE['items'].append({'account': name, **result})
        shared[chat] = result
    return shared


def _result(group, actions, engine=None, error=None, cfg=None):
    merged = merge_action_results(actions) if actions else {'ok': False, 'clicked': False, 'with_image': False, 'error': None}
    confirms = [action.get('confirm', 'unconfirmed') for action in actions]
    confirm = 'failed' if error or not merged['ok'] else ('failed' if 'failed' in confirms else 'unconfirmed' if 'unconfirmed' in confirms else 'success')
    if confirm == 'success' and any(action.get('buttons_complete') is False for action in actions):
        confirm = 'unconfirmed'
    trusted = [text for action in actions for text in action.get('trusted_replies', [])]
    replies = [text for action in actions for text in action.get('replies', [])]
    evidence = [item for action in actions for item in action.get('evidence', [])]
    actual_error = str(error) if error else merged.get('error')
    sent = any(action.get('sent') for action in actions)
    warning = '；'.join(action['warning'] for action in actions if action.get('warning'))
    draft_warning = '；'.join(action['draft_warning'] for action in actions if action.get('draft_warning')) or getattr(engine,'last_draft_warning',None) or ''
    sources = {item.get('source') for item in evidence}
    source = next((kind for kind in ('button_popup', 'reply_to_sent', 'mention_self', 'private_chat') if kind in sources), 'unknown')
    waiting = '已点击按钮，尚未取得可关联的反馈' if any(action.get('button_only') for action in actions) else '已发送，尚未捕获与本轮消息关联的签到反馈'
    return {'chat': str(group['chat']), 'name': group.get('title') or str(group['chat']),
            'ok': merged['ok'] and not error, 'confirm': confirm,
            'clicked': merged['clicked'], 'with_image': merged['with_image'],
            'sent': sent, 'error': actual_error, 'stage': 'failed' if confirm == 'failed' else 'completed' if confirm == 'success' else 'awaiting_confirmation',
            'draft_warning': draft_warning,
            'source': source, 'replies': trusted, 'raw_replies': replies, 'evidence': evidence,
            'actions': _jsonable(actions),
            'result_text': '' if actual_error else pick_result_reply(group, cfg or {}, trusted, confirm) or warning or (waiting if sent else ''),
            'expected_peer': getattr(engine, '_expected_peer', None),
            'actual_peer': getattr(engine, '_last_actual_peer', None)}


def _store(state, name, group, result, today, history=True):
    # An unconfirmed send is remembered to prevent accidental duplicate sends.
    mark_date = today if result['confirm'] == 'success' or result.get('sent') else None
    record_last(state, name, result['chat'], result, mark_date=mark_date, today=today)
    if history:
        append_history(name, result)
    log.info('[%s] %s：%s%s', name, result['name'], status_of(result), f'，{result["error"]}' if result.get('error') else '')


async def _send_group_actions(engine, state, name, group, cfg, lo, hi, today=None):
    engine.last_draft_warning = None
    today = today or f'{local_now():%Y-%m-%d}'
    configured_actions = group_actions(group)
    all_button_only = bool(configured_actions) and all((a.get('method') or infer_method(a)) == 'button_only' for a in configured_actions)
    _progress(name, '打开会话', group)
    open_kwargs = {'require_composer': False} if all_button_only and 'require_composer' in inspect.signature(engine.open_chat).parameters else {}
    for attempt in range(2):
        try:
            await engine.open_chat(str(group['chat']), group.get('title'), group.get('topic'), group.get('folder'), group.get('topic_id'), **open_kwargs)
            break
        except RuntimeError as error:
            if attempt == 0 and '超过 45 秒' in str(error):
                log.warning('[%s] %s 首次打开超时（未发送），重试一次', name, group.get('title') or group['chat'])
                await asyncio.sleep(1.0)
                continue
            raise
    await engine.assert_destination()
    cancelled_holder = {}
    if all_button_only:
        try:
            return await _execute_group_actions(engine, state, name, group, cfg, lo, hi, today=today, cancelled_holder=cancelled_holder)
        except asyncio.CancelledError:
            if 'result' in cancelled_holder:
                _store(state, name, group, cancelled_holder['result'], today)
            raise
    try:
        async with engine.preserve_draft():
            outcome = await _execute_group_actions(engine, state, name, group, cfg, lo, hi, today=today, cancelled_holder=cancelled_holder)
    except asyncio.CancelledError:
        if 'result' in cancelled_holder:
            cancelled_result = cancelled_holder['result']
            if engine.last_draft_warning:
                cancelled_result['draft_warning'] = '；'.join(filter(None, (cancelled_result.get('draft_warning'), engine.last_draft_warning)))
            _store(state, name, group, cancelled_result, today)
        raise
    if engine.last_draft_warning and outcome[1]:
        outcome[1][-1]['draft_warning'] = engine.last_draft_warning
    return outcome


async def _wait_until(deadline):
    # Some event loops can wake a timer before its requested deadline.
    while (remaining := deadline - time.monotonic()) > 0:
        await asyncio.sleep(remaining)


async def _watch_feedback(engine, boundary, group, cfg, *, sent_mids, started_at=None, min_wait=0.0, max_wait=None, **extra):
    configured_wait = float(cfg.get('reply_wait_seconds', 6))
    wait_seconds = max(configured_wait, min_wait) if configured_wait >= 1.0 else configured_wait
    if max_wait is not None:
        wait_seconds = min(wait_seconds, float(max_wait))
    return await engine.watch_new_incoming(
        boundary, wait_seconds,
        group.get('reply_mode') or cfg.get('reply_mode') or 'reply_only',
        idle_after=0.8,
        sent_mids=sent_mids, started_at=started_at,
        is_complete=lambda trusted: judge_replies(group, cfg, trusted, trusted) != 'unconfirmed',
        **extra)


def _reconcile_action_warning(action):
    if not action.get('warning'):
        return
    has_server_mid = any('.' not in str(m) for m in (action.get('sent_mids') or []))
    if has_server_mid or action.get('confirmed_by_reply') or action.get('confirm') != 'unconfirmed':
        if action.get('buttons_complete') is False and not action.get('button_only'):
            action['warning'] = '必需按钮尚未完成，请核对原消息，不要重新发送口令'
        else:
            action.pop('warning', None)


def _judge_action(group, cfg, action):
    confirm = judge_replies(group, cfg, action['replies'], action['trusted_replies'])
    return 'unconfirmed' if confirm == 'success' and action.get('buttons_complete') is False else confirm


async def _execute_group_actions(engine, state, name, group, cfg, lo, hi, today=None, cancelled_holder=None):
    """Each required action has its own boundary, sent IDs, confirmation and error."""
    today = today or f'{local_now():%Y-%m-%d}'
    info = await engine.opened_chat_info()
    title = clean_title(info.get('title'))
    if len(title) >= 2 and not group.get('topic'):
        record_title(state, str(group['chat']), title)
    mode = group.get('reply_mode') or cfg.get('reply_mode') or 'reply_only'
    actions = []
    group_sent_at = None
    for index, configured in enumerate(group_actions(group), 1):
        method = configured.get('method') or infer_method(configured)
        _progress(name, '准备动作', group, action=index, sent_at=group_sent_at)
        before = set(engine.last_sent_mids)
        boundary = {}
        action = {'method': method, 'ok': False, 'clicked': False, 'with_image': False, 'error': None,
                  'sent': False, 'replies': [], 'trusted_replies': [], 'evidence': [],
                  'confirm': 'unconfirmed', 'boundary': boundary, 'sent_mids': []}
        if method in ('command_button', 'button_only'):
            action['buttons_complete'] = False
        button_only = method == 'button_only'
        if button_only:
            action['button_only'] = True
        dispatch_prepared = False
        def checkpoint(known_sent=False):
            pending = copy.deepcopy(action)
            if not known_sent:
                pending.update(sent=None, warning='操作结果待核对；若进程中断，请先检查 Telegram，避免重复签到' if button_only else '发送请求待核对；若进程中断，请先检查 Telegram，不要直接重发')
            result = _result(group, [*actions, pending], engine, cfg=cfg)
            result.update(confirm='unconfirmed', stage='awaiting_confirmation',
                          sent=True if known_sent or any(a.get('sent') for a in actions) else None,
                          result_text=('已点击按钮，等待反馈' if button_only else '消息已发出，等待完成签到动作与反馈') if known_sent else pending['warning'])
            record_last(state, name, str(group['chat']), result, mark_date=today, today=today)
        async def before_dispatch():
            nonlocal dispatch_prepared
            checkpoint()
            dispatch_prepared = True
        try:
            boundary = await engine.snapshot_incoming(mode)
            action['boundary'] = boundary
            next_send_at = getattr(engine, '_next_send_at', 0)
            remaining = next_send_at - time.monotonic()
            if remaining > 0:
                _progress(name, '等待发送间隔', group, action=index, sent_at=group_sent_at)
                await _wait_until(next_send_at)
            _progress(name, '点击按钮' if button_only else '发送', group, action=index, sent_at=group_sent_at)
            engine.before_send = before_dispatch
            if button_only:
                boundary['button_only'] = True
                action['button_mid'] = await engine.click_existing_button(configured['click_button'], configured.get('button_message'), 0)
                action.update(clicked=True, buttons_complete=True)
            elif method in ('image_text', 'image_only'):
                action['with_image'] = True
                action['sent_text'] = pick(configured.get('message')) if method == 'image_text' else None
                await engine.send_image(pick(configured.get('image')), action['sent_text'])
            else:
                action['sent_text'] = pick(configured.get('message', '签到'))
                await engine.send_text(action['sent_text'])
            action['sent'] = True
            if getattr(engine, '_last_send_confirmed_by_reply', False):
                action['confirmed_by_reply'] = True
            engine._next_send_at = (getattr(engine, 'send_started_monotonic', None) or time.monotonic()) + random.uniform(lo, hi)
            group_sent_at = group_sent_at or getattr(engine, 'send_started_at', None)
            outgoing_mids = sorted(engine.last_sent_mids - before)
            action['sent_mids'] = list(outgoing_mids)
            engine._active_mids = set(outgoing_mids)
            if method == 'command_button':
                checkpoint(known_sent=True)
                _progress(name, '点击按钮', group, action=index, sent_at=group_sent_at)
                for button in click_steps(configured):
                    if not await engine.click_button(button):
                        raise RuntimeError(f'未找到本轮消息对应的按钮「{button}」；已发送口令，请勿直接重发')
                    action['clicked'] = True
                if getattr(engine, '_last_button_mid', None):
                    action['button_mid'] = engine._last_button_mid
                action['buttons_complete'] = True
            action['ok'] = True
            action['sent_mids'] = sorted(outgoing_mids)
            checkpoint(known_sent=True)
            _progress(name, '等待反馈', group, action=index, sent_at=group_sent_at)
            watch_sig = inspect.signature(engine.watch_new_incoming).parameters
            if button_only:
                if 'check_popup' in watch_sig:
                    watch_extra = {
                        'check_popup': True,
                        'watch_button_mid': action.get('button_mid'),
                        'button_initial_text': getattr(engine, '_last_button_initial_text', None),
                    }
                    if 'button_initial_buttons' in watch_sig:
                        watch_extra['button_initial_buttons'] = getattr(engine, '_last_button_initial_buttons', None)
                    action['replies'] = await _watch_feedback(
                        engine, boundary, group, cfg, sent_mids=action['sent_mids'],
                        started_at=getattr(engine, 'send_started_monotonic', None), min_wait=8.0, **watch_extra,
                    )
                    action['sent_mids'] = getattr(engine, 'last_allowed_mids', action['sent_mids'])
                    action['trusted_replies'] = list(engine.last_replied)
                    action['evidence'] = list(engine.last_evidence)
                else:
                    popup = await engine.read_button_popup(float(cfg.get('reply_wait_seconds', 6)))
                    if popup:
                        action.update(replies=[popup], trusted_replies=[popup], evidence=[
                            {'source': 'button_popup', 't': popup, 'mid': action['button_mid'], 'ts': time.time()}
                        ])
                    else:
                        action['replies'] = await _watch_feedback(engine, boundary, group, cfg, sent_mids=action['sent_mids'], started_at=getattr(engine, 'send_started_monotonic', None), min_wait=8.0)
                        action['sent_mids'] = getattr(engine, 'last_allowed_mids', action['sent_mids'])
                        action['trusted_replies'] = list(engine.last_replied)
                        action['evidence'] = list(engine.last_evidence)
            else:
                watch_extra = {}
                if method == 'command_button' and action.get('clicked') and 'check_popup' in watch_sig:
                    watch_extra = {
                        'check_popup': True,
                        'watch_button_mid': action.get('button_mid'),
                        'button_initial_text': getattr(engine, '_last_button_initial_text', None),
                    }
                    if 'button_initial_buttons' in watch_sig:
                        watch_extra['button_initial_buttons'] = getattr(engine, '_last_button_initial_buttons', None)
                reply_target_mids = sorted(engine._active_mids) if method == 'command_button' else action['sent_mids']
                action['replies'] = await _watch_feedback(
                    engine, boundary, group, cfg, sent_mids=reply_target_mids,
                    started_at=getattr(engine, 'send_started_monotonic', None),
                    min_wait=8.0 if method == 'command_button' else 0.0, **watch_extra,
                )
                allowed_after = [m for m in getattr(engine, 'last_allowed_mids', action['sent_mids']) if m in set(outgoing_mids) or m not in {action.get('button_mid')}]
                action['sent_mids'] = allowed_after if outgoing_mids else action['sent_mids']
                action['trusted_replies'] = list(engine.last_replied)
                action['evidence'] = list(engine.last_evidence)
            action['confirm'] = _judge_action(group, cfg, action)
        except asyncio.CancelledError:
            action['error'] = '用户取消任务；如已发出，请先核对反馈，不要重复发送'
            unsettled = dispatch_prepared or action['sent'] or any(a.get('sent') for a in actions)
            action['confirm'] = 'unconfirmed' if unsettled else 'failed'
            if dispatch_prepared and not action['sent']:
                action.update(sent=None, warning='发送结果待核对，请先检查 Telegram，不要直接重发')
            actions.append(action)
            result = _result(group, actions, engine, action['error'], cfg=cfg)
            if unsettled:
                result.update(confirm='unconfirmed', stage='awaiting_confirmation', sent=True if any(a.get('sent') for a in actions) else None)
            if cancelled_holder is not None:
                cancelled_holder['result'] = result
            else:
                _store(state, name, group, result, today)
            raise
        except Exception as error:
            action['sent'] = action['sent'] or bool(getattr(error, 'sent', False))
            if action['sent']:
                engine._next_send_at = (getattr(engine, 'send_started_monotonic', None) or time.monotonic()) + random.uniform(lo, hi)
            if getattr(error, 'uncertain', False):
                action.update(ok=True, confirm='unconfirmed', warning=str(error))
                if action.get('buttons_complete') is False and not button_only:
                    action['warning'] += '；必需按钮尚未完成，请核对原消息，不要重新发送口令'
                group_sent_at = group_sent_at or getattr(engine, 'send_started_at', None)
                _progress(name, '核对发送与反馈', group, action=index, sent_at=group_sent_at)
                action['sent_mids'] = sorted(engine.last_sent_mids - before)
                # Keep the observer alive even when a bot deleted the outgoing bubble too soon.
                try:
                    watch_sig = inspect.signature(engine.watch_new_incoming).parameters
                    watch_extra = {}
                    if (button_only or (method == 'command_button' and action.get('clicked'))) and 'check_popup' in watch_sig:
                        watch_extra = {
                            'check_popup': True,
                            'watch_button_mid': action.get('button_mid'),
                            'button_initial_text': getattr(engine, '_last_button_initial_text', None),
                        }
                        if 'button_initial_buttons' in watch_sig:
                            watch_extra['button_initial_buttons'] = getattr(engine, '_last_button_initial_buttons', None)
                    action['replies'] = await _watch_feedback(engine, boundary, group, cfg, sent_mids=action['sent_mids'], started_at=getattr(engine, 'send_started_monotonic', None), **watch_extra)
                    action['sent_mids'] = getattr(engine, 'last_allowed_mids', action['sent_mids'])
                    action['trusted_replies'] = list(engine.last_replied)
                    action['evidence'] = list(engine.last_evidence)
                    action['confirm'] = _judge_action(group, cfg, action)
                    _reconcile_action_warning(action)
                except asyncio.CancelledError:
                    action.update(error='用户取消反馈核对；发送状态待核对', confirm='unconfirmed')
                    actions.append(action)
                    cancelled_result = _result(group, actions, engine, cfg=cfg)
                    if cancelled_holder is not None:
                        cancelled_holder['result'] = cancelled_result
                    else:
                        _store(state, name, group, cancelled_result, today)
                    raise
                except Exception as feedback_error:
                    action['warning'] += f'；反馈采集未完成：{feedback_error}'
            elif button_only and action.get('clicked'):
                action.update(ok=True, confirm='unconfirmed', warning=f'按钮已点击，反馈读取未完成：{error}')
            else:
                action['error'] = f'{type(error).__name__}: {error}'
                action['confirm'] = 'failed'
        finally:
            engine.before_send = None
        actions.append(action)
        if action['confirm'] == 'failed' or action.get('warning'):
            break
    return group.get('title') or str(group['chat']), actions, None, mode


async def _recheck(engine, group, result, cfg):
    # Work on a private copy; the previous result may be referenced by live state.
    result = {'actions': copy.deepcopy(result['actions'])}
    await engine.open_chat(str(group['chat']), group.get('title'), group.get('topic'), group.get('folder'), group.get('topic_id'))
    await engine._scroll_chat_bottom()
    configured = group_actions(group)
    for index, action in enumerate(result['actions']):
        if action.get('button_only'):
            continue
        if index < len(configured) and infer_method(configured[index]) == 'command_button' and not action.get('clicked'):
            action.setdefault('buttons_complete', False)
        if action.get('buttons_complete') is False and action.get('confirm') == 'success':
            action['confirm'] = 'unconfirmed'
        reviewable_failure = action.get('confirm') == 'failed' and action.get('ok') and not action.get('error')
        if (action.get('confirm') != 'unconfirmed' and not reviewable_failure) or not action.get('sent'):
            continue
        history = await engine.collect_feedback_history(action['boundary'])
        ids = set(action.get('sent_mids') or [])
        for old, new in history.get('aliases', []):
            if str(old) in ids:
                ids.add(str(new))
        action['sent_mids'] = sorted(ids)
        has_server_mid = any('.' not in str(m) for m in (action.get('sent_mids') or []))
        if not has_server_mid:
            text = action.get('sent_text')
            if text is None and index < len(configured):
                candidate = configured[index].get('message')
                text = candidate if isinstance(candidate, str) else None
            recovered = await engine.recover_sent_mids(action['boundary'], text)
            if recovered['sent_mids']:
                action['sent_mids'] = sorted(set(action.get('sent_mids') or []) | set(recovered['sent_mids']))
                action['id_recovery'] = recovered
                has_server_mid = any('.' not in str(m) for m in action['sent_mids'])
        if not has_server_mid and not str((action.get('boundary') or {}).get('peer', '')).isdigit():
            continue
        engine.last_sent_mids.update(action['sent_mids'])
        replies = await _watch_feedback(engine, action['boundary'], group, cfg, sent_mids=action['sent_mids'], max_wait=1.5)
        action['sent_mids'] = getattr(engine, 'last_allowed_mids', action['sent_mids'])
        action['replies'] = list(dict.fromkeys([*action.get('replies', []), *replies]))
        action['trusted_replies'] = list(dict.fromkeys([*action.get('trusted_replies', []), *engine.last_replied]))
        evidence = {(item.get('mid'), item.get('t')):item for item in action.get('evidence', [])}
        evidence.update({(item.get('mid'), item.get('t')):item for item in engine.last_evidence})
        action['evidence'] = list(evidence.values())
        action['confirm'] = _judge_action(group, cfg, action)
        _reconcile_action_warning(action)
        if action.get('ok') and action['confirm'] != 'unconfirmed':
            action['error'] = None
            action.pop('warning', None)
    return _result(group, result['actions'], engine, cfg=cfg)


def _blocked_systems(members, state, name, today):
    blocked = set()
    last_acc = state.get('_last', {}).get(name, {})
    done_acc = state.get(name, {})
    for group in members:
        system = group.get('share_group')
        if not system:
            continue
        chat = str(group['chat'])
        prev = last_acc.get(chat, {})
        if done_acc.get(chat) == today:
            blocked.add(system)
        elif isinstance(prev, dict) and prev.get('date') == today and (prev.get('confirm') in ('success', 'unconfirmed') or prev.get('sent') is not False):
            blocked.add(system)
    return blocked


async def _web_account_worker(account, cfg, state, today, lo, hi, wait_seconds, chats, recheck=False):
    from web_engine import WebEngine
    name = account['name']
    members = account.get('groups', [])
    blocked = set() if (chats is not None or recheck) else _blocked_systems(members, state, name, today)
    selected = [
        group for group in members
        if (str(group['chat']) in chats if chats is not None else (
            state.get(name, {}).get(str(group['chat'])) != today and group.get('share_group') not in blocked
        ))
    ]
    if not selected:
        return {}
    engine = WebEngine(account, cfg)
    results, pending = {}, []
    launch_sem = _LAUNCH_SEM.get()
    try:
        if launch_sem is not None:
            async with launch_sem:
                _progress(name, '启动浏览器')
                await engine.launch()
                await engine.ensure_login()
        else:
            _progress(name, '启动浏览器')
            await engine.launch()
            await engine.ensure_login()
        if recheck:
            for group in selected:
                previous = state.get('_last', {}).get(name, {}).get(str(group['chat']), {})
                if not recheck_eligible(previous, today):
                    prev_actions = previous.get('actions') or []
                    result = _result(group, prev_actions, engine, cfg=cfg)
                    result.update(error='旧结果缺少可靠发送边界，无法安全补查；请先在 Telegram 核对', stage='recheck_failed')
                    results[result['chat']] = (group, result)
                    continue
                _progress(name, '仅补查反馈', group)
                try:
                    result = await _recheck(engine, group, {'actions': previous['actions']}, cfg)
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    result = _result(group, previous['actions'], engine, cfg=cfg)
                    result.update(error=f'补查未完成：{error}', stage='recheck_failed')
                results[result['chat']] = (group, result)
                _store(state, name, group, result, today)
                if result['confirm'] == 'success' and group.get('share_group'):
                    all_members = [g for g in members if g.get('share_group') == group['share_group']]
                    shared = share_result_to_members(state, name, today, group['share_group'], all_members, result['chat'])
                    for chat, shared_result in shared.items():
                        if chat in results:
                            results[chat] = (results[chat][0], shared_result)
        else:
            solo, chains = [], {}
            for group in selected:
                if group.get('share_group'):
                    chains.setdefault(group['share_group'], []).append(group)
                else:
                    solo.append(group)
            work = [(None, [group]) for group in solo] + [(key, order_chain_members(groups)) for key, groups in chains.items()]
            for system, groups in work:
                for group in groups:
                    try:
                        _, actions, _, _ = await _send_group_actions(engine, state, name, group, cfg, lo, hi, today=today)
                        result = _result(group, actions, engine, cfg=cfg)
                    except asyncio.CancelledError:
                        raise
                    except Exception as error:
                        result = _result(group, [], engine, error, cfg=cfg)
                    results[result['chat']] = (group, result)
                    needs_check = result['confirm'] == 'unconfirmed' and cfg.get('reply_second_pass') and recheck_eligible(result)
                    _store(state, name, group, result, today, history=not needs_check)
                    if needs_check:
                        pending.append((group, result))
                    if result['confirm'] == 'success' and system:
                        all_members = [g for g in members if g.get('share_group') == system]
                        shared = share_result_to_members(state, name, today, system, all_members, result['chat'])
                        for item in all_members:
                            chat = str(item['chat'])
                            if chat in results and chat in shared:
                                results[chat] = (item, shared[chat])
                        break
                    # A sent or uncertain command must not fan out into more sends.
                    if system and (result['confirm'] == 'unconfirmed' or result.get('sent') is not False):
                        break
            for group, result in pending:
                _progress(name, '补查反馈', group)
                try:
                    updated = await _recheck(engine, group, result, cfg)
                    results[updated['chat']] = (group, updated)
                    _store(state, name, group, updated, today)
                    if updated['confirm'] == 'success' and group.get('share_group'):
                        shared = share_result_to_members(state, name, today, group['share_group'], [g for g in members if g.get('share_group') == group['share_group']], updated['chat'])
                        for chat, shared_result in shared.items():
                            if chat in results:
                                results[chat] = (results[chat][0], shared_result)
                except asyncio.CancelledError:
                    append_history(name, result)
                    raise
                except Exception as error:
                    result['recheck_error'] = str(error)
                    record_last(state, name, result['chat'], result, today=today)
                    append_history(name, result)
    except asyncio.CancelledError:
        _progress(name, '已取消')
        raise
    except Exception as error:
        # Startup, login and unexpected worker failures are visible for every affected target.
        for group in selected:
            if str(group['chat']) not in results:
                result = _result(group, [], engine, error, cfg=cfg)
                results[result['chat']] = (group, result)
                _store(state, name, group, result, today)
    finally:
        try:
            await engine.close()
        except Exception:
            log.exception('[%s] 关闭浏览器失败', name)
    for group, result in results.values():
        CAPTURE['items'].append({'account': name, **result})
    _progress(name, '已完成')
    retry = [chat for chat, (_, result) in results.items() if result['confirm'] != 'success']
    return {name: retry} if retry else {}


async def run_web_round(cfg, state, only=None, chats=None, chats_by_account=None, recheck=False, accounts=None):
    lock = FileLock(BASE_DIR / 'data' / 'locks' / 'round.lock').acquire()
    zone_token = set_timezone(cfg)
    sem_token = None
    tasks = []
    try:
        state.clear()
        state.update(load_state())
        prune_state(cfg, state)
        today = f'{local_now():%Y-%m-%d}'
        lo, hi = delay_range(cfg.get('group_delay_seconds'), (1, 3))
        wait = float(cfg.get('reply_wait_seconds', 6))
        CAPTURE.update(items=[], window=wait)
        ACTIVITY.update(act='', accounts={})
        delay_lo, delay_hi = delay_range(cfg.get('account_delay_seconds'), (0, 0))
        sem_token = _LAUNCH_SEM.set(asyncio.Semaphore(1))
        for account in cfg.get('accounts', []):
            if only and account['name'] != only:
                continue
            if accounts and account['name'] not in accounts:
                continue
            selected = (chats_by_account or {}).get(account['name'], chats)
            blocked = set() if (selected is not None or recheck) else _blocked_systems(account.get('groups', []), state, account['name'], today)
            todo = any(
                (str(g['chat']) in selected if selected is not None else (
                    state.get(account['name'], {}).get(str(g['chat'])) != today and g.get('share_group') not in blocked
                ))
                for g in account.get('groups', [])
            )
            if not todo:
                continue
            if tasks and delay_hi:
                await _wait_until(time.monotonic() + random.uniform(delay_lo, delay_hi))
            tasks.append(asyncio.create_task(_web_account_worker(account, cfg, state, today, lo, hi, wait, selected, recheck)))
            if delay_hi:
                await asyncio.sleep(0)  # Start this worker before timing the next account's interval.
        outcomes = await asyncio.gather(*tasks, return_exceptions=True)
        retries = {}
        first_error = None
        for outcome in outcomes:
            if isinstance(outcome, BaseException):
                if isinstance(outcome, asyncio.CancelledError):
                    raise outcome
                if first_error is None:
                    first_error = outcome
                log.exception('账号任务异常', exc_info=outcome)
            else:
                retries.update(outcome)
        if first_error is not None:
            raise first_error
        return retries
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        ACTIVITY['act'] = ''
        try:
            save_capture()
        finally:
            lock.release()
            reset_timezone(zone_token)
            if sem_token is not None:
                _LAUNCH_SEM.reset(sem_token)


async def _probe_account(account, cfg, chats=None):
    from web_engine import WebEngine
    start = time.monotonic()
    result = {'account': account['name'], 'login': None, 'chats': [], 'error': None, 'elapsed': 0}
    engine = WebEngine(account, cfg)
    try:
        await engine.launch()
        result['login'] = await engine.is_logged_in()
        if not result['login']:
            raise RuntimeError('未登录，需要扫码')
        for group in account.get('groups', []):
            if chats is not None and str(group['chat']) not in chats:
                continue
            _progress(account['name'], '只读体检', group)
            PROBE['progress'] = ACTIVITY['act']
            actions = group_actions(group)
            probe_method = 'button_only' if actions and all((a.get('method') or infer_method(a)) == 'button_only' for a in actions) else infer_method(group)
            result['chats'].append(await engine.probe_chat(str(group['chat']), group.get('title'), group.get('topic'), group.get('folder'), probe_method, group.get('topic_id')))
    except asyncio.CancelledError:
        result['error'] = '用户取消体检'
        raise
    except Exception as error:
        result['error'] = str(error)
    finally:
        await engine.close()
        result['elapsed'] = round(time.monotonic() - start, 2)
    return result


async def run_probe_round(cfg, only=None, chats=None):
    lock = FileLock(BASE_DIR / 'data' / 'locks' / 'round.lock').acquire()
    zone_token = set_timezone(cfg)
    ACTIVITY.update(act='', accounts={})
    PROBE.update(running=True, progress='准备体检', result=None, started=f'{local_now():%Y-%m-%d %H:%M:%S}')
    try:
        accounts = await asyncio.gather(*[_probe_account(a, cfg, chats) for a in cfg.get('accounts', []) if (not only or a['name'] == only) and a.get('groups')])
        total = sum(len(a['chats']) for a in accounts)
        ok = sum(chat['ok'] for a in accounts for chat in a['chats'])
        PROBE['result'] = {'time': PROBE['started'], 'accounts': accounts, 'summary': {'total': total, 'ok': ok, 'bad': total-ok, 'account_errors': sum(bool(a['error']) for a in accounts)}}
        return PROBE['result']
    finally:
        PROBE.update(running=False, progress='')
        ACTIVITY['act'] = ''
        lock.release()
        reset_timezone(zone_token)

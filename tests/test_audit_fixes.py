"""Regression tests for audit fixes across locks, storage, config, notifications, GUI and runner."""
import asyncio
import copy
import http.client
import json
import os
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import checkin
import gui
import web_engine
from core import config, locks, logic, notifications, paths, runner, scheduler, state, storage

ROOT = Path(__file__).resolve().parents[1]
KEY = 'SCT' + 'a' * 24


class AuditFixUnitTests(unittest.TestCase):
    def test_file_lock_fails_immediately_when_held(self):
        with tempfile.TemporaryDirectory() as directory:
            lock_path = Path(directory) / 'round.lock'
            first = locks.FileLock(lock_path).acquire()
            try:
                started = time.monotonic()
                with self.assertRaises(RuntimeError):
                    locks.FileLock(lock_path).acquire()
                self.assertLess(time.monotonic() - started, 1.5)
            finally:
                first.release()

    def test_account_lock_path_is_case_insensitive(self):
        p1 = paths.account_lock_path(Path('/tmp/app'), 'MainAcc')
        p2 = paths.account_lock_path(Path('/tmp/app'), 'mainacc')
        self.assertEqual(p1, p2)

    def test_atomic_write_retries_transient_permission_error_on_windows(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'state.json'
            calls = {'n': 0}
            real_replace = os.replace

            def flaky_replace(src, dst):
                calls['n'] += 1
                if calls['n'] < 3:
                    raise PermissionError('busy')
                return real_replace(src, dst)

            with patch.object(storage, '_WINDOWS', True), patch.object(storage.os, 'replace', side_effect=flaky_replace), patch.object(storage.time, 'sleep'):
                storage.atomic_write(target, '{"ok":true}')
            self.assertEqual(calls['n'], 3)
            self.assertEqual(target.read_text(encoding='utf-8'), '{"ok":true}')

    def test_config_validation_catches_audit_edge_cases(self):
        # ZoneInfo directory name should return readable error instead of raising OSError
        err = config.validate_config({'accounts': [], 'schedule': {'timezone': 'Asia'}})
        self.assertIsNotNone(err)
        self.assertIn('时区', err)

        # Empty message list, blank separators, trailing newline, leading space chat bypass
        cases = [
            {'accounts': [{'name': 'A', 'groups': [{'chat': '-101', 'method': 'text', 'message': []}]}]},
            {'accounts': [{'name': 'A', 'groups': [{'chat': '-101', 'method': 'command_button', 'message': '/sign', 'click_button': ' , '}]}]},
            {'accounts': [{'name': 'A', 'groups': [{'chat': '-101', 'method': 'image_text', 'image': 'a.jpg', 'message': '   '}]}]},
            {'accounts': [{'name': 'A', 'groups': [{'chat': '701', 'method': 'button_only', 'click_button': '签到\n'}]}]},
            {'accounts': [{'name': 'A', 'groups': [{'chat': ' -101', 'method': 'button_only', 'click_button': '签到'}]}]},
            {'accounts': [{'name': 'A', 'groups': [{'chat': '701', 'method': 'button_only', 'click_button': '签到', 'button_message': []}]}]},
        ]
        for bad in cases:
            with self.subTest(bad=bad):
                self.assertIsNotNone(config.validate_config(bad))

    def test_button_message_ref_accepts_int_http_and_single_query(self):
        self.assertEqual(logic.button_message_ref(12345), ('12345', None, None))
        self.assertEqual(logic.button_message_ref('http://t.me/c/999/42?single'), ('42', '-999', None))
        self.assertEqual(logic.button_message_ref('https://t.me/demo_group/10/42?thread=10&single'), ('42', '@demo_group', '10'))
        with self.assertRaises(ValueError):
            logic.button_message_ref(True)

    def test_notifications_catches_http_exception_without_leaking_key_and_truncates_utf8(self):
        with patch.object(notifications.urllib.request, 'urlopen', side_effect=http.client.IncompleteRead(b'part')):
            result = notifications._post(KEY, '标题\r\n换行', '中' * 15000)
        self.assertFalse(result['ok'])
        self.assertIn('IncompleteRead', result['error'])
        self.assertNotIn(KEY, result['error'])
        self.assertLessEqual(len(notifications._truncate_utf8('中' * 15000, 28000).encode('utf-8')), 28000)

        import ssl
        import urllib.error

        class DirectOK:
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def read(self, n): return b'{"code":0}'

        class FakeOpener:
            def __init__(self): self.calls = 0
            def open(self, req, timeout=15):
                self.calls += 1
                return DirectOK()

        fake_opener = FakeOpener()
        with patch.object(notifications.urllib.request, 'getproxies', return_value={'https': 'http://127.0.0.1:7897'}), \
             patch.object(notifications.urllib.request, 'urlopen', side_effect=urllib.error.URLError(ssl.SSLError('handshake eof'))), \
             patch.object(notifications.urllib.request, 'build_opener', return_value=fake_opener):
            fallback_res = notifications._post(KEY, '标题', '正文')
        self.assertTrue(fallback_res['ok'])
        self.assertEqual(fake_opener.calls, 1)

        # Direct pre-HTTP TLS handshake timeout retries once; post-connect read timeout does not retry
        with patch.object(notifications.urllib.request, 'getproxies', return_value={}), \
             patch.object(notifications.time, 'sleep'), \
             patch.object(notifications.urllib.request, 'urlopen', side_effect=[
                 urllib.error.URLError(TimeoutError('_ssl.c:993: The handshake operation timed out')),
                 DirectOK(),
             ]) as mock_open:
            retry_res = notifications._post(KEY, '标题', '正文')
        self.assertTrue(retry_res['ok'])
        self.assertEqual(mock_open.call_count, 2)

        with patch.object(notifications.urllib.request, 'getproxies', return_value={}), \
             patch.object(notifications.urllib.request, 'urlopen', side_effect=TimeoutError('read timed out')) as mock_no_retry:
            no_retry_res = notifications._post(KEY, '标题', '正文')
        self.assertFalse(no_retry_res['ok'])
        self.assertEqual(mock_no_retry.call_count, 1)

    def test_resolve_proxy_supports_direct_and_authenticated_url(self):
        self.assertIsNone(web_engine.resolve_proxy({'web_proxy': 'direct'}))
        self.assertIsNone(web_engine.resolve_proxy({'web_proxy': 'NONE'}))
        proxy = web_engine.resolve_proxy({'web_proxy': 'http://alice:p%40ss@127.0.0.1:7890'})
        self.assertEqual(proxy, {'server': 'http://127.0.0.1:7890', 'username': 'alice', 'password': 'p@ss'})

    def test_checkin_cli_rejects_unknown_flags(self):
        with self.assertRaises(SystemExit) as ctx:
            checkin._parse_cli(['--unknown-flag'])
        self.assertNotEqual(ctx.exception.code, 0)

    def test_scheduler_handles_null_schedule_block(self):
        info = scheduler.describe({'accounts': [], 'schedule': None}, datetime(2026, 11, 4, 8, 0, tzinfo=ZoneInfo('Asia/Hong_Kong')))
        self.assertEqual(info['error'], '')
        self.assertIsNone(info['next_run'])

    def test_result_uses_global_keywords_and_record_last_respects_round_date(self):
        cfg = {'success_keywords': ['自定义全局命中']}
        res = runner._result({'chat': '-101', 'title': '示例群'}, [
            {'ok': True, 'clicked': False, 'with_image': False, 'sent': True, 'confirm': 'success', 'error': None,
             'replies': ['无关消息', '自定义全局命中 +10'], 'trusted_replies': ['无关消息', '自定义全局命中 +10'],
             'evidence': [{'source': 'reply_to_sent', 'mid': '1', 't': '自定义全局命中 +10'}]}
        ], cfg=cfg)
        self.assertEqual(res['result_text'], '自定义全局命中 +10')

        st = {'_last': {'A': {'-101': {'date': '2026-11-04', 'confirm': 'success'}}}}
        with patch.object(state, 'save_state'):
            state.record_last(st, 'A', '-102', {'confirm': 'success', 'replies': ['ok']}, mark_date='2026-11-04', today='2026-11-04')
        self.assertIn('-101', st['_last']['A'])
        self.assertIn('-102', st['_last']['A'])

    def test_blocked_systems_skips_siblings_after_unconfirmed_send(self):
        members = [
            {'chat': '-101', 'share_group': 'sys1'},
            {'chat': '-102', 'share_group': 'sys1'},
            {'chat': '-103', 'share_group': 'sys2'},
        ]
        st = {'_last': {'A': {'-101': {'date': '2026-11-04', 'confirm': 'unconfirmed', 'sent': True}}}}
        blocked = runner._blocked_systems(members, st, 'A', '2026-11-04')
        self.assertIn('sys1', blocked)
        self.assertNotIn('sys2', blocked)


class AuditFixGuiTests(unittest.TestCase):
    def setUp(self):
        (ROOT / 'audit').mkdir(exist_ok=True)
        self.directory = tempfile.TemporaryDirectory(prefix='regression-audit-', dir=ROOT / 'audit')
        self.root = Path(self.directory.name)
        self.profiles = self.root / 'data' / 'web_profiles'
        self.profiles.mkdir(parents=True)
        self.patches = [
            patch.object(config, 'CONFIG_PATH', self.root / 'config.yaml'),
            patch.object(gui, 'CONFIG_PATH', self.root / 'config.yaml'),
            patch.object(gui, 'BASE_DIR', self.root),
            patch.object(scheduler, 'BASE_DIR', self.root),
            patch.object(scheduler, 'STATE_PATH', self.root / 'data' / 'schedule-state.json'),
            patch.object(notifications, 'SECRETS_PATH', self.root / 'data' / 'secrets.json'),
            patch.object(notifications, 'STATUS_PATH', self.root / 'data' / 'notification-status.json'),
            patch.object(state, 'STATE_PATH', self.root / 'state.json'),
            patch.object(web_engine, 'PROFILES_DIR', self.profiles),
            patch.dict(os.environ, {'SERVERCHAN_SENDKEY': '', 'CHECKIN_GUI_TOKEN': ''}),
        ]
        for p in self.patches:
            p.start()
        self.orig_bot = gui.BOT.copy()
        self.orig_listen = gui._RUNTIME_LISTEN.copy()
        gui.BOT.update(busy=False, scanning=False, job=None, login_process=None, task=None, cancel_requested=False)
        gui._RUNTIME_LISTEN.update(host='127.0.0.1', token='')
        self.client = gui.app.test_client()

    def tearDown(self):
        gui.BOT.clear(); gui.BOT.update(self.orig_bot)
        gui._RUNTIME_LISTEN.clear(); gui._RUNTIME_LISTEN.update(self.orig_listen)
        for p in reversed(self.patches):
            p.stop()
        self.directory.cleanup()

    def test_gui_token_hidden_from_api_config_preserved_and_nonlocal_cannot_clear(self):
        cfg = {'gui_token': 'secret-token', 'accounts': [{'name': 'A', 'groups': [{'chat': '-101', 'message': '签到'}]}]}
        config.save_config(cfg)
        resp = self.client.get('/api/config', headers={'X-Token': 'secret-token'})
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn('gui_token', resp.json)
        self.assertEqual(resp.headers.get('X-Content-Type-Options'), 'nosniff')
        self.assertEqual(resp.headers.get('Cache-Control'), 'no-store')

        # Saving config without gui_token preserves existing gui_token
        payload = {k: v for k, v in resp.json.items() if k != '_revision'}
        save_resp = self.client.post('/api/config', headers={'X-Token': 'secret-token'}, json={'cfg': payload, 'revision': resp.json['_revision']})
        self.assertEqual(save_resp.status_code, 200)
        self.assertEqual(config.load_config().get('gui_token'), 'secret-token')

        # Non-local listen rejects clearing gui_token
        gui._RUNTIME_LISTEN['host'] = '0.0.0.0'
        clear_resp = self.client.post('/api/config', headers={'X-Token': 'secret-token'}, json={'cfg': {**payload, 'gui_token': ''}})
        self.assertEqual(clear_resp.status_code, 400)

    def test_delete_and_swap_rename_scheduled_accounts(self):
        cfg = {
            'accounts': [
                {'name': 'A', 'groups': [{'chat': '-101', 'message': '签到'}]},
                {'name': 'B', 'groups': [{'chat': '-102', 'message': '签到'}]},
            ],
            'schedule': {**scheduler.DEFAULTS, 'enabled': True, 'accounts': ['A', 'B']},
        }
        config.save_config(cfg)
        (self.profiles / 'A').mkdir(); (self.profiles / 'A' / 'tag.txt').write_text('fromA', encoding='utf-8')
        (self.profiles / 'B').mkdir(); (self.profiles / 'B' / 'tag.txt').write_text('fromB', encoding='utf-8')
        state.save_state({'A': {'-101': '2026-11-04'}, 'B': {'-102': '2026-11-04'}})

        # Swap rename A <-> B
        swapped = copy.deepcopy(cfg)
        swapped['accounts'][0]['name'] = 'B'
        swapped['accounts'][1]['name'] = 'A'
        resp = self.client.post('/api/config', json={'cfg': swapped, 'renames': {'A': 'B', 'B': 'A'}})
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        self.assertEqual((self.profiles / 'B' / 'tag.txt').read_text(encoding='utf-8'), 'fromA')
        self.assertEqual((self.profiles / 'A' / 'tag.txt').read_text(encoding='utf-8'), 'fromB')
        st = state.load_state()
        self.assertIn('-101', st['B'])
        self.assertIn('-102', st['A'])

        # Delete scheduled account B should succeed and clean schedule.accounts
        del_resp = self.client.post('/api/delete_account', json={'account': 'B'})
        self.assertEqual(del_resp.status_code, 200, del_resp.get_data(as_text=True))
        saved_cfg = config.load_config()
        self.assertEqual(saved_cfg['schedule']['accounts'], ['A'])

    def test_corrupted_secrets_json_can_be_repaired_via_automation_api(self):
        config.save_config({'accounts': [{'name': 'A', 'groups': [{'chat': '-101', 'message': '签到'}]}]})
        notifications.SECRETS_PATH.parent.mkdir(parents=True, exist_ok=True)
        notifications.SECRETS_PATH.write_text('{corrupted', encoding='utf-8')
        resp = self.client.post('/api/automation', json={'sendkey': KEY, 'notifications': {'enabled': True, 'only_on_issues': False}})
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        self.assertEqual(notifications.get_key(), KEY)

    def test_favicon_and_slim_status_actions_payload(self):
        import core.timeutil as timeutil
        cfg = {'accounts': [{'name': 'A', 'groups': [{'chat': '-101', 'message': '签到'}]}]}
        config.save_config(cfg)
        today = f'{timeutil.now(cfg):%Y-%m-%d}'
        state.save_state({
            '_last': {
                'A': {
                    '-101': {
                        'date': today,
                        'confirm': 'unconfirmed',
                        'sent': True,
                        'replies': ['duplicate reply'],
                        'evidence': [{'mid': '1002', 't': 'duplicate evidence', 'source': 'reply_to_sent', 'rm': '1001', 'btns': ['A'], 'out': False, 'peer': '-101'}],
                        'actions': [{
                            'method': 'text',
                            'sent': True,
                            'confirm': 'unconfirmed',
                            'sent_mids': ['1001'],
                            'confirmed_by_reply': True,
                            'boundary': {'peer': '-101', 'cutoff': 1700000000, 'seen': [str(i) for i in range(50)]},
                            'replies': ['duplicate reply'],
                            'evidence': [{'mid': '1002', 't': 'duplicate evidence'}],
                        }],
                    }
                }
            }
        })
        fav = self.client.get('/favicon.ico')
        self.assertIn(fav.status_code, (200, 204))
        resp = self.client.get('/api/status')
        self.assertNotIn(b'\\u', resp.data)
        status = resp.json
        group = status['accounts'][0]['groups'][0]
        self.assertTrue(group['can_recheck'])
        self.assertEqual(group['evidence'], [{'mid': '1002', 't': 'duplicate evidence', 'source': 'reply_to_sent', 'rm': '1001'}])
        action = group['actions'][0]
        self.assertNotIn('boundary', action)
        self.assertNotIn('replies', action)
        self.assertNotIn('evidence', action)
        self.assertTrue(action['confirmed_by_reply'])
        self.assertEqual(action['sent_mids'], ['1001'])

    def test_send_group_actions_retries_once_on_open_timeout_before_sending(self):
        from contextlib import asynccontextmanager
        import core.runner as runner

        class FlakyOpenEngine:
            def __init__(self):
                self.opens = 0
                self.sends = 0
                self.last_draft_warning = None
                self.last_sent_mids = set()
                self.last_replied = ['签到成功']
                self.last_evidence = [{'mid': '2', 't': '签到成功', 'source': 'private_chat'}]
                self.last_allowed_mids = ['1']
                self._expected_peer = '701'
                self._last_actual_peer = '701'
                self.send_started_at = 1.0
                self.send_started_monotonic = 1.0
                self.before_send = None
                self._next_send_at = 0

            async def open_chat(self, *args, **kwargs):
                self.opens += 1
                if self.opens == 1:
                    raise RuntimeError('打开 Target 超过 45 秒；未发送，请检查会话是否仍可访问')

            async def assert_destination(self): pass
            async def opened_chat_info(self): return {'pid': '701', 'title': 'Target'}
            @asynccontextmanager
            async def preserve_draft(self): yield
            async def snapshot_incoming(self, mode='after'): return {'peer': '701', 'cutoff': 1.0, 'seen': []}
            async def send_text(self, text):
                self.sends += 1
                self.last_sent_mids.add('1')
            async def watch_new_incoming(self, *args, **kwargs): return ['签到成功']

        eng = FlakyOpenEngine()
        cfg = {'reply_wait_seconds': 1}
        group = {'chat': '701', 'title': 'Target', 'method': 'text', 'message': '签到'}
        with patch.object(runner.asyncio, 'sleep', AsyncMock()):
            title, actions, err, mode = asyncio.run(runner._send_group_actions(eng, {}, 'A', group, cfg, 0, 0, today='2026-11-23'))
        self.assertEqual(eng.opens, 2)
        self.assertEqual(eng.sends, 1)
        self.assertIsNone(err)
        self.assertEqual(actions[0]['confirm'], 'success')

    def test_already_signed_verdict_mention_source_and_button_runner_wiring(self):
        self.assertEqual(logic.judge_replies({}, {}, ['您今天已经签到过了'], ['您今天已经签到过了']), 'success')
        self.assertEqual(logic.judge_replies({}, {}, ['签到失败，您今天已经签到过了'], ['签到失败，您今天已经签到过了']), 'success')
        self.assertEqual(logic.judge_replies({}, {}, ['签到失败，积分不足'], ['签到失败，积分不足']), 'failed')

        mention_res = runner._result({'chat': '-101', 'title': '示例群'}, [{
            'ok': True, 'clicked': False, 'with_image': False, 'sent': True, 'confirm': 'success', 'error': None,
            'replies': ['示例用户 签到成功'], 'trusted_replies': ['示例用户 签到成功'],
            'evidence': [{'source': 'mention_self', 'mid': '88', 't': '示例用户 签到成功'}],
        }])
        self.assertEqual(mention_res['source'], 'mention_self')

        from contextlib import asynccontextmanager

        class ButtonFakeEngine:
            def __init__(self):
                self.earliest_seen = None
                self.watch_kwargs = []
                self.last_draft_warning = None
                self.last_sent_mids = set()
                self.last_replied = ['签到成功 +10']
                self.last_evidence = [{'source': 'button_popup', 'mid': '40', 't': '签到成功 +10'}]
                self.last_allowed_mids = []
                self._expected_peer = '701'
                self._last_actual_peer = '701'
                self.send_started_at = 1.0
                self.send_started_monotonic = 1.0
                self.before_send = None
                self._next_send_at = 0
                self._active_mids = set()
                self._last_button_mid = '40'
                self._last_button_initial_text = '请选择'
                self._last_button_initial_buttons = ['确认签到']

            async def open_chat(self, *args, require_composer=True, **kwargs): pass
            async def assert_destination(self): pass
            async def opened_chat_info(self): return {'pid': '701', 'title': '示例机器人'}
            @asynccontextmanager
            async def preserve_draft(self): yield
            async def snapshot_incoming(self, mode='after'): return {'peer': '701', 'cutoff': 1.0, 'seen': []}
            async def click_existing_button(self, label, reference, earliest=0):
                self.earliest_seen = earliest
                if self.before_send:
                    await self.before_send()
                return '40'
            async def send_text(self, text):
                if self.before_send:
                    await self.before_send()
                self.last_sent_mids.add('10')
            async def click_button(self, label):
                self._active_mids.add('40')
                return True
            async def watch_new_incoming(self, known, seconds, mode='after', idle_after=2.0, sent_mids=None, started_at=None, is_complete=None, *, watch_button_mid=None, button_initial_text=None, button_initial_buttons=None, check_popup=False):
                self.watch_kwargs.append({
                    'sent_mids': list(sent_mids or []),
                    'watch_button_mid': watch_button_mid,
                    'button_initial_buttons': button_initial_buttons,
                    'check_popup': check_popup,
                })
                self.last_allowed_mids = sorted(sent_mids or [])
                return ['签到成功 +10']

        eng = ButtonFakeEngine()
        _, acts_btn, _, _ = asyncio.run(runner._send_group_actions(
            eng, {}, 'A', {'chat': '701', 'method': 'button_only', 'click_button': '点我签到'}, {'reply_wait_seconds': 0.05}, 0, 0, today='2026-11-25'))
        self.assertEqual(eng.earliest_seen, 0)
        self.assertEqual(acts_btn[0]['confirm'], 'success')
        self.assertTrue(eng.watch_kwargs[0]['check_popup'])

        eng2 = ButtonFakeEngine()
        _, acts_cmd, _, _ = asyncio.run(runner._send_group_actions(
            eng2, {}, 'A', {'chat': '701', 'method': 'command_button', 'message': '/checkin', 'click_button': '确认签到'}, {'reply_wait_seconds': 0.05}, 0, 0, today='2026-11-25'))
        self.assertEqual(acts_cmd[0]['confirm'], 'success')
        self.assertTrue(eng2.watch_kwargs[0]['check_popup'])
        self.assertEqual(eng2.watch_kwargs[0]['watch_button_mid'], '40')
        self.assertEqual(acts_cmd[0]['sent_mids'], ['10'])

        # Recheck caps live tail wait to 1.5s after collecting historical messages
        recheck_waits = []
        class RecheckEngine(ButtonFakeEngine):
            async def _scroll_chat_bottom(self): pass
            async def collect_feedback_history(self, boundary): return {'items': [], 'aliases': []}
            async def watch_new_incoming(self, known, seconds, *args, **kwargs):
                recheck_waits.append(seconds)
                self.last_allowed_mids = sorted(kwargs.get('sent_mids') or [])
                return []
        eng3 = RecheckEngine()
        prev = {'actions': [{'ok': True, 'clicked': False, 'with_image': False, 'error': None, 'confirm': 'unconfirmed', 'sent': True, 'sent_mids': ['10'], 'boundary': {'peer': '-101', 'cutoff': 1.0}}]}
        asyncio.run(runner._recheck(eng3, {'chat': '-101'}, prev, {'reply_wait_seconds': 6}))
        self.assertEqual(recheck_waits, [1.5])

        # Temporary-only sent_mids triggers recover_sent_mids during _recheck and missing_message_id hint
        temp_res = {'confirm': 'unconfirmed', 'actions': [{'ok': True, 'clicked': False, 'with_image': False, 'error': None, 'confirm': 'unconfirmed', 'sent': True, 'sent_mids': ['100.0001'], 'boundary': {'peer': '-101', 'cutoff': 1.0}}]}
        self.assertEqual(logic.feedback_hint({}, {}, temp_res)['code'], 'missing_message_id')

        class TempRecoverEngine(RecheckEngine):
            def __init__(self):
                super().__init__()
                self.recover_calls = 0
            async def recover_sent_mids(self, boundary, text=None):
                self.recover_calls += 1
                return {'sent_mids': ['501'], 'evidence': [], 'source': 'own_outgoing_history'}
        eng4 = TempRecoverEngine()
        rechecked = asyncio.run(runner._recheck(eng4, {'chat': '-101', 'message': '签到'}, temp_res, {'reply_wait_seconds': 6}))
        self.assertEqual(eng4.recover_calls, 1)
        self.assertIn('501', rechecked['actions'][0]['sent_mids'])

        # /api/status caps unrelated evidence items and truncates long spam text
        long_spam = '广' * 500
        slimmed = gui._slim_evidence([
            {'mid': '1', 't': '签到成功', 'source': 'reply_to_sent', 'rm': '10'},
            *[{'mid': str(i + 2), 't': long_spam, 'source': 'unrelated', 'rm': None} for i in range(15)],
        ])
        self.assertEqual(len(slimmed), 13)
        self.assertEqual(slimmed[0]['t'], '签到成功')
        self.assertLessEqual(len(slimmed[-1]['t']), 300)
        self.assertTrue(slimmed[-1]['t'].endswith('…'))


if __name__ == '__main__':
    unittest.main()

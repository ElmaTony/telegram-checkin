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
from unittest.mock import patch
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
        status = self.client.get('/api/status').json
        group = status['accounts'][0]['groups'][0]
        self.assertTrue(group['can_recheck'])
        action = group['actions'][0]
        self.assertNotIn('boundary', action)
        self.assertNotIn('replies', action)
        self.assertNotIn('evidence', action)
        self.assertTrue(action['confirmed_by_reply'])
        self.assertEqual(action['sent_mids'], ['1001'])


if __name__ == '__main__':
    unittest.main()

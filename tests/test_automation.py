"""Scheduler, notification, privacy and release invariants. No real deliveries."""
import asyncio
import copy
import json
import os
import tempfile
import unittest
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch
from zipfile import ZipFile

import gui
from core import config, scheduler, notifications, state, runner, timeutil
from tools import package_release, maintenance

ROOT = Path(__file__).resolve().parents[1]
CFG = {'accounts': [{'name': 'A', 'groups': [{'chat': '-101', 'title': '示例群', 'message': '签到'}]},
                    {'name': 'B', 'groups': [{'chat': '-102', 'title': '示例论坛', 'topic': '讨论区', 'message': '签到'}]}],
       'schedule': {**scheduler.DEFAULTS, 'enabled': True}, 'notifications': {'enabled': True}}
KEY = 'SCT' + 'x'*24
CURRENT = datetime(2026, 11, 4, 1, 0, tzinfo=timezone.utc)  # 09:00 Hong Kong


class AutomationTests(unittest.TestCase):
    def setUp(self):
        (ROOT/'audit').mkdir(exist_ok=True)
        self.directory = tempfile.TemporaryDirectory(prefix='regression-automation-', dir=ROOT/'audit')
        self.root = Path(self.directory.name)
        self.patches = [patch.object(config, 'CONFIG_PATH', self.root/'config.yaml'),
                        patch.object(gui, 'CONFIG_PATH', self.root/'config.yaml'),
                        patch.object(gui, 'BASE_DIR', self.root),
                        patch.object(scheduler, 'BASE_DIR', self.root),
                        patch.object(scheduler, 'STATE_PATH', self.root/'data'/'schedule-state.json'),
                        patch.object(notifications, 'SECRETS_PATH', self.root/'data'/'secrets.json'),
                        patch.object(notifications, 'STATUS_PATH', self.root/'data'/'notification-status.json'),
                        patch.object(state, 'STATE_PATH', self.root/'state.json'),
                        patch.dict(os.environ, {'SERVERCHAN_SENDKEY': ''})]
        for item in self.patches:
            item.start()
        self.original_bot = gui.BOT.copy()
        gui.BOT.update(busy=False, scanning=False, job=None, login_process=None, task=None, cancel_requested=False)
        gui.TRIGGER['event'].clear()
        config.save_config(copy.deepcopy(CFG))
        self.client = gui.app.test_client()

    def tearDown(self):
        gui.BOT.clear(); gui.BOT.update(self.original_bot)
        gui.TRIGGER['event'].clear()
        for item in reversed(self.patches):
            item.stop()
        self.directory.cleanup()

    def test_schedule_validation_rejects_invalid_time_timezone_scope_and_types(self):
        for key, value in [('time','24:00'),('time','9:00'),('timezone','Not/AZone'),('enabled','true'),('accounts',['missing']),('accounts',['A','A'])]:
            with self.subTest(key=key,value=value):
                cfg=copy.deepcopy(CFG);cfg['schedule'][key]=value
                self.assertIsNotNone(config.validate_config(cfg))
        self.assertIsNone(config.validate_config(CFG))
        self.assertIsNotNone(config.validate_config({'accounts':[{'name':[],'groups':[]}]}))

    def test_timezone_calendar_does_not_depend_on_host_timezone(self):
        self.assertTrue(scheduler.is_due(CFG,{},CURRENT))
        self.assertFalse(scheduler.is_due(CFG,{},CURRENT.replace(hour=0,minute=59)))
        cfg=copy.deepcopy(CFG);cfg['schedule']['timezone']='UTC'
        self.assertFalse(scheduler.is_due(cfg,{},CURRENT))
        self.assertEqual(timeutil.now(cfg).tzinfo.key,'UTC')

    def test_catch_up_and_target_minute(self):
        cfg=copy.deepcopy(CFG);cfg['schedule']['catch_up']=False
        self.assertTrue(scheduler.is_due(cfg,{},CURRENT.replace(second=59)))
        self.assertFalse(scheduler.is_due(cfg,{},CURRENT.replace(minute=1)))
        self.assertTrue(scheduler.is_due(CFG,{},CURRENT.replace(hour=10)))

    def test_claim_is_durable_once_per_day_across_new_scheduler_instances(self):
        self.assertTrue(scheduler.claim(CFG,'job1',CURRENT))
        self.assertFalse(scheduler.claim(CFG,'job2',CURRENT.replace(hour=2)))
        runtime=scheduler.read_runtime()
        self.assertEqual(runtime['job_id'],'job1')
        self.assertFalse(scheduler.is_due(CFG,runtime,CURRENT))
        self.assertTrue(scheduler.is_due(CFG,runtime,CURRENT.replace(day=5)))

    def test_future_claim_after_clock_rollback_cannot_repeat(self):
        self.assertFalse(scheduler.is_due(CFG,{'last_date':'2026-11-05'},CURRENT))

    def test_dst_fallback_is_still_one_calendar_day(self):
        cfg=copy.deepcopy(CFG);cfg['schedule'].update(time='01:30',timezone='America/New_York')
        self.assertTrue(scheduler.is_due(cfg,{},datetime(2026,11,1,5,30,tzinfo=timezone.utc)))
        self.assertFalse(scheduler.is_due(cfg,{'last_date':'2026-11-01'},datetime(2026,11,1,6,30,tzinfo=timezone.utc)))

    def test_corrupted_scheduler_state_fails_closed(self):
        scheduler.STATE_PATH.parent.mkdir(parents=True)
        scheduler.STATE_PATH.write_text('{broken',encoding='utf-8')
        with self.assertRaises(RuntimeError):scheduler.claim(CFG,'job',CURRENT)
        self.assertIn('停止自动执行',scheduler.describe(CFG,CURRENT)['error'])

    def test_busy_manual_job_defers_schedule_without_claim(self):
        gui.BOT['busy']=True
        self.assertFalse(asyncio.run(gui._submit_scheduled(CFG,CURRENT)))
        self.assertFalse(scheduler.STATE_PATH.exists())
        self.assertFalse(gui.TRIGGER['event'].is_set())

    def test_scheduler_claim_precedes_trigger_and_reserves_correct_scope(self):
        cfg=copy.deepcopy(CFG);cfg['schedule']['accounts']=['B']
        real_write=scheduler.write_runtime
        def durable(value):
            self.assertFalse(gui.TRIGGER['event'].is_set())
            real_write(value)
        with patch.object(scheduler,'write_runtime',side_effect=durable):
            self.assertTrue(asyncio.run(gui._submit_scheduled(cfg,CURRENT)))
        self.assertTrue(gui.BOT['job']['scheduled'])
        self.assertFalse(gui.BOT['job']['show_browser'])
        self.assertEqual(gui.TRIGGER['scheduled_accounts'],['B'])
        self.assertEqual(scheduler.read_runtime()['job_id'],gui.BOT['job']['id'])
        gui._release(gui.BOT['job']['id'],'cancelled')
        self.assertEqual(scheduler.read_runtime()['status'],'cancelled')

    def test_failed_durable_claim_never_enqueues_and_releases_gui(self):
        with patch.object(scheduler,'write_runtime',side_effect=OSError('disk full')):
            with self.assertRaises(OSError):asyncio.run(gui._submit_scheduled(CFG,CURRENT))
        self.assertFalse(gui.TRIGGER['event'].is_set())
        self.assertFalse(gui.BOT['busy'])

    def test_tick_reports_invalid_config_without_submitting(self):
        cfg=copy.deepcopy(CFG);cfg['schedule']['time']='bad'
        submit=AsyncMock()
        service=scheduler.DailyScheduler(lambda:cfg,submit)
        asyncio.run(service.tick())
        submit.assert_not_called();self.assertIn('HH:MM',service.error)

    def test_restarted_service_marks_interrupted_without_repeating_day(self):
        scheduler.claim(CFG,'old-job',CURRENT)
        submit=AsyncMock()
        service=scheduler.DailyScheduler(lambda:CFG,submit)
        async def stop_after_tick(*args):raise asyncio.CancelledError()
        with patch.object(scheduler,'now',return_value=CURRENT),patch.object(scheduler.asyncio,'sleep',side_effect=stop_after_tick):
            with self.assertRaises(asyncio.CancelledError):asyncio.run(service.run())
        self.assertEqual(scheduler.read_runtime()['status'],'interrupted')
        submit.assert_not_called()

    def test_corrupt_startup_remains_visible_and_never_submits(self):
        scheduler.STATE_PATH.parent.mkdir(parents=True)
        scheduler.STATE_PATH.write_text('broken',encoding='utf-8')
        service=scheduler.DailyScheduler(lambda:CFG,AsyncMock())
        async def stop(*args):raise asyncio.CancelledError()
        with patch.object(scheduler.asyncio,'sleep',side_effect=stop):
            with self.assertRaises(asyncio.CancelledError):asyncio.run(service.run())
        self.assertIn('无法读取',service.error)
        service.submit.assert_not_called()

    def test_key_is_private_and_environment_takes_precedence(self):
        notifications.save_key(KEY)
        self.assertEqual(notifications.get_key(),KEY)
        with patch.dict(os.environ,{'SERVERCHAN_SENDKEY':'SCT'+'y'*24}):
            self.assertEqual(notifications.get_key(),'SCT'+'y'*24)
            self.assertEqual(notifications.key_status()['source'],'environment')
        for endpoint in ('/api/config','/api/automation','/api/status'):
            response=self.client.get(endpoint)
            self.assertEqual(response.status_code,200)
            self.assertNotIn(KEY,response.get_data(as_text=True))

    def test_automation_save_preserves_accounts_and_hides_key(self):
        original=config.load_config()['accounts']
        data=self.client.get('/api/automation').json
        result=self.client.post('/api/automation',json={'schedule':{**data['settings'],'time':'00:10','accounts':['B']},'notifications':{'enabled':True},'sendkey':KEY,'revision':data['revision']})
        self.assertEqual(result.status_code,200)
        self.assertTrue(result.json['key']['configured'])
        self.assertNotIn(KEY,result.get_data(as_text=True))
        self.assertEqual(config.load_config()['accounts'],original)
        self.assertNotIn(KEY,config.CONFIG_PATH.read_text(encoding='utf-8'))

    def test_stale_revision_busy_and_invalid_secret_do_not_change_config(self):
        original=config.CONFIG_PATH.read_text(encoding='utf-8')
        self.assertEqual(self.client.post('/api/automation',json={'revision':'stale','sendkey':KEY}).status_code,409)
        gui.BOT['busy']=True
        self.assertEqual(self.client.post('/api/automation',json={'sendkey':KEY}).status_code,409)
        gui.BOT['busy']=False
        self.assertEqual(self.client.post('/api/automation',json={'sendkey':'https://example.invalid/secret'}).status_code,400)
        self.assertEqual(config.CONFIG_PATH.read_text(encoding='utf-8'),original)
        self.assertFalse(notifications.SECRETS_PATH.exists())

    def test_config_failure_rolls_back_private_key(self):
        original='SCT'+'z'*24;notifications.save_key(original)
        with patch.object(gui,'save_config',side_effect=OSError('disk full')):
            response=self.client.post('/api/automation',json={'sendkey':KEY})
        self.assertEqual(response.status_code,503)
        self.assertEqual(notifications.get_key(),original)

    def test_notification_http_error_does_not_leak_url_or_secret(self):
        notifications.save_key(KEY)
        error=urllib.error.HTTPError('https://sctapi.ftqq.com/'+KEY+'.send',429,'quota',{},None)
        with patch.object(notifications.urllib.request,'urlopen',side_effect=error):
            result=notifications.send('title','body')
        self.assertFalse(result['ok'])
        self.assertNotIn(KEY,json.dumps(result))
        self.assertNotIn(KEY,notifications.STATUS_PATH.read_text(encoding='utf-8'))

    def test_serverchan_checks_response_code_and_sends_form_once(self):
        class Response:
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def read(self,limit):return b'{"code":0}'
        with patch.object(notifications.urllib.request,'urlopen',return_value=Response()) as post:
            result=notifications._post(KEY,'Summary','Markdown\nbody')
        self.assertTrue(result['ok']);self.assertEqual(post.call_count,1)
        req=post.call_args.args[0]
        self.assertEqual(req.get_method(),'POST');self.assertIn(b'desp=',req.data)

    def test_test_endpoint_does_not_trigger_checkin_and_is_rate_limited(self):
        notifications.save_key(KEY)
        with patch.object(notifications,'_post',return_value={'ok':True,'message':'accepted','error':''}) as post:
            self.assertEqual(self.client.post('/api/notifications/test',json={}).status_code,200)
            self.assertEqual(self.client.post('/api/notifications/test',json={}).status_code,409)
        self.assertEqual(post.call_count,1)
        self.assertFalse(gui.TRIGGER['event'].is_set())

    def test_notification_summarizes_only_requested_scope_and_keeps_unconfirmed(self):
        day=f'{timeutil.now(CFG):%Y-%m-%d}'
        runtime={'_last':{'A':{'-101':{'date':day,'confirm':'success'}},'B':{'-102':{'date':day,'confirm':'unconfirmed'}}}}
        _,description,issues=notifications.round_message(CFG,runtime,accounts=['B'])
        self.assertTrue(issues);self.assertIn('待核对',description);self.assertNotIn('示例群',description)

    def test_only_on_issues_does_not_send_success_or_retry_network_errors(self):
        cfg=copy.deepcopy(CFG);cfg['notifications']['only_on_issues']=True
        day=f'{timeutil.now(cfg):%Y-%m-%d}'
        runtime={'A':{'-101':day},'B':{'-102':day}}
        with patch.object(notifications,'send') as post:
            result=asyncio.run(notifications.notify_round(cfg,runtime))
        self.assertTrue(result['skipped']);post.assert_not_called()

    def test_selected_schedule_preserves_other_accounts_state(self):
        runtime={'A':{'-101':'2026-11-03'}}
        state.save_state(runtime)
        async def worker(account,*args):return {}
        with patch.object(runner,'BASE_DIR',self.root),patch.object(runner,'_web_account_worker',side_effect=worker) as work,patch.object(runner,'save_capture'):
            asyncio.run(runner.run_web_round(CFG,runtime,accounts=['B']))
        self.assertEqual(work.call_args.args[0]['name'],'B')
        self.assertIn('A',runtime)

    def test_release_allowlist_excludes_every_runtime_directory(self):
        (self.root/'VERSION').write_text('1.2.3',encoding='utf-8')
        (self.root/'checkin.py').write_text('print("safe")',encoding='utf-8')
        (self.root/'config.yaml').write_text('private config',encoding='utf-8')
        for directory in ('data','logs','audit','history'):
            (self.root/directory).mkdir(exist_ok=True)
            (self.root/directory/'private.txt').write_text('private',encoding='utf-8')
        path=package_release.build(self.root)
        with ZipFile(path) as archive:
            names=archive.namelist()
            self.assertTrue(any(name.endswith('MANIFEST.json') for name in names))
            self.assertFalse(any('/data/' in name or name.endswith('/config.yaml') or '/audit/' in name for name in names))
        self.assertTrue(path.with_suffix('.zip.sha256').exists())
        original=path.read_bytes()
        self.assertEqual(package_release.build(self.root).read_bytes(),original)

    def test_release_secret_scan_fails_closed(self):
        (self.root/'checkin.py').write_text('key="'+KEY+'"',encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'密钥'):package_release.source_files(self.root)

    def test_release_includes_reviewed_readme_images_but_not_other_screenshots(self):
        assets=self.root/'docs'/'assets';assets.mkdir(parents=True)
        for name in ('workbench.png','automation.png','unreviewed.png'):
            (assets/name).write_bytes(b'PNG fixture')
        included={path.relative_to(self.root).as_posix() for path in package_release.source_files(self.root)}
        self.assertIn('docs/assets/workbench.png',included)
        self.assertIn('docs/assets/automation.png',included)
        self.assertNotIn('docs/assets/unreviewed.png',included)

    def test_cleanup_refuses_data_dependencies_or_escape(self):
        for path in (self.root,self.root/'data'/'anything',self.root/'.audit-venv'/'cache',self.root.parent/'outside'):
            with self.subTest(path=path):
                with self.assertRaises(ValueError):maintenance.checked_target(self.root,path)
        expected=self.root/'core'/'__pycache__'
        self.assertEqual(maintenance.checked_target(self.root,expected),expected.resolve())


if __name__=='__main__':unittest.main()

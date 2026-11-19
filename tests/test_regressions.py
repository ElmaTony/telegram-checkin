"""Regression coverage for bugs reproduced in the real three-account round."""
import asyncio
from contextlib import asynccontextmanager
import copy
import csv
import json
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import yaml

import gui
import core.config as config
import core.state as state
import core.runner as runner
import core.history as history
import web_engine
from core.locks import FileLock
from core.logic import judge_replies, pick_result_reply, feedback_hint

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = {'accounts': [{'name': 'Audit', 'groups': [{'chat': '-101', 'title': 'Target', 'method': 'text', 'message': '签到', 'topic': '每日签到', 'folder': '签到', 'reply_mode': 'after', 'primary': True, 'share_group': 'Shared'}]}]}


class ResultJudgementTests(unittest.TestCase):
    prompt = '资料校验出现异常，是否确认继续打卡？'

    def test_confirmation_question_is_not_a_failed_result(self):
        self.assertEqual(judge_replies({}, {}, [self.prompt], [self.prompt]), 'unconfirmed')

    def test_intermediate_error_does_not_poison_later_success(self):
        replies = [self.prompt, '打卡成功']
        self.assertEqual(judge_replies({}, {}, replies, replies), 'success')
        self.assertEqual(pick_result_reply({}, {}, replies, 'success'), '打卡成功')

    def test_explicit_failure_with_retry_question_remains_failed(self):
        text = '打卡失败：余额不足。是否继续打卡？'
        self.assertEqual(judge_replies({}, {}, [text], [text]), 'failed')

    def test_custom_group_keyword_still_overrides_prompt_filter(self):
        self.assertEqual(judge_replies({'fail_keywords':['异常']}, {}, [self.prompt], [self.prompt]), 'failed')
        self.assertEqual(pick_result_reply({'fail_keywords':['异常']}, {}, [self.prompt,'打卡成功'], 'failed'), self.prompt)

    def test_specific_global_keyword_still_applies_to_prompt(self):
        self.assertEqual(judge_replies({}, {'fail_keywords':['资料校验']}, [self.prompt], [self.prompt]), 'failed')

    def test_global_phrase_can_span_related_messages(self):
        replies = ['本次操作', '获得奖励']
        self.assertEqual(judge_replies({}, {'success_keywords':['操作 获得']}, replies, replies), 'success')

    def pending(self, **action):
        return {'confirm':'unconfirmed','actions':[{'confirm':'unconfirmed','sent':True,
                'sent_mids':['501'],'boundary':{'peer':'-101'},'trusted_replies':['签到成功，奖励已发放'], **action}]}

    def test_rule_mismatch_is_explained_without_changing_verdict(self):
        group={'success_keywords':['签到完成，增加{n}奖励']}
        result=self.pending()
        self.assertEqual(feedback_hint(group,{},result)['code'],'rule_mismatch')
        self.assertEqual(judge_replies(group,{},[],result['actions'][0]['trusted_replies']),'unconfirmed')

    def test_rule_hint_never_uses_unrelated_messages(self):
        result=self.pending(trusted_replies=[],replies=['签到成功'])
        self.assertIsNone(feedback_hint({'success_keywords':['OLD']},{},result))

    def test_default_rules_and_confirmed_results_have_no_rule_warning(self):
        self.assertIsNone(feedback_hint({}, {}, self.pending()))
        result=self.pending();result['confirm']='success'
        self.assertIsNone(feedback_hint({'success_keywords':['OLD']},{},result))

    def test_missing_ids_and_pending_buttons_take_priority(self):
        self.assertEqual(feedback_hint({}, {}, self.pending(sent_mids=[]))['code'],'missing_message_id')
        self.assertEqual(feedback_hint({}, {}, self.pending(sent_mids=[],buttons_complete=False))['code'],'buttons_pending')

    def test_global_rule_mismatch_can_also_be_explained(self):
        self.assertEqual(feedback_hint({}, {'success_keywords':['特定完成回执']},self.pending())['code'],'rule_mismatch')

    def test_incomplete_later_action_has_priority_over_rule_hint(self):
        result=self.pending()
        result['actions'].append({**result['actions'][0],'buttons_complete':False})
        self.assertEqual(feedback_hint({'success_keywords':['OLD']},{},result)['code'],'buttons_pending')


class IsolatedFiles(unittest.TestCase):
    def setUp(self):
        (ROOT / 'audit').mkdir(exist_ok=True)
        self.directory = tempfile.TemporaryDirectory(prefix='regression-', dir=ROOT / 'audit')
        self.root = Path(self.directory.name).resolve()
        assert self.root.parent == (ROOT / 'audit').resolve()
        self.patches = [patch.object(config, 'CONFIG_PATH', self.root/'config.yaml'),
                        patch.object(gui, 'CONFIG_PATH', self.root/'config.yaml'),
                        patch.object(gui, 'BASE_DIR', self.root),
                        patch.object(state, 'STATE_PATH', self.root/'state.json'),
                        patch.object(web_engine, 'PROFILES_DIR', self.root/'data'/'web_profiles'),
                        patch.object(gui, 'HISTORY_DIR', self.root/'history'),
                        patch.object(gui, 'LOG_PATH', self.root/'checkin.log')]
        for item in self.patches: item.start()
        self.original_bot = gui.BOT.copy()
        gui.BOT.update(busy=False, scanning=False, job=None, task=None, login_process=None, cancel_requested=False)
        gui.TRIGGER['event'].clear()
        config.save_config(copy.deepcopy(FIXTURE))
        self.client = gui.app.test_client()

    def tearDown(self):
        gui.BOT.clear()
        gui.BOT.update(self.original_bot)
        gui.TRIGGER['event'].clear()
        for item in reversed(self.patches): item.stop()
        self.directory.cleanup()

    def test_queue_is_atomic_and_busy_rejects_another_trigger(self):
        accepted = self.client.post('/api/checkin_now', json={'account': 'Audit', 'chats': ['-101']})
        self.assertEqual(accepted.status_code, 202)
        first_id = accepted.json['job_id']
        second = self.client.post('/api/probe', json={})
        self.assertEqual(second.status_code, 409)
        self.assertEqual(gui.BOT['job']['id'], first_id)
        self.assertEqual(gui.TRIGGER['mode'], 'checkin')
        self.assertEqual(gui.TRIGGER['chats'], ['-101'])

    def test_new_job_clears_old_progress_but_rejected_job_keeps_current_progress(self):
        with patch.dict(gui.ACTIVITY,{'act':'Previous fixture','accounts':{'Previous':{'stage':'已完成'}}},clear=True):
            before=copy.deepcopy(state.load_state())
            accepted=self.client.post('/api/checkin_now',json={'account':'Audit','chats':['-101']})
            self.assertEqual(accepted.status_code,202)
            status=self.client.get('/api/status').json
            self.assertEqual(status['job']['id'],accepted.json['job_id'])
            self.assertEqual(status['progress'],{})
            self.assertEqual(status['activity'],'')
            self.assertEqual(state.load_state(),before)
            current={'act':'Current fixture','accounts':{'Audit':{'stage':'等待反馈','chat':'-101'}}}
            gui.ACTIVITY.update(copy.deepcopy(current))
            self.assertEqual(self.client.post('/api/probe',json={}).status_code,409)
            self.assertEqual(gui.ACTIVITY,current)

    def test_status_preserves_every_method_in_a_multi_action_group(self):
        cfg=copy.deepcopy(FIXTURE)
        cfg['accounts'][0]['groups'][0]['actions']=[{'method':'text','message':'签到'},{'method':'image_text','message':'打卡','image':'images/example.png'}]
        config.save_config(cfg)
        row=self.client.get('/api/status').json['accounts'][0]['groups'][0]
        self.assertEqual(row['methods'],['text','image_text'])
        self.assertEqual(row['action_count'],2)
        self.assertEqual(row['topic'],'每日签到')

    def test_cancel_queued_job_does_not_execute_it(self):
        self.client.post('/api/checkin_now', json={})
        self.assertEqual(self.client.post('/api/cancel', json={}).status_code, 200)
        self.assertFalse(gui.TRIGGER['event'].is_set())
        self.assertFalse(gui.BOT['busy'])
        self.assertEqual(gui.BOT['job']['status'], 'cancelled')

    def test_topics_cache_is_account_scoped_and_returns_exact_ids(self):
        cached={'is_forum':True,'topics':[{'id':'4294967297','title':'讨论区'}],'chat':'-101','checked_at':time.time()}
        state.save_state({'_chat_cache':{'Audit':{'topics':{'-101':cached}}}})
        with patch.object(gui,'call') as call:
            response=self.client.get('/api/topics?account=Audit&chat=-101')
        self.assertEqual(response.status_code,200)
        self.assertTrue(response.json['cached'])
        self.assertEqual(response.json['topics'][0]['id'],'4294967297')
        call.assert_not_called()
        self.assertEqual(self.client.get('/api/topics?account=Unknown&chat=-101').status_code,400)
        self.assertEqual(self.client.get('/api/topics?account=Audit&chat=@not_numeric').status_code,400)

    def test_empty_legacy_dialog_cache_is_rescanned_and_does_not_poison_good_cache(self):
        state.save_state({'_chat_cache':{'Audit':{'lists':{'':[]}}}})
        dialogs=[{'id':'-201','title':'Fixture group','is_forum':False}]
        with patch.object(gui,'call',side_effect=lambda coro, **kw:asyncio.run(coro)), \
             patch.object(web_engine,'WebEngine') as engine:
            scanner=engine.return_value
            scanner.launch=AsyncMock();scanner.close=AsyncMock();scanner.list_folders=AsyncMock(return_value=[])
            scanner.list_chats=AsyncMock(side_effect=[dialogs,RuntimeError('Telegram did not load')])
            response=self.client.get('/api/dialogs?account=Audit')
            self.assertEqual(response.status_code,200)
            self.assertEqual(response.json['dialogs'],dialogs)
            self.assertFalse(response.json['cached'])
            self.assertEqual(self.client.get('/api/dialogs?account=Audit&refresh=1').status_code,409)
            again=self.client.get('/api/dialogs?account=Audit')
            self.assertTrue(again.json['cached'])
            self.assertEqual(again.json['dialogs'],dialogs)
            self.assertEqual(scanner.list_chats.await_count,2)

    def test_topic_scan_respects_running_job(self):
        self.client.post('/api/checkin_now',json={})
        response=self.client.get('/api/topics?account=Audit&chat=-101&refresh=1')
        self.assertEqual(response.status_code,409)

    def test_fresh_topic_cache_tolerates_subsecond_clock_rounding(self):
        cached={'is_forum':True,'topics':[{'id':'21','title':'讨论区'}],'chat':'-101','checked_at':time.time()+.01}
        state.save_state({'_chat_cache':{'Audit':{'topics':{'-101':cached}}}})
        with patch.object(gui,'call') as call:
            response=self.client.get('/api/topics?account=Audit&chat=-101')
        self.assertEqual(response.status_code,200)
        self.assertTrue(response.json['cached'])
        call.assert_not_called()

    def test_forum_config_requires_topic_and_valid_id(self):
        cfg=copy.deepcopy(FIXTURE);group=cfg['accounts'][0]['groups'][0]
        group.update(is_forum=True,topic='')
        self.assertIn('选择发送话题',config.validate_config(cfg))
        group.update(topic='讨论区',topic_id='21')
        self.assertIsNone(config.validate_config(cfg))
        group['topic_id']='0'
        self.assertIn('topic_id',config.validate_config(cfg))

    def test_manual_task_explicitly_defaults_to_visible_browser(self):
        cfg=copy.deepcopy(FIXTURE);cfg['web_headless']=True;config.save_config(cfg)
        with patch.object(gui,'visible_browser_available',return_value=True):
            self.assertEqual(self.client.post('/api/checkin_now',json={}).status_code,202)
            self.assertTrue(gui.BOT['job']['show_browser'])
            self.client.post('/api/cancel',json={})
            self.assertEqual(self.client.post('/api/checkin_now',json={'show_browser':False}).status_code,202)
            self.assertFalse(gui.BOT['job']['show_browser'])

    def test_manual_task_uses_background_browser_without_a_desktop(self):
        with patch.object(gui,'visible_browser_available',return_value=False):
            for data in ({},{'show_browser':True}):
                self.assertEqual(self.client.post('/api/checkin_now',json=data).status_code,202)
                self.assertFalse(gui.BOT['job']['show_browser'])
                self.client.post('/api/cancel',json={})

    def test_browser_visibility_must_be_a_boolean(self):
        response=self.client.post('/api/checkin_now',json={'show_browser':'false'})
        self.assertEqual(response.status_code,400)
        self.assertFalse(gui.BOT['busy'])

    def test_explicit_empty_account_cannot_expand_to_all_accounts(self):
        for account in ('','  '):
            response=self.client.post('/api/checkin_now',json={'account':account})
            self.assertEqual(response.status_code,400)
            self.assertFalse(gui.BOT['busy'])
            self.assertFalse(gui.TRIGGER['event'].is_set())

    def test_history_retains_unconfirmed_send_warning(self):
        result={'name':'Target','ok':True,'sent':True,'confirm':'unconfirmed','replies':[],
                'error':None,'result_text':'未能登记新消息 ID，发送状态待核对'}
        with patch.object(history,'HISTORY_DIR',self.root/'history'):
            history.append_history('Audit',result)
        path=next((self.root/'history').glob('*.csv'))
        with path.open(encoding='utf-8-sig') as stream:
            rows=list(csv.reader(stream))
        self.assertEqual(rows[1][3],'未确认')
        self.assertIn('发送状态待核对',rows[1][4])

    def test_history_does_not_call_unattributed_feedback_missing(self):
        result={'name':'Target','ok':True,'sent':True,'confirm':'unconfirmed','replies':[],
                'error':None,'evidence':[{'t':'签到成功','source':'unrelated'}]}
        with patch.object(history,'HISTORY_DIR',self.root/'history'):
            history.append_history('Audit',result)
        path=next((self.root/'history').glob('*.csv'))
        with path.open(encoding='utf-8-sig') as stream:
            rows=list(csv.reader(stream))
        self.assertIn('已捕获消息',rows[1][4])
        self.assertIn('归属',rows[1][4])
        self.assertIn('归属',history.build_report('Audit',[result]))

    def test_text_report_shows_failed_action_before_earlier_success_reply(self):
        result={'name':'Target','ok':False,'confirm':'failed','replies':['第一步签到成功'],
                'error':'第二步按钮未找到'}
        report=history.build_report('Audit',[result])
        self.assertIn('第二步按钮未找到',report)
        self.assertNotIn('第一步签到成功',report)

    def test_single_account_and_chat_request_stays_within_configured_account(self):
        cfg=copy.deepcopy(FIXTURE)
        cfg['accounts'].append({'name':'Other','groups':[{'chat':'-202'}]})
        config.save_config(cfg)
        self.assertEqual(self.client.post('/api/checkin_now',json={'account':'Audit','chats':['-202']}).status_code,400)
        self.assertFalse(gui.BOT['busy'])
        accepted=self.client.post('/api/checkin_now',json={'account':'Audit','chats':['-101']})
        self.assertEqual(accepted.status_code,202)
        self.assertEqual(gui.TRIGGER['only'],'Audit')
        self.assertEqual(gui.TRIGGER['chats'],['-101'])

    def test_legacy_result_cannot_be_rechecked_without_message_evidence(self):
        state.save_state({'_last': {'Audit': {'-101': {'date': f'{datetime.now():%Y-%m-%d}', 'confirm': 'unconfirmed'}}}})
        response = self.client.post('/api/recheck', json={'account': 'Audit', 'chats': ['-101']})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(gui.BOT['busy'])

    def test_missing_id_send_with_reliable_boundary_can_be_inspected_readonly(self):
        saved={'date':f'{datetime.now():%Y-%m-%d}','confirm':'unconfirmed','actions':[{
            'sent':True,'sent_mids':[],'boundary':{'peer':'-101','cutoff':time.time()}}]}
        state.save_state({'_last':{'Audit':{'-101':saved}}})
        self.assertTrue(self.client.get('/api/status').json['accounts'][0]['groups'][0]['can_recheck'])
        response=self.client.post('/api/recheck',json={'account':'Audit','chats':['-101']})
        self.assertEqual(response.status_code,202)
        self.assertEqual(gui.TRIGGER['mode'],'recheck')

    def test_legacy_success_is_labelled_without_fabricating_evidence(self):
        state.save_state({'_last': {'Audit': {'-101': {'date': f'{datetime.now():%Y-%m-%d}', 'confirm': 'success', 'detail': 'old text'}}}})
        group = self.client.get('/api/status').json['accounts'][0]['groups'][0]
        self.assertEqual(group['source'], 'legacy')
        self.assertFalse(group['can_recheck'])

    def test_unattributed_success_is_exposed_as_candidate_without_marking_success(self):
        state.save_state({'_last':{'Audit':{'-101':{'date':f'{datetime.now():%Y-%m-%d}','confirm':'unconfirmed',
            'evidence':[{'mid':'20','t':'签到成功，积分 +5','source':'unrelated'}]}}}})
        group=self.client.get('/api/status').json['accounts'][0]['groups'][0]
        self.assertEqual(group['confirm'],'unconfirmed')
        self.assertEqual(group['candidate_replies'][0]['mid'],'20')

    def test_previous_day_recheck_cannot_mark_today_done(self):
        previous={'date':'2000-01-01','confirm':'unconfirmed','actions':[{'sent_mids':['5'],'boundary':{'peer':'-101'}}]}
        state.save_state({'_last':{'Audit':{'-101':previous}}})
        response=self.client.post('/api/recheck',json={'account':'Audit','chats':['-101']})
        self.assertEqual(response.status_code,400)
        self.assertFalse(gui.BOT['busy'])

    def test_malformed_requests_are_400(self):
        for endpoint, data in [('/api/checkin_now', []), ('/api/checkin_now', {'account': []}),
                               ('/api/checkin_now', {'chats': []}), ('/api/config', {'accounts': [7]}),
                               ('/api/delete_session', {'account': []}), ('/api/web_login', {'account': []})]:
            with self.subTest(endpoint=endpoint, data=data):
                self.assertEqual(self.client.post(endpoint, json=data).status_code, 400)
        self.assertEqual(self.client.get('/api/stats?days=abc').status_code, 400)

    def test_delete_rejects_path_escape_before_touching_files(self):
        with patch('shutil.rmtree') as delete:
            for name in ('..', '.', '', 'unknown', 'CON', '../Audit'):
                self.assertEqual(self.client.post('/api/delete_session', json={'account': name}).status_code, 400)
            delete.assert_not_called()

    def test_last_account_can_be_deleted_with_session(self):
        profile = web_engine.PROFILES_DIR/'Audit'
        profile.mkdir(parents=True)
        (profile/'dummy.txt').write_text('fixture')
        response = self.client.post('/api/delete_account', json={'account': 'Audit'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(config.load_config()['accounts'], [])
        self.assertFalse(profile.exists())

    def test_failed_account_delete_rolls_back_session_and_configuration(self):
        profile = web_engine.PROFILES_DIR/'Audit'
        profile.mkdir(parents=True)
        (profile/'dummy.txt').write_text('fixture')
        with patch.object(gui, 'save_config', side_effect=OSError('disk unavailable')):
            response = self.client.post('/api/delete_account', json={'account': 'Audit'})
        self.assertEqual(response.status_code, 503)
        self.assertTrue((profile/'dummy.txt').exists())
        self.assertEqual(config.load_config()['accounts'][0]['name'], 'Audit')

    def test_token_required_for_mutations_but_assets_are_public(self):
        cfg = copy.deepcopy(FIXTURE)
        cfg['gui_token'] = 'test-secret'
        config.save_config(cfg)
        self.assertEqual(self.client.get('/api/config').status_code, 401)
        self.assertEqual(self.client.post('/api/delete_session', json={'account': 'Audit'}).status_code, 401)
        with self.client.get('/static/app.js') as response:
            self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get('/api/config', headers={'X-Token': 'test-secret'}).status_code, 200)

    def test_revision_conflict_does_not_overwrite_config(self):
        first = self.client.get('/api/config').json
        altered = copy.deepcopy(FIXTURE)
        altered['reply_wait_seconds'] = 12
        config.save_config(altered)
        response = self.client.post('/api/config', json={'cfg': FIXTURE, 'revision': first['_revision']})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(config.load_config()['reply_wait_seconds'], 12)

    def test_full_group_fields_survive_save(self):
        response = self.client.post('/api/config', json={'cfg': FIXTURE})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(config.load_config(), FIXTURE)

    def test_rename_preserves_daily_state_and_cache(self):
        today = f'{datetime.now():%Y-%m-%d}'
        state.save_state({'Audit': {'-101': today}, '_last': {'Audit': {'-101': {'date': today}}}, '_chat_cache': {'Audit': {'lists': {}}}})
        changed = copy.deepcopy(FIXTURE)
        changed['accounts'][0]['name'] = 'Renamed'
        response = self.client.post('/api/config', json={'cfg': changed, 'renames': {'Audit': 'Renamed'}})
        self.assertEqual(response.status_code, 200)
        result = state.load_state()
        self.assertEqual(result['Renamed']['-101'], today)
        self.assertIn('Renamed', result['_last'])
        self.assertIn('Renamed', result['_chat_cache'])

    def test_state_errors_take_priority_and_full_evidence_is_saved(self):
        value = {}
        state.record_last(value, 'Audit', '-101', {'ok': False, 'confirm': 'failed', 'sent': False,
                          'error': 'Wrong destination', 'result_text': 'unrelated success', 'replies': ['ad text'],
                          'expected_peer': '-101', 'actual_peer': '-999'})
        self.assertEqual(state.load_state()['_last']['Audit']['-101']['detail'], 'Wrong destination')
        self.assertIs(value['_last']['Audit']['-101']['sent'], False)

    def test_state_recovers_from_backup_or_fails_closed(self):
        state.save_state({'Audit': {'-101': 'today'}})
        state.STATE_PATH.write_text('{broken', encoding='utf-8')
        self.assertEqual(state.load_state()['Audit']['-101'], 'today')
        state.STATE_PATH.with_suffix('.json.bak').write_text('{broken', encoding='utf-8')
        with self.assertRaises(RuntimeError): state.load_state()

    def test_invalid_names_types_and_ranges(self):
        for name in ('..', '.', '_last', 'CON', 'LPT1.txt', 'bad.'):
            cfg = {'accounts': [{'name': name, 'groups': []}]}
            self.assertIsNotNone(config.validate_config(cfg), name)
        cfg = {'accounts': [{'name': 'A', 'groups': []}, {'name': 'a', 'groups': []}]}
        self.assertIn('冲突', config.validate_config(cfg))
        for value in (-1, [3,1], float('nan'), [0, float('inf')]):
            self.assertIsNotNone(config.validate_config({**FIXTURE, 'group_delay_seconds': value}))

    def test_unrelated_reply_cannot_confirm_and_no_implicit_keyword_overwrite(self):
        self.assertEqual(judge_replies({}, {}, ['签到成功'], []), 'unconfirmed')
        cfg = {'accounts': [{'name': 'A', 'groups': [{'chat': '-101', 'success_keywords': ['OLD']}]},
                            {'name': 'B', 'groups': [{'chat': '-101', 'success_keywords': ['NEW']}]}]}
        config.save_config(cfg)
        self.assertEqual(config.load_config()['accounts'][1]['groups'][0]['success_keywords'], ['NEW'])

    def test_file_lock_releases_without_killing_other_process(self):
        lock = FileLock(self.root/'execution.lock').acquire()
        try:
            with self.assertRaises(RuntimeError): FileLock(self.root/'execution.lock').acquire()
        finally:
            lock.release()
        with FileLock(self.root/'execution.lock'): pass


    def test_api_explains_rule_mismatch_without_modifying_result(self):
        cfg=copy.deepcopy(FIXTURE);cfg['accounts'][0]['groups'][0]['success_keywords']=['专属完成回执']
        config.save_config(cfg)
        result=ResultJudgementTests().pending()
        state.record_last({},'Audit','-101',{**result,'sent':True,'replies':['签到成功，奖励已发放']})
        before=state.load_state()
        row=self.client.get('/api/status').json['accounts'][0]['groups'][0]
        self.assertEqual(row['confirm'],'unconfirmed')
        self.assertEqual(row['feedback_hint']['code'],'rule_mismatch')
        self.assertTrue(row['can_recheck'])
        self.assertEqual(state.load_state(),before)


class FakeEngine:
    confirmation = ['签到成功']
    fail_open = False
    fail_launch = False
    click_ok = True
    instances = []

    def __init__(self, account, cfg):
        self.name=account['name']; self.last_sent_mids=set(); self.last_replied=[]; self.last_evidence=[]
        self.sent_count=0; self.watch_count=0; self._expected_peer='-101'; self._last_actual_peer='-101'
        self.instances.append(self)
        self.last_draft_warning=None

    @asynccontextmanager
    async def preserve_draft(self):
        yield

    async def launch(self):
        if self.fail_launch: raise RuntimeError('Browser unavailable')
    async def close(self): pass
    async def ensure_login(self): pass
    async def is_logged_in(self): return True
    async def probe_chat(self,chat,*_):return {'chat':chat,'ok':True,'elapsed':0}
    async def open_chat(self, *args):
        if self.fail_open: raise RuntimeError('Wrong destination; not sent')
    async def assert_destination(self): return '-101'
    async def _scroll_chat_bottom(self): pass
    async def collect_feedback_history(self, *_):return {'items':[],'aliases':[]}
    async def recover_sent_mids(self, *_):return {'sent_mids':[],'evidence':[]}
    async def opened_chat_info(self): return {'pid':'-101','title':'Verified title'}
    async def snapshot_incoming(self, *_): return {'seen': set(), 'cutoff': time.time(), 'peer':'-101'}
    async def send_text(self, *_):
        if getattr(self,'before_send',None):await self.before_send()
        self.sent_count+=1
        self.last_sent_mids.add(str(self.sent_count))
    send_image=send_text
    async def click_button(self, *_): return self.click_ok
    async def watch_new_incoming(self, *_, **kwargs):
        self.watch_count+=1
        self.last_replied=list(self.confirmation) if self.watch_count==1 else []
        self.last_evidence=[{'mid':'42','t':'签到成功','source':'reply_to_sent'}] if self.last_replied else []
        return self.last_replied


class RunnerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        FakeEngine.instances=[];FakeEngine.fail_open=False;FakeEngine.fail_launch=False;FakeEngine.click_ok=True
        self.patches=[patch.object(web_engine,'WebEngine',FakeEngine), patch.object(state,'save_state'),
                      patch.object(runner,'append_history'), patch.object(runner,'record_title')]
        for item in self.patches: item.start()
        runner.CAPTURE['items']=[]
        self.today=f'{runner.local_now():%Y-%m-%d}'

    async def asyncTearDown(self):
        for item in reversed(self.patches): item.stop()

    async def execute(self, groups, settings=None):
        value={}
        result=await runner._web_account_worker({'name':'Audit','groups':groups}, {'reply_wait_seconds':.01,**(settings or {})}, value,self.today,0,0,.01,None)
        return result,value

    async def test_dispatch_intent_is_durable_before_a_hard_exit(self):
        class SimulatedCrash(BaseException):pass
        value={};saved=[];engine=FakeEngine({'name':'Audit'}, {})
        async def crash(*args):
            await engine.before_send()
            raise SimulatedCrash()
        engine.send_text=crash
        with patch.object(state,'save_state',side_effect=lambda value:saved.append(copy.deepcopy(value))):
            with self.assertRaises(SimulatedCrash):
                await runner._send_group_actions(engine,value,'Audit',{'chat':'-101','message':'签到'},{},0,0)
        recovered=saved[-1]
        self.assertEqual(recovered['Audit']['-101'],self.today)
        self.assertEqual(recovered['_last']['Audit']['-101']['confirm'],'unconfirmed')
        self.assertIsNone(recovered['_last']['Audit']['-101']['sent'])
        self.assertEqual(engine.sent_count,0)
        self.assertIsNone(engine.before_send)
        json.dumps(recovered)

    async def test_outgoing_ids_are_saved_before_waiting_for_feedback(self):
        value={};engine=FakeEngine({'name':'Audit'}, {});original=engine.watch_new_incoming;observed=[]
        async def watch(*args,**kwargs):
            observed.append(copy.deepcopy(value['_last']['Audit']['-101']))
            return await original(*args,**kwargs)
        engine.watch_new_incoming=watch
        await runner._send_group_actions(engine,value,'Audit',{'chat':'-101','message':'签到'},{},0,0)
        self.assertTrue(observed[0]['sent'])
        self.assertEqual(observed[0]['actions'][0]['sent_mids'],['1'])
        self.assertEqual(observed[0]['confirm'],'unconfirmed')

    async def test_button_only_uses_popup_as_feedback_without_sending_a_message(self):
        value={};engine=FakeEngine({'name':'Audit'}, {})
        group={'chat':'701','method':'button_only','click_button':'示例签到'}

        async def click(*args):
            self.assertIsNotNone(engine.before_send)
            await engine.before_send()
            self.assertIsNone(value['_last']['Audit']['701']['sent'])
            return '30'

        async def popup(seconds):
            self.assertTrue(value['_last']['Audit']['701']['sent'])
            return '签到成功，获得 8 积分'

        engine.click_existing_button=click
        engine.read_button_popup=popup
        _,actions,_,_=await runner._send_group_actions(engine,value,'Audit',group,{},0,0)
        result=runner._result(group,actions,engine)
        self.assertEqual(engine.sent_count,0)
        self.assertEqual(result['confirm'],'success')
        self.assertTrue(result['clicked'])
        self.assertEqual(result['source'],'button_popup')
        self.assertEqual(result['evidence'][0]['mid'],'30')
        self.assertEqual(result['replies'],['签到成功，获得 8 积分'])

    async def test_final_result_and_processed_date_are_saved_together_once(self):
        value={};saved=[]
        result={'chat':'-101','name':'Fixture','ok':True,'confirm':'success','sent':True,'error':None,'replies':['签到成功']}
        with patch.object(state,'save_state',side_effect=lambda value:saved.append(copy.deepcopy(value))):
            runner._store(value,'Audit',{'chat':'-101'},result,self.today)
        self.assertEqual(len(saved),1)
        self.assertEqual(saved[0]['Audit']['-101'],self.today)
        self.assertEqual(saved[0]['_last']['Audit']['-101']['confirm'],'success')

    async def test_latest_message_preparation_counts_towards_the_send_interval(self):
        from types import SimpleNamespace
        clock=SimpleNamespace(value=0);events=[];engine=FakeEngine({'name':'Audit'},{});engine._next_send_at=10
        snapshot=engine.snapshot_incoming;send=engine.send_text
        async def prepare(*args):
            events.append(('prepare',clock.value));clock.value+=6
            return await snapshot(*args)
        async def wait(deadline):events.append(('wait',deadline-clock.value));clock.value=max(clock.value,deadline)
        async def dispatch(*args):events.append(('send',clock.value));return await send(*args)
        engine.snapshot_incoming=prepare;engine.send_text=dispatch
        with patch.object(runner,'time',SimpleNamespace(monotonic=lambda:clock.value)),patch.object(runner,'_wait_until',side_effect=wait):
            await runner._send_group_actions(engine,{},'Audit',{'chat':'-101'},{},0,0)
        self.assertEqual(events,[('prepare',0),('wait',4),('send',10)])

    async def test_failed_intent_write_prevents_dispatch(self):
        value={};engine=FakeEngine({'name':'Audit'}, {})
        with patch.object(state,'save_state',side_effect=OSError('fixture storage failure')):
            outcome=await runner._send_group_actions(engine,value,'Audit',{'chat':'-101','message':'签到'},{},0,0)
        self.assertEqual(engine.sent_count,0)
        self.assertFalse(outcome[1][0]['sent'])
        self.assertIn('storage failure',outcome[1][0]['error'])

    async def test_next_chat_opens_immediately_while_send_interval_is_preserved(self):
        events=[];original_send=FakeEngine.send_text;original_watch=FakeEngine.watch_new_incoming
        async def opened(engine,*args):events.append(('open',time.monotonic()))
        async def sent(engine,*args):
            await original_send(engine,*args)
            engine.send_started_monotonic=time.monotonic()
            events.append(('send',engine.send_started_monotonic))
        async def watched(engine,*args,**kwargs):
            await asyncio.sleep(.03)
            result=await original_watch(engine,*args,**kwargs)
            events.append(('feedback',time.monotonic()))
            return result
        with patch.object(FakeEngine,'open_chat',opened),patch.object(FakeEngine,'send_text',sent),patch.object(FakeEngine,'watch_new_incoming',watched):
            await runner._web_account_worker({'name':'Audit','groups':[{'chat':'-101'},{'chat':'-102'}]}, {},{},self.today,.3,.3,.01,None)
        self.assertEqual([kind for kind,_ in events],['open','send','feedback','open','send','feedback'])
        self.assertLess(events[3][1]-events[2][1],.1)
        self.assertGreaterEqual(events[4][1]-events[1][1],.3)

    async def test_open_failure_is_persisted_without_waiting_for_feedback(self):
        FakeEngine.fail_open=True
        retry,value=await self.execute([{'chat':'-101','title':'Target'}])
        self.assertEqual(retry, {'Audit':['-101']})
        self.assertEqual(value['_last']['Audit']['-101']['confirm'],'failed')
        self.assertIn('Wrong destination', value['_last']['Audit']['-101']['error'])
        self.assertEqual(FakeEngine.instances[0].watch_count,0)
        self.assertEqual(FakeEngine.instances[0].sent_count,0)

    async def test_later_preparation_failure_keeps_previous_send_evidence(self):
        boundary={'seen':[], 'cutoff':time.time(), 'peer':'-101'}
        with patch.object(FakeEngine,'snapshot_incoming',side_effect=[boundary,RuntimeError('history not ready')]):
            _,value=await self.execute([{'chat':'-101','actions':[{'message':'first'},{'message':'second'}]}])
        result=value['_last']['Audit']['-101']
        self.assertTrue(result['sent'])
        self.assertEqual(result['confirm'],'failed')
        self.assertEqual(len(result['actions']),2)
        self.assertEqual(result['actions'][0]['sent_mids'],['1'])
        self.assertFalse(result['actions'][1]['sent'])
        self.assertIn('history not ready',result['error'])
        self.assertEqual(value['Audit']['-101'],self.today)
        self.assertEqual(FakeEngine.instances[0].sent_count,1)

    async def test_cancel_during_later_preparation_keeps_previous_send(self):
        value={};account={'name':'Audit','groups':[{'chat':'-101','actions':[{'message':'first'},{'message':'second'}]}]}
        with patch.object(FakeEngine,'snapshot_incoming',side_effect=[{'seen':[],'cutoff':time.time(),'peer':'-101'},asyncio.CancelledError()]):
            with self.assertRaises(asyncio.CancelledError):
                await runner._web_account_worker(account,{},value,self.today,0,0,.01,None)
        self.assertTrue(value['_last']['Audit']['-101']['sent'])
        self.assertEqual(value['_last']['Audit']['-101']['actions'][0]['sent_mids'],['1'])
        self.assertEqual(FakeEngine.instances[0].sent_count,1)

    async def test_single_chat_probe_does_not_visit_unselected_groups(self):
        account={'name':'Audit','groups':[{'chat':'-101'},{'chat':'-102'}]}
        result=await runner._probe_account(account,{},['-102'])
        self.assertEqual([row['chat'] for row in result['chats']],['-102'])
        self.assertEqual(FakeEngine.instances[0].sent_count,0)

    async def test_second_pass_does_not_put_runtime_sets_in_persisted_state(self):
        async def watch(engine,boundary,*args,**kwargs):
            engine.watch_count+=1;boundary['seen']=set(boundary.get('seen',[]))
            boundary['seen'].add('502')
            engine.last_replied=['签到成功'] if engine.watch_count>1 else []
            engine.last_evidence=[]
            return engine.last_replied
        with patch.object(FakeEngine,'watch_new_incoming',watch),patch.object(state,'save_state',side_effect=lambda value:json.dumps(value)):
            _,value=await self.execute([{'chat':'-101'}],{'reply_second_pass':True})
        self.assertEqual(value['_last']['Audit']['-101']['confirm'],'success')
        self.assertEqual(FakeEngine.instances[0].sent_count,1)
        self.assertEqual(FakeEngine.instances[0].watch_count,2)
        json.dumps(value)

    async def test_failed_recheck_does_not_mutate_previous_result(self):
        previous={'actions':[{'confirm':'unconfirmed','sent':True,'sent_mids':['501'],
                             'boundary':{'peer':'-101','seen':['100']}}]}
        before=copy.deepcopy(previous)
        async def failed_watch(engine,boundary,*args,**kwargs):
            boundary['seen']=set(boundary['seen']);boundary['seen'].add('502')
            raise RuntimeError('page disconnected')
        engine=FakeEngine({'name':'Audit'}, {})
        with patch.object(FakeEngine,'watch_new_incoming',failed_watch),self.assertRaisesRegex(RuntimeError,'disconnected'):
            await runner._recheck(engine,{'chat':'-101'},previous,{})
        self.assertEqual(previous,before)
        json.dumps(previous)

    async def test_readonly_recheck_corrects_keyword_failure_without_resending(self):
        prompt = ResultJudgementTests.prompt
        previous={'actions':[{'ok':True,'confirm':'failed','sent':True,'sent_mids':['501'],
                  'error':None,'clicked':True,'with_image':False,'replies':[prompt],
                  'trusted_replies':[prompt],'evidence':[],
                  'boundary':{'peer':'-101','seen':[],'cutoff':time.time()}}]}
        before=copy.deepcopy(previous)
        engine=FakeEngine({'name':'Audit'}, {})
        result=await runner._recheck(engine,{'chat':'-101'},previous,{})
        self.assertEqual(result['confirm'],'success')
        self.assertEqual(engine.sent_count,0)
        self.assertEqual(engine.watch_count,1)
        self.assertEqual(previous,before)

    async def test_recheck_preserves_incomplete_button_error(self):
        previous={'actions':[{'ok':False,'confirm':'failed','sent':True,'sent_mids':['501'],
                  'error':'Required button not found','clicked':False,'with_image':False,'replies':[],'trusted_replies':[],'evidence':[],
                  'boundary':{'peer':'-101','seen':[],'cutoff':time.time()}}]}
        engine=FakeEngine({'name':'Audit'}, {})
        result=await runner._recheck(engine,{'chat':'-101'},previous,{})
        self.assertEqual(result['confirm'],'failed')
        self.assertEqual(engine.watch_count,0)
        self.assertEqual(engine.sent_count,0)

    async def test_readonly_recheck_recovers_missing_id_without_sending(self):
        previous={'actions':[{'ok':True,'confirm':'unconfirmed','sent':True,'sent_mids':[],'error':None,'clicked':False,'with_image':False,
            'replies':[],'trusted_replies':[],'evidence':[],
            'boundary':{'peer':'-101','cutoff':time.time()}}]}
        before=copy.deepcopy(previous);engine=FakeEngine({'name':'Audit'}, {})
        with patch.object(engine,'recover_sent_mids',AsyncMock(return_value={'sent_mids':['501'],'evidence':[],'source':'account_mention_reply'})):
            result=await runner._recheck(engine,{'chat':'-101','message':'签到'},previous,{})
        self.assertEqual(result['confirm'],'success')
        self.assertEqual(result['actions'][0]['sent_mids'],['501'])
        self.assertEqual(engine.sent_count,0)
        self.assertEqual(previous,before)

    async def test_readonly_missing_id_remains_pending_when_recovery_is_ambiguous(self):
        previous={'actions':[{'ok':True,'confirm':'unconfirmed','sent':True,'sent_mids':[],'error':None,'clicked':False,'with_image':False,
            'replies':[],'trusted_replies':[],'evidence':[],
            'boundary':{'peer':'-101','cutoff':time.time()}}]}
        engine=FakeEngine({'name':'Audit'}, {})
        result=await runner._recheck(engine,{'chat':'-101'},previous,{})
        self.assertEqual(result['confirm'],'unconfirmed')
        self.assertEqual(engine.sent_count,0)
        self.assertEqual(engine.watch_count,0)

    async def test_feedback_completion_uses_group_keywords_and_trusted_pool(self):
        engine=FakeEngine({'name':'Audit'}, {})
        with patch.object(engine,'watch_new_incoming',new=AsyncMock(return_value=[])) as watch:
            await runner._watch_feedback(engine,{}, {'chat':'-101','success_keywords':['奖励入账']},{},sent_mids=['501'])
        complete=watch.call_args.kwargs['is_complete']
        self.assertFalse(complete([]))
        self.assertFalse(complete(['正在处理请求']))
        self.assertTrue(complete(['奖励入账']))

    async def test_uncertain_command_does_not_complete_required_buttons(self):
        async def uncertain(engine,*_):
            if engine.before_send:await engine.before_send()
            engine.sent_count+=1;engine.last_sent_mids.add('501')
            error=RuntimeError('Receipt pending');error.sent=error.uncertain=True
            raise error
        with patch.object(FakeEngine,'send_text',uncertain):
            _,value=await self.execute([{'chat':'-101','method':'command_button','message':'/checkin','click_button':'Confirm'}])
        action=value['_last']['Audit']['-101']['actions'][0]
        self.assertEqual(action['confirm'],'unconfirmed')
        self.assertFalse(action['buttons_complete'])
        self.assertFalse(action['clicked'])
        self.assertEqual(FakeEngine.instances[0].sent_count,1)
        self.assertIn('必需按钮',action['warning'])

    async def test_legacy_uncertain_button_cannot_be_completed_by_readonly_feedback(self):
        previous={'actions':[{'ok':True,'confirm':'unconfirmed','sent':True,'sent_mids':['501'],
                  'error':None,'clicked':False,'with_image':False,'replies':[],
                  'trusted_replies':[],'evidence':[],
                  'boundary':{'peer':'-101','seen':[],'cutoff':time.time()}}]}
        for old_confirm in ('unconfirmed','success'):
            with self.subTest(old_confirm=old_confirm):
                previous['actions'][0]['confirm']=old_confirm
                engine=FakeEngine({'name':'Audit'}, {})
                result=await runner._recheck(engine,{'chat':'-101','method':'command_button','click_button':'Confirm'},previous,{})
                self.assertEqual(result['confirm'],'unconfirmed')
                self.assertEqual(result['actions'][0]['confirm'],'unconfirmed')
                self.assertFalse(result['actions'][0]['buttons_complete'])
                self.assertEqual(engine.sent_count,0)
                self.assertNotIn('buttons_complete',previous['actions'][0])

    async def test_completed_button_sequence_is_recorded(self):
        _,value=await self.execute([{'chat':'-101','method':'command_button','message':'/checkin','click_button':['Confirm','Claim']}])
        action=value['_last']['Audit']['-101']['actions'][0]
        self.assertEqual(action['confirm'],'success')
        self.assertTrue(action['buttons_complete'])

    async def test_single_account_round_does_not_launch_other_account_with_same_chat(self):
        cfg={'group_delay_seconds':0,'accounts':[
            {'name':'Audit','groups':[{'chat':'-101'},{'chat':'-102'}]},
            {'name':'Other','groups':[{'chat':'-101'}]}]}
        with tempfile.TemporaryDirectory(prefix='scope-',dir=ROOT/'audit') as directory:
            with patch.object(runner,'BASE_DIR',Path(directory)),patch.object(runner,'load_state',return_value={}),patch.object(runner,'save_capture'):
                await runner.run_web_round(cfg,{},only='Audit',chats=['-101'])
        self.assertEqual([engine.name for engine in FakeEngine.instances],['Audit'])
        self.assertEqual(FakeEngine.instances[0].sent_count,1)

    async def test_single_account_default_skips_processed_chats_but_explicit_chat_can_run(self):
        account={'name':'Audit','groups':[{'chat':'-101'},{'chat':'-102'}]}
        value={'Audit':{'-101':self.today}}
        await runner._web_account_worker(account,{},value,self.today,0,0,.01,None)
        self.assertEqual(FakeEngine.instances[0].sent_count,1)
        await runner._web_account_worker(account,{},value,self.today,0,0,.01,['-101'])
        self.assertEqual(FakeEngine.instances[1].sent_count,1)

    async def test_startup_failure_records_every_target(self):
        FakeEngine.fail_launch=True
        retry,value=await self.execute([{'chat':'-101'},{'chat':'-102'}])
        self.assertEqual(len(retry['Audit']),2)
        self.assertEqual(len(value['_last']['Audit']),2)

    async def test_required_button_failure_is_failed_after_send_and_no_watch(self):
        FakeEngine.click_ok=False
        retry,value=await self.execute([{'chat':'-101','method':'command_button','message':'/checkin','click_button':'Confirm'}])
        last=value['_last']['Audit']['-101']
        self.assertEqual(last['confirm'],'failed')
        self.assertTrue(last['sent'])
        self.assertIn('Confirm',last['error'])
        self.assertEqual(FakeEngine.instances[0].watch_count,0)

    async def test_missing_outgoing_id_still_collects_feedback_and_stops_dependent_send(self):
        error=RuntimeError('command deleted too quickly');error.sent=True;error.uncertain=True
        async def feedback(engine,*_,**kwargs):
            engine.watch_count+=1;engine.last_replied=[];engine.last_evidence=[]
            return ['unrelated message']
        with patch.object(FakeEngine,'send_text',side_effect=error) as send,patch.object(FakeEngine,'watch_new_incoming',feedback):
            _,value=await self.execute([{'chat':'-101','actions':[{'message':'One'},{'message':'Two'}]}])
        last=value['_last']['Audit']['-101']
        self.assertEqual(last['confirm'],'unconfirmed')
        self.assertTrue(last['sent'])
        self.assertEqual(FakeEngine.instances[0].watch_count,1)
        self.assertEqual(send.call_count,1)
        self.assertIn('deleted',last['actions'][0]['warning'])

    async def test_cancel_during_uncertain_send_feedback_remains_recheckable(self):
        value={};engine=FakeEngine({'name':'Audit'}, {});original_send=engine.send_text
        async def uncertain(*args):
            await original_send(*args)
            error=RuntimeError('fixture acknowledgement lost');error.sent=error.uncertain=True
            raise error
        engine.send_text=uncertain
        with patch.object(engine,'watch_new_incoming',side_effect=asyncio.CancelledError):
            with self.assertRaises(asyncio.CancelledError):
                await runner._send_group_actions(engine,value,'Audit',{'chat':'-101'},{},0,0)
        last=value['_last']['Audit']['-101']
        self.assertEqual(last['confirm'],'unconfirmed')
        self.assertTrue(last['sent'])
        self.assertEqual(last['actions'][0]['confirm'],'unconfirmed')
        self.assertEqual(last['actions'][0]['sent_mids'],['1'])
        self.assertEqual(value['Audit']['-101'],self.today)

    async def test_shared_success_retains_source_chat_and_feedback(self):
        value={'_last':{'Audit':{'-102':{'date':self.today,'confirm':'success','name':'Actual bot','replies':['签到成功 +5'],'evidence':[{'mid':'123'}]}}}}
        saved=[]
        with patch.object(state,'save_state',side_effect=lambda value:saved.append(copy.deepcopy(value))):
            runner.share_result_to_members(value,'Audit',self.today,'Shared',[{'chat':'-101'},{'chat':'-102'}],'-102')
        self.assertEqual(len(saved),1)
        self.assertEqual(saved[0]['Audit']['-101'],self.today)
        self.assertEqual(saved[0]['_last']['Audit']['-101']['source'],'shared')
        result=value['_last']['Audit']['-101']
        self.assertEqual(result['shared_from'],{'chat':'-102','name':'Actual bot'})
        self.assertIn('签到成功 +5',result['replies'])
        self.assertEqual(result['evidence'],[{'mid':'123'}])

    async def test_multi_action_requires_confirmation_for_every_action(self):
        _,value=await self.execute([{'chat':'-101','actions':[{'method':'text','message':'One'},{'method':'text','message':'Two'}]}])
        last=value['_last']['Audit']['-101']
        self.assertEqual(last['confirm'],'unconfirmed')
        self.assertEqual([a['confirm'] for a in last['actions']],['success','unconfirmed'])

    async def test_cancellation_closes_browser_and_propagates(self):
        with patch.object(FakeEngine,'send_text',side_effect=asyncio.CancelledError), patch.object(FakeEngine,'close',new_callable=AsyncMock) as close:
            with self.assertRaises(asyncio.CancelledError): await self.execute([{'chat':'-101'}])
            close.assert_awaited_once()

    async def test_shared_success_overrides_previously_sent_failed_member(self):
        value={'Audit':{'-101':self.today},'_last':{'Audit':{'-101':{'date':self.today,'confirm':'failed','sent':True}}}}
        runner.share_result_to_members(value,'Audit',self.today,'Shared',[{'chat':'-101'},{'chat':'-102'}],'-102')
        last=value['_last']['Audit']['-101']
        self.assertEqual(last['confirm'],'success')
        self.assertEqual(last['source'],'shared')
        self.assertTrue(last['sent'])

    async def test_worker_exception_is_not_reported_as_empty_success(self):
        with tempfile.TemporaryDirectory(prefix='round-',dir=ROOT/'audit') as directory:
            with patch.object(runner,'BASE_DIR',Path(directory)),patch.object(runner,'load_state',return_value={}),patch.object(runner,'prune_state'),patch.object(runner,'save_capture'),patch.object(runner,'_web_account_worker',side_effect=RuntimeError('worker crashed')):
                with self.assertRaisesRegex(RuntimeError,'worker crashed'):
                    await runner.run_web_round(FIXTURE,{})

    async def test_first_two_accounts_are_staggered(self):
        started=[]
        async def worker(account,*args):
            started.append(time.monotonic())
            return {}
        cfg={'accounts':[{'name':name,'groups':[{'chat':'-101'}]} for name in ('A','B')],'account_delay_seconds':.03}
        with tempfile.TemporaryDirectory(prefix='round-',dir=ROOT/'audit') as directory:
            with patch.object(runner,'BASE_DIR',Path(directory)),patch.object(runner,'load_state',return_value={}),patch.object(runner,'prune_state'),patch.object(runner,'save_capture'),patch.object(runner,'_web_account_worker',worker):
                await runner.run_web_round(cfg,{})
        self.assertGreaterEqual(started[1]-started[0],.025)


if __name__=='__main__': unittest.main()

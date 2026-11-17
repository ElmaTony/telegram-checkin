"""Readable, truthful classified reports; all delivery calls are intercepted."""
import asyncio
import copy
import json
import unittest
from datetime import datetime
from unittest.mock import patch
from urllib.parse import parse_qs
from zoneinfo import ZoneInfo

from core import notifications

CURRENT = datetime(2026, 11, 4, 12, 34, 56, tzinfo=ZoneInfo('Asia/Hong_Kong'))
TODAY = '2026-11-04'


def fixture():
    cfg = {'accounts': [
        {'name': '主号', 'groups': [
            {'chat': '-101', 'title': '普通成功群'},
            {'chat': '-102', 'title': '未发送群', 'topic': '讨论区'},
            {'chat': '-103', 'title': '发送存疑群'},
            {'chat': '-104', 'title': '尚未执行群'}]},
        {'name': '备用号', 'groups': [
            {'chat': '-201', 'title': '共享群'},
            {'chat': '-202', 'title': '按钮失败群'},
            {'chat': '-203', 'title': '候选反馈群'},
            {'chat': '-204', 'title': '草稿恢复群'}]}],
        'notifications': {'enabled': True}}
    runtime = {'_last': {
        '主号': {
            '-101': {'date': TODAY, 'confirm': 'success', 'source': 'reply_to_sent', 'sent': True},
            '-102': {'date': TODAY, 'confirm': 'failed', 'sent': False, 'error': '话题核对失败'},
            '-103': {'date': TODAY, 'confirm': 'unconfirmed', 'sent': True, 'actions': [{'sent': True, 'warning': '未登记 ID', 'sent_mids': []}]}},
        '备用号': {
            '-201': {'date': TODAY, 'confirm': 'success', 'source': 'shared', 'shared_from': {'name': '普通成功群'}},
            '-202': {'date': TODAY, 'confirm': 'failed', 'sent': True, 'error': '未找到必需按钮'},
            '-203': {'date': TODAY, 'confirm': 'unconfirmed', 'sent': True, 'evidence': [{'source': 'unrelated', 't': '签到成功，积分加20'}]},
            '-204': {'date': TODAY, 'confirm': 'success', 'draft_warning': '已保留 /private/path/backup.json'}}}}
    return cfg, runtime


class NotificationReportTests(unittest.TestCase):
    def setUp(self):
        self.clock = patch.object(notifications, 'now', return_value=CURRENT)
        self.clock.start()
        self.cfg, self.runtime = fixture()

    def tearDown(self):
        self.clock.stop()

    def test_status_categories_and_account_totals_are_consistent(self):
        title, body, issues = notifications.round_message(self.cfg, self.runtime)
        self.assertTrue(issues)
        self.assertIn('共 8 个会话 · 2 个账号', body)
        self.assertIn('✅ 成功 3 · ⚠️ 待核对 2 · ❌ 失败 2 · ⏸ 未执行 1', body)
        self.assertIn('含 1 项共享完成，不重复计数', body)
        self.assertIn('主号**（4）：✅ 1 · ⚠️ 1 · ❌ 1 · ⏸ 1', body)
        self.assertLess(body.index('## 📝'), body.index('## ❌'))
        self.assertLess(body.index('## ❌'), body.index('## ⚠️'))
        self.assertLess(body.index('## ⚠️'), body.index('## ⏸'))
        self.assertLess(body.index('## ⏸'), body.index('## ✅'))
        self.assertIn('草稿异常', title)

    def test_uncertain_send_never_claims_definite_delivery(self):
        _, body, _ = notifications.round_message(self.cfg, self.runtime, only='主号', chats=['-103'])
        self.assertIn('发送状态待核对', body)
        self.assertNotIn('已发出；', body)
        self.assertNotIn('## ✅ 已完成', body)
        self.assertIn('勿直接重发', body)

    def test_candidate_feedback_is_not_promoted_to_success(self):
        before = copy.deepcopy(self.runtime)
        _, body, issues = notifications.round_message(self.cfg, self.runtime, only='备用号', chats=['-203'])
        self.assertTrue(issues)
        self.assertIn('归属尚未确认', body)
        self.assertIn('✅ 成功 0', body)
        self.assertNotIn('## ✅ 已完成', body)
        self.assertEqual(self.runtime, before)

    def test_failure_distinguishes_before_and_after_send_with_topic(self):
        _, body, _ = notifications.round_message(self.cfg, self.runtime)
        self.assertIn('未发送群 / 讨论区** — 未发送；话题定位未完成', body)
        self.assertIn('按钮失败群** — 已发送后失败；必需按钮未完成', body)

    def test_shared_success_includes_source_without_raw_feedback(self):
        self.runtime['_last']['备用号']['-201']['replies'] = ['private bot chatter']
        _, body, issues = notifications.round_message(self.cfg, self.runtime, only='备用号', chats=['-201'])
        self.assertFalse(issues)
        self.assertIn('成功 1', body)
        self.assertIn('共享完成，来源：普通成功群', body)
        self.assertNotIn('private bot chatter', body)

    def test_draft_warning_stays_visible_even_if_all_checkins_succeed(self):
        cfg = copy.deepcopy(self.cfg)
        cfg['notifications']['only_on_issues'] = True
        with patch.object(notifications, 'send', return_value={'ok': True}) as delivery:
            result = asyncio.run(notifications.notify_round(cfg, self.runtime, only='备用号', chats=['-204']))
        self.assertTrue(result['ok'])
        self.assertEqual(delivery.call_count, 1)
        title, body = delivery.call_args.args
        self.assertIn('草稿异常', title)
        self.assertIn('## 📝 草稿恢复需处理（1）', body)
        self.assertNotIn('/private/path', body)
        self.assertIn('成功 1', delivery.call_args.kwargs['short'])

    def test_cancelled_or_failed_round_is_not_presented_as_completed(self):
        for flag, label in [('cancelled', '已取消'), ('error', '中断')]:
            with self.subTest(flag=flag):
                title, body, issues = notifications.round_message(self.cfg, self.runtime, **{flag: 'sensitive exception details' if flag == 'error' else True})
                self.assertTrue(issues)
                self.assertIn(label, title)
                self.assertNotIn('sensitive exception details', body)
                self.assertNotIn('签到完成', title)

    def test_expired_results_do_not_leak_into_current_day(self):
        self.runtime['_last']['主号']['-101'].update(date='2026-11-03', draft_warning='expired private path')
        title, body, issues = notifications.round_message(self.cfg, self.runtime, only='主号', chats=['-101'])
        self.assertTrue(issues)
        self.assertIn('未执行', title)
        self.assertIn('成功 0', body)
        self.assertNotIn('expired', body)
        self.assertNotIn('## 📝', body)

    def test_account_and_chat_filters_exclude_other_results(self):
        _, body, _ = notifications.round_message(self.cfg, self.runtime, accounts=['备用号'], chats=['-201'])
        self.assertIn('共 1 个会话 · 1 个账号', body)
        self.assertNotIn('主号', body)
        self.assertNotIn('按钮失败群', body)

    def test_names_cannot_inject_markdown_sections_or_html(self):
        cfg = {'accounts': [{'name': 'A*<admin>', 'groups': [{'chat': '-1', 'title': '群\n## forged [link](x)'}]}]}
        _, body, _ = notifications.round_message(cfg, {})
        self.assertNotIn('<admin>', body)
        self.assertNotIn('\n## forged', body)
        self.assertIn(r'A\*&lt;admin&gt;', body)
        self.assertIn(r'\[link\]', body)

    def test_unknown_status_is_failed_instead_of_success(self):
        self.runtime['_last']['主号']['-101']['confirm'] = 'unexpected'
        _, body, issues = notifications.round_message(self.cfg, self.runtime, only='主号', chats=['-101'])
        self.assertTrue(issues)
        self.assertIn('❌ 失败 1', body)
        self.assertNotIn('## ✅', body)

    def test_no_targets_is_not_a_fake_success(self):
        title, body, _ = notifications.round_message({'accounts': []}, {})
        self.assertIn('暂无会话', title)
        self.assertIn('共 0 个会话 · 0 个账号', body)

    def test_large_report_is_bounded_and_indicates_omitted_details(self):
        cfg = {'accounts': [{'name': '账号', 'groups': [{'chat': str(i), 'title': f'{i}'+('示例群名'*30)} for i in range(600)]}]}
        _, body, _ = notifications.round_message(cfg, {})
        self.assertIn('共 600 个会话', body)
        self.assertIn('⏸ 未执行 600', body)
        self.assertLessEqual(len(body.encode('utf-8')), 24000)
        self.assertIn('后续内容已省略', body)

    def test_preview_uses_saved_scope_and_does_not_send_checkins(self):
        self.cfg['schedule'] = {'accounts': ['备用号']}
        with patch.object(notifications, 'send', return_value={'ok': True}) as delivery:
            notifications.send_preview(self.cfg, self.runtime)
        self.assertEqual(delivery.call_count, 1)
        title, body = delivery.call_args.args
        self.assertTrue(title.startswith('【预览】'))
        self.assertIn('不触发签到', body)
        self.assertNotIn('主号', body)
        self.assertEqual(delivery.call_args.kwargs['kind'], 'test')

    def test_serverchan_card_summary_matches_body_and_is_form_encoded(self):
        class Response:
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def read(self,limit):return b'{"code":0}'
        title, body, _ = notifications.round_message(self.cfg, self.runtime)
        short = notifications._card_summary(body)
        with patch.object(notifications.urllib.request, 'urlopen', return_value=Response()) as request:
            result = notifications._post('SCT'+'x'*24, title, body, short=short)
        self.assertTrue(result['ok'])
        fields = parse_qs(request.call_args.args[0].data.decode())
        self.assertEqual(fields['desp'], [body])
        self.assertEqual(fields['short'], [short])
        self.assertLessEqual(len(fields['title'][0]), 32)
        self.assertIn('待核对 2', fields['short'][0])


if __name__ == '__main__':unittest.main()

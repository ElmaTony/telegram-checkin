"""Real Chromium DOM tests, with local fixtures only and no Telegram connection."""
import asyncio
import time
import tempfile
import json
from pathlib import Path
import unittest
import sys
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace

from playwright.async_api import async_playwright, ElementHandle
from web_engine import WebEngine, SEL_COMPOSER
from core.logic import judge_replies


class NavigationRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_transient_connection_close_retries_navigation_only(self):
        engine=WebEngine({'name':'Fixture'}, {})
        engine.page=SimpleNamespace(goto=AsyncMock(side_effect=[RuntimeError('net::ERR_CONNECTION_CLOSED'),None]))
        engine._looks_logged_in_now=AsyncMock(return_value=True)
        with patch('web_engine.asyncio.sleep',AsyncMock()):
            self.assertTrue(await engine.is_logged_in())
        self.assertEqual(engine.page.goto.await_count,2)

    async def test_nontransient_navigation_error_is_not_retried(self):
        engine=WebEngine({'name':'Fixture'}, {})
        engine.page=SimpleNamespace(goto=AsyncMock(side_effect=RuntimeError('net::ERR_CERT_AUTHORITY_INVALID')))
        with self.assertRaisesRegex(RuntimeError,'本次未发送'):
            await engine.is_logged_in()
        self.assertEqual(engine.page.goto.await_count,1)

    async def test_connection_retry_has_a_fixed_limit(self):
        engine=WebEngine({'name':'Fixture'}, {})
        engine.page=SimpleNamespace(goto=AsyncMock(side_effect=RuntimeError('net::ERR_CONNECTION_CLOSED')))
        with patch('web_engine.asyncio.sleep',AsyncMock()),self.assertRaisesRegex(RuntimeError,'本次未发送'):
            await engine.is_logged_in()
        self.assertEqual(engine.page.goto.await_count,3)


class EngineDOMTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.pw=await async_playwright().start()
        self.browser=await self.pw.chromium.launch(**({'channel':'msedge'} if sys.platform=='win32' else {}),headless=True)
        self.page=await self.browser.new_page()
        await self.page.set_content('''<div class="chat-info"><span class="peer-title" data-peer-id="-101">Target</span></div>
            <div contenteditable="true" class="input-message-input" data-peer-id="-101"></div><div class="bubbles"><div class="bubbles-inner no-messages"></div></div>''')
        self.engine=WebEngine({'name':'Fixture'}, {})
        self.engine.page=self.page
        self.engine._mids_chat='-101'
        self.engine._expected_peer='-101'
        self.draft_directory=tempfile.TemporaryDirectory(prefix='draft-test-',dir=Path(__file__).resolve().parents[1]/'audit')
        self.engine.profile=Path(self.draft_directory.name)

    async def asyncTearDown(self):
        await self.browser.close()
        await self.pw.stop()
        self.draft_directory.cleanup()

    async def test_dialog_scan_waits_for_active_list_after_login(self):
        self.engine.ensure_login = AsyncMock()
        await self.page.set_content('''
            <div class="folders-tabs-scrollable">
              <button data-filter-id="0" aria-selected="true" aria-controls="all-dialogs">All</button>
            </div>
            <div id="chatlist-container">
              <div id="all-dialogs" class="tabs-tab active scrollable-y">
                <canvas class="dialogs-placeholder-canvas"></canvas><ul class="chatlist"></ul>
              </div>
              <div class="tabs-tab inactive"><ul class="chatlist">
                <li class="chatlist-chat" data-peer-id="-999"><span class="peer-title">Wrong folder</span></li>
              </ul></div>
            </div>
            <script>
              const list=document.querySelector('#all-dialogs .chatlist');
              setTimeout(()=>list.innerHTML=
                '<li class="chatlist-chat" data-peer-id="-201"><span class="is-forum"></span><span class="peer-title">Forum</span></li>',3300);
              setTimeout(()=>list.insertAdjacentHTML('beforeend',
                '<li class="chatlist-chat" data-peer-id="-202"><span class="peer-title">Later group</span></li>'),5100);
            </script>
        ''')
        self.assertEqual(await self.engine.list_chats(), [
            {'id':'-201','title':'Forum','is_forum':True,'type':'论坛群 · 含话题'},
            {'id':'-202','title':'Later group','is_forum':False,'type':'群组/频道'}])

    async def test_dialog_scan_distinguishes_loading_from_explicit_empty_folder(self):
        await self.page.set_content('''<div id="chatlist-container"><div class="tabs-tab active chatlist-parts">
          <canvas class="dialogs-placeholder-canvas"></canvas><ul class="chatlist"></ul></div></div>''')
        with self.assertRaisesRegex(RuntimeError,'未返回会话列表'):
            await self.engine._wait_dialog_list(timeout=200)
        await self.page.locator('.chatlist-parts').evaluate('''el=>{
            el.dataset.placeholderType='folder';el.innerHTML+='<div class="empty-placeholder">Empty</div>';
        }''')
        self.engine.ensure_login = AsyncMock()
        self.assertEqual(await self.engine.list_chats(), [])

    async def test_dialog_scan_recovers_when_virtual_list_jumps_to_bottom(self):
        self.engine.ensure_login = AsyncMock()
        await self.page.set_content('''
            <div id="chatlist-container" class="scrollable-y" style="height:120px;overflow:auto">
              <ul class="chatlist" style="margin:0;height:480px;position:relative;list-style:none"></ul>
            </div>
            <script>
              const scroller=document.querySelector('.scrollable-y');
              const list=scroller.querySelector('ul');
              let jumped=false;
              function render(){
                if(!jumped&&scroller.scrollTop>=140){
                  jumped=true;
                  scroller.scrollTop=scroller.scrollHeight;
                }
                const first=Math.floor(scroller.scrollTop/20);
                list.replaceChildren();
                for(let i=first;i<Math.min(first+7,24);i++){
                  const row=document.createElement('li');
                  row.className='chatlist-chat';
                  row.dataset.peerId=String(-300-i);
                  row.style.cssText='position:absolute;height:20px;top:'+i*20+'px';
                  row.innerHTML='<span class="peer-title">Fixture Group</span>';
                  list.append(row);
                }
              }
              scroller.addEventListener('scroll',render);
              render();
            </script>
        ''')
        dialogs = await self.engine.list_chats()
        self.assertEqual({row['id'] for row in dialogs}, {str(-300-i) for i in range(24)})

    async def bubble(self,mid,text,reply=None,age=0,out=False):
        await self.page.evaluate('''({mid,text,reply,age,out})=>{
            const b=document.createElement('div');b.className='bubble '+(out?'is-out':'is-in');
            b.dataset.mid=mid;b.dataset.peerId='-101';b.dataset.timestamp=Math.floor(Date.now()/1000)-age;
            if(reply)b.dataset.replyToMid=reply;const m=document.createElement('div');m.className='message';m.textContent=text;b.append(m);
            const pane=document.querySelector('.bubbles-inner')||document.querySelector('.bubbles');pane.classList.remove('no-messages');pane.append(b);
        }''',{'mid':mid,'text':text,'reply':reply,'age':age,'out':out})

    async def test_button_only_clicks_newest_exact_button_and_reads_popup(self):
        self.engine._expected_peer = self.engine._mids_chat = '701'
        await self.page.set_content('''
          <div class="chat-info"><span class="peer-title" data-peer-id="701">Example</span></div>
          <div contenteditable="true" class="input-message-input" data-peer-id="701"></div>
          <div class="bubbles">
            <div class="bubble is-in" data-peer-id="701" data-mid="1"><button class="reply-markup-button" id="old">✅ 点我签到</button></div>
            <div class="bubble is-in" data-peer-id="701" data-mid="2">
              <button class="reply-markup-button" id="latest"><span class="reply-markup-button-text"><img alt="✅"> 点我签到</span></button>
              <button class="reply-markup-button" id="disable">关闭提醒</button>
            </div>
          </div>
          <script>
            const rows=[...document.querySelectorAll('.bubble')];
            const now=Math.floor(Date.now()/1000);
            rows[0].dataset.timestamp=now-10;
            rows[1].dataset.timestamp=now;
            window.clicked=[];
            document.querySelector('#old').onclick=()=>window.clicked.push('old');
            document.querySelector('#disable').onclick=()=>window.clicked.push('disable');
            document.querySelector('#latest').onclick=()=>{
              window.clicked.push('latest');
              setTimeout(()=>{
                const popup=document.createElement('div');
                popup.className='popup popup-confirmation';
                popup.innerHTML='<p class="popup-description">签到成功，获得 8 积分</p><div class="popup-buttons"><button>确定</button></div>';
                popup.querySelector('button').onclick=()=>popup.remove();
                document.body.append(popup);
              },80);
            };
          </script>
        ''')
        self.engine.before_send = AsyncMock()
        self.assertEqual(await self.engine.click_existing_button('✅ 点我签到', None, time.time()-3600), '2')
        self.engine.before_send.assert_awaited_once()
        self.assertEqual(await self.engine.read_button_popup(1.5), '签到成功，获得 8 积分')
        self.assertEqual(await self.page.evaluate('window.clicked'), ['latest'])
        self.assertEqual(await self.page.locator('.popup-confirmation').count(), 0)

    async def test_button_only_rejects_link_to_a_different_peer(self):
        with self.assertRaisesRegex(ValueError, '会话不符'):
            await self.engine.click_existing_button('Confirm', 'https://t.me/c/123/17', 0)

    async def test_hash_does_not_override_visible_peer_and_send_is_blocked(self):
        await self.page.evaluate("location.hash='-999'")
        self.assertFalse(await self.engine._wait_opened('-999',timeout=.02))
        self.engine._expected_peer='-999'
        with self.assertRaisesRegex(RuntimeError,'目标会话核对失败'):
            await self.engine.send_text('must not send')
        self.assertEqual(await self.page.locator('[contenteditable]').inner_text(),'')

    async def test_delayed_topic_render_registers_sent_id_without_resending(self):
        self.engine._capture_by_chat['-101']=await self.engine.snapshot_incoming()
        await self.page.evaluate('''()=>setTimeout(()=>{const b=document.createElement('div');b.className='bubble is-out';b.dataset.mid='999';b.dataset.peerId='-101';b.dataset.timestamp=Math.floor(Date.now()/1000);b.innerHTML='<span class="message">签到</span>';document.querySelector('.bubbles').append(b)},3200)''')
        started=time.monotonic()
        await self.engine._track_sent_mid(None)
        self.assertIn('999',self.engine.last_sent_mids)
        self.assertGreater(time.monotonic()-started,3)
        self.assertLess(time.monotonic()-started,6)
        self.assertEqual(await self.page.locator('.bubble.is-out').count(),1)

    async def test_temporary_id_waits_for_server_id_before_leaving_chat(self):
        self.engine._capture_by_chat['-101']=await self.engine.snapshot_incoming()
        await self.bubble('500.0001','签到',out=True)
        await self.page.evaluate('''()=>setTimeout(()=>{
            const b=document.querySelector('[data-mid="500.0001"]');b.dataset.mid='501';
            const reply=document.createElement('div');reply.className='bubble is-in';reply.dataset.mid='502';reply.dataset.peerId='-101';reply.dataset.replyToMid='501';reply.dataset.timestamp=Math.floor(Date.now()/1000);reply.innerHTML='<span class="message">签到成功</span>';document.querySelector('.bubbles').append(reply);
        },300)''')
        started=time.monotonic()
        await self.engine._track_sent_mid(None)
        self.assertGreater(time.monotonic()-started,.2)
        self.assertIn('501',self.engine.last_sent_mids)
        await self.engine.watch_new_incoming(self.engine._capture_by_chat['-101'],1.5,'reply_only',idle_after=.05)
        self.assertEqual(self.engine.last_replied,['签到成功'])

    async def test_hidden_stale_title_is_not_a_visible_peer(self):
        await self.page.evaluate('''()=>{const d=document.createElement('div');d.className='chat-info';d.hidden=true;d.innerHTML='<span class="peer-title" data-peer-id="-999">Stale</span>';document.body.prepend(d)}''')
        self.assertEqual(await self.engine._opened_peer(),'-101')

    async def test_header_and_composer_must_agree(self):
        await self.page.locator('[contenteditable]').evaluate("el=>el.dataset.peerId='-999'")
        self.assertIsNone(await self.engine._opened_peer())

    async def test_topic_match_is_exact_not_substring(self):
        self.engine._expected_topic='Daily'
        self.engine._current_topic=AsyncMock(return_value='Daily unrelated')
        with self.assertRaisesRegex(RuntimeError,'话题核对失败'):
            await self.engine.assert_destination()

    async def test_draft_is_not_appended_or_sent(self):
        await self.page.locator('[contenteditable]').fill('existing draft')
        with self.assertRaisesRegex(RuntimeError,'已有草稿'):
            await self.engine.send_text('new command')
        self.assertEqual(await self.page.locator('[contenteditable]').inner_text(),'existing draft')

    async def test_same_text_new_mid_is_captured_and_old_loaded_mid_is_excluded(self):
        await self.bubble('100','签到成功',age=100)
        boundary=await self.engine.snapshot_incoming()
        self.engine.last_sent_mids={'501'}
        await self.bubble('101','old newly loaded text',age=100)
        await self.bubble('502','签到成功',reply='501')
        found=await self.engine.watch_new_incoming(boundary,1.5,idle_after=.05)
        self.assertEqual(found,['签到成功'])
        self.assertEqual(self.engine.last_replied,['签到成功'])

    async def test_unrelated_success_is_not_a_trusted_result(self):
        boundary=await self.engine.snapshot_incoming()
        self.engine.last_sent_mids={'501'}
        await self.bubble('502','签到成功',reply='someone-else')
        found=await self.engine.watch_new_incoming(boundary,1.5,'after',idle_after=.05)
        self.assertEqual(judge_replies({}, {},found,self.engine.last_replied),'unconfirmed')

    async def test_reactions_and_delivery_status_are_not_part_of_message_text(self):
        await self.bubble('502','',reply='501')
        await self.page.locator('[data-mid="502"] .message').evaluate('''el=>el.innerHTML='<div data-message-text-source>签到成功</div><reactions-element>other user</reactions-element><span class="time">19:2219:22</span>' ''')
        data=await self.page.evaluate(self.engine._IN_COLLECT_JS,{'peer':'-101','reset':True})
        self.assertEqual(data['items'][0]['t'],'签到成功')

    async def test_same_text_is_promoted_when_reply_attribution_arrives_late(self):
        boundary=await self.engine.snapshot_incoming()
        await self.bubble('502','签到成功')
        await self.engine.watch_new_incoming(boundary,.1,'after',sent_mids=['501'])
        self.assertEqual(self.engine.last_replied,[])
        await self.page.locator('[data-mid="502"]').evaluate("el=>el.dataset.replyToMid='501'")
        await self.engine.watch_new_incoming(boundary,.3,'after',sent_mids=['501'],idle_after=.02)
        self.assertEqual(self.engine.last_replied,['签到成功'])
        self.assertEqual(self.engine.last_evidence[0]['source'],'reply_to_sent')

    async def test_readonly_history_loads_original_window_without_touching_draft(self):
        await self.page.locator('[contenteditable]').fill('keep this draft')
        cutoff=time.time()-600
        await self.bubble('900','latest unrelated')
        await self.page.evaluate('''cutoff=>{
            const sc=document.createElement('div');sc.className='bubbles-scrollable';sc.style='height:80px;overflow:auto';
            const space=document.createElement('div');space.style.height='500px';sc.append(space);
            document.querySelector('.bubbles').append(sc);
            sc.addEventListener('scroll',()=>{
                if(window.loadedHistory)return;window.loadedHistory=true;
                const b=document.createElement('div');b.className='bubble is-in';
                Object.assign(b.dataset,{mid:'499',peerId:'-101',timestamp:String(Math.floor(cutoff-10))});
                b.innerHTML='<div class="message">older history</div>';document.querySelector('.bubbles').append(b);
            });
        }''',cutoff)
        data=await self.engine.collect_feedback_history({'peer':'-101','cutoff':cutoff},seconds=1)
        self.assertIn('499',[item['mid'] for item in data['items']])
        self.assertEqual(await self.page.locator('[contenteditable]').inner_text(),'keep this draft')

    async def test_readonly_recovery_requires_unique_own_outgoing_text(self):
        known={'peer':'-101','cutoff':time.time()-2,'seen':['100']}
        await self.bubble('100','签到',out=True)
        await self.bubble('501','签到',out=True)
        recovery=await self.engine.recover_sent_mids(known,'签到')
        self.assertEqual(recovery['sent_mids'],['501'])
        await self.bubble('503','签到',out=True)
        self.assertEqual((await self.engine.recover_sent_mids(known,'签到'))['sent_mids'],[])

    async def test_recovery_rejects_other_accounts_old_replies_and_ambiguous_ids(self):
        self.engine._self_peer='777'
        cutoff=time.time()-5
        for mentions,reply,age,second in [('888','501',0,False),('777','100',0,False),('777','501',100,False),('777','501',0,True)]:
            with self.subTest(mentions=mentions,reply=reply,age=age,second=second):
                await self.page.locator('.bubbles').evaluate('el=>el.innerHTML=""')
                await self.page.evaluate(self.engine._IN_COLLECT_JS,{'peer':'-101','reset':True})
                await self.bubble('502','',reply=reply,age=age)
                await self.page.locator('[data-mid="502"] .message').evaluate('(el,peer)=>el.innerHTML=`<div data-message-text-source><a data-follow="${peer}">Fixture</a> 签到成功</div>`',mentions)
                if second:
                    await self.bubble('504','',reply='503')
                    await self.page.locator('[data-mid="504"] .message').evaluate('el=>el.innerHTML=\'<a data-follow="777">Fixture</a> 签到成功\'')
                result=await self.engine.recover_sent_mids({'peer':'-101','cutoff':cutoff,'seen':['100']},'签到')
                self.assertEqual(result['sent_mids'],[])

    async def test_deleted_command_can_be_recovered_from_reply_naming_current_account(self):
        self.engine._self_peer='777'
        known={'peer':'-101','cutoff':time.time()-2,'sent_at':time.time()-2,'seen':[]}
        await self.bubble('502','',reply='501')
        await self.page.locator('[data-mid="502"] .message').evaluate('el=>el.innerHTML=\'<div data-message-text-source><a data-follow="777">Fixture</a> 签到成功</div>\'')
        recovered=await self.engine.recover_sent_mids(known,'签到')
        self.assertEqual(recovered['sent_mids'],['501'])
        self.assertEqual(recovered['source'],'account_mention_reply')
        await self.engine.watch_new_incoming(known,.2,'after',sent_mids=[],idle_after=.02)
        self.assertEqual(self.engine.last_replied,['Fixture 签到成功'])
        self.assertEqual(self.engine.last_allowed_mids,['501'])

    async def test_current_account_is_read_from_active_menu_avatar_and_keeps_draft(self):
        await self.page.locator('[contenteditable]').fill('original draft')
        await self.page.evaluate('''()=>{
            const toggle=document.createElement('button');toggle.className='sidebar-tools-button';toggle.setAttribute('aria-expanded','false');
            const menu=document.createElement('div');menu.setAttribute('role','menu');menu.hidden=true;
            menu.innerHTML='<div class="avatar active" data-peer-id="777">Current</div><div class="avatar" data-peer-id="888">Other</div>';
            toggle.onclick=()=>{menu.hidden=false;toggle.setAttribute('aria-expanded','true')};
            document.addEventListener('keydown',e=>{if(e.key==='Escape'){menu.hidden=true;toggle.setAttribute('aria-expanded','false')}});
            document.body.append(toggle,menu);
        }''')
        self.assertEqual(await self.engine.current_user_peer(),'777')
        self.assertEqual(await self.page.locator('[contenteditable]').inner_text(),'original draft')
        self.assertEqual(await self.page.locator('.sidebar-tools-button').get_attribute('aria-expanded'),'false')

    async def test_reply_to_previous_action_does_not_confirm_current_action(self):
        boundary=await self.engine.snapshot_incoming()
        self.engine.last_sent_mids={'501','601'}
        await self.bubble('602','签到成功',reply='501')
        await self.engine.watch_new_incoming(boundary,1.5,'after',idle_after=.05,sent_mids=['601'])
        self.assertEqual(self.engine.last_replied,[])

    async def test_temporary_mid_alias_is_tracked_without_trusting_old_outgoing(self):
        boundary=await self.engine.snapshot_incoming()
        self.engine.last_sent_mids={'501.0001'}
        await self.bubble('501.0001','my command',out=True)
        await self.page.evaluate('''async()=>{await new Promise(r=>setTimeout(r,0));document.querySelector('[data-mid="501.0001"]').dataset.mid='501'}''')
        await self.bubble('502','签到成功',reply='501')
        found=await self.engine.watch_new_incoming(boundary,1.5,idle_after=.05,sent_mids=['501.0001'])
        self.assertEqual(found,['签到成功'])
        self.assertIn('501',self.engine.last_allowed_mids)

    async def test_old_outgoing_ids_are_not_registered_as_this_round(self):
        for mid in ['100','200','300']: await self.bubble(mid,'old command',age=100,out=True)
        boundary=await self.engine.snapshot_incoming()
        self.engine.last_sent_mids={'501'}
        await self.bubble('502','签到成功',reply='300')
        await self.engine.watch_new_incoming(boundary,1.5,'after',idle_after=.05)
        self.assertEqual(self.engine.last_sent_mids,{'501'})
        self.assertEqual(self.engine.last_replied,[])

    async def test_edited_new_reply_is_collected_again_by_message_version(self):
        boundary=await self.engine.snapshot_incoming()
        self.engine.last_sent_mids={'501'}
        await self.bubble('502','处理中',reply='501')
        await self.engine.watch_new_incoming(boundary,1.5,idle_after=.05)
        await self.page.locator('[data-mid="502"] .message').evaluate("el=>el.textContent='签到成功'")
        found=await self.engine.watch_new_incoming(boundary,1.5,idle_after=.05)
        self.assertEqual(found,['签到成功'])

    async def test_intermediate_reply_does_not_end_feedback_window_early(self):
        boundary=await self.engine.snapshot_incoming()
        self.engine.last_sent_mids={'501'}
        await self.bubble('502','正在处理请求',reply='501')
        await self.page.locator('[data-mid="502"] .message').evaluate("el=>setTimeout(()=>el.textContent='签到成功',350)")
        start=time.monotonic()
        found=await self.engine.watch_new_incoming(boundary,1.3,idle_after=.05,
                    is_complete=lambda trusted:judge_replies({}, {}, trusted, trusted)!='unconfirmed')
        self.assertIn('签到成功',found)
        self.assertGreaterEqual(time.monotonic()-start,.35)
        self.assertEqual(judge_replies({}, {},found,self.engine.last_replied),'success')

    async def test_final_feedback_still_ends_before_full_window(self):
        boundary=await self.engine.snapshot_incoming()
        self.engine.last_sent_mids={'501'}
        await self.bubble('502','签到成功',reply='501')
        start=time.monotonic()
        await self.engine.watch_new_incoming(boundary,2,idle_after=.05,
                    is_complete=lambda trusted:judge_replies({}, {}, trusted, trusted)!='unconfirmed')
        self.assertLess(time.monotonic()-start,1.5)

    async def test_observer_remembers_reply_removed_before_next_poll(self):
        boundary=await self.engine.snapshot_incoming()
        self.engine.last_sent_mids={'501'}
        await self.bubble('502','签到成功',reply='501')
        await self.page.evaluate('''async()=>{await new Promise(r=>setTimeout(r,0));document.querySelector('[data-mid="502"]').remove()}''')
        self.assertEqual(await self.engine.watch_new_incoming(boundary,1.5,idle_after=.05),['签到成功'])

    async def test_feedback_deadline_bounds_slow_dom_evaluation(self):
        boundary=await self.engine.snapshot_incoming()
        async def delayed(*_): await asyncio.sleep(10)
        original=self.engine.page
        self.engine.page=type('SlowPage',(),{'evaluate':staticmethod(delayed)})()
        start=time.monotonic()
        await self.engine.watch_new_incoming(boundary,.05)
        self.assertLess(time.monotonic()-start,.3)
        self.engine.page=original

    async def test_historical_button_is_not_clicked(self):
        await self.bubble('100','old prompt',reply='501',age=100)
        await self.page.locator('[data-mid="100"]').evaluate('''el=>{const b=document.createElement('button');b.className='reply-markup-button';b.textContent='Confirm';b.onclick=()=>window.wasClicked=true;el.append(b)}''')
        await self.engine.snapshot_incoming()
        self.engine.last_sent_mids={'501'}
        self.assertFalse(await self.engine.click_button('Confirm',timeout=.1))
        self.assertFalse(await self.page.evaluate('window.wasClicked===true'))

    async def test_deleted_outgoing_command_keeps_id_and_trusts_its_reply(self):
        boundary=await self.engine.snapshot_incoming()
        await self.page.evaluate('''()=>{
            const b=document.createElement('div');b.className='bubble is-out';
            b.dataset.mid='501';b.dataset.peerId='-101';b.dataset.timestamp=Math.floor(Date.now()/1000);
            b.textContent='签到';document.querySelector('.bubbles').append(b);b.remove();
        }''')
        self.assertIsNone(await self.engine._last_out_mid())
        await self.engine._track_sent_mid(None)
        self.assertEqual(self.engine.last_sent_mids,{'501'})
        await self.bubble('502','签到成功',reply='501')
        self.assertEqual(await self.engine.watch_new_incoming(boundary,1.5,idle_after=.05),['签到成功'])
        self.assertEqual(self.engine.last_replied,['签到成功'])

    async def test_reply_added_and_removed_in_same_dom_batch_is_retained(self):
        boundary=await self.engine.snapshot_incoming()
        self.engine.last_sent_mids={'501'}
        await self.page.evaluate('''()=>{
            const b=document.createElement('div');b.className='bubble is-in';
            Object.assign(b.dataset,{mid:'502',peerId:'-101',replyToMid:'501',timestamp:String(Math.floor(Date.now()/1000))});
            b.innerHTML='<div class="message">签到成功</div>';document.querySelector('.bubbles').append(b);b.remove();
        }''')
        self.assertEqual(await self.engine.watch_new_incoming(boundary,1.5,idle_after=.05),['签到成功'])

    async def test_navigation_closes_forum_drawer_before_clicking_chat(self):
        await self.page.evaluate('''()=>{
            document.querySelector('.peer-title').dataset.peerId='-999';document.querySelector('[contenteditable]').dataset.peerId='-999';
            const ul=document.createElement('ul');ul.className='chatlist';ul.innerHTML='<a class="chatlist-chat" data-peer-id="-101" href="#-101">Target</a>';
            ul.querySelector('a').onclick=()=>{document.querySelector('.peer-title').dataset.peerId='-101';document.querySelector('[contenteditable]').dataset.peerId='-101'};document.body.append(ul);
            const panel=document.createElement('div');panel.className='topics-slider';panel.style='position:fixed;inset:0;background:white;z-index:100';
            panel.innerHTML='<button class="sidebar-close-button">Back</button>';panel.querySelector('button').onclick=()=>panel.remove();document.body.append(panel);
        }''')
        await self.engine.open_chat('-101','Target')
        self.assertEqual(await self.engine._opened_peer(),'-101')
        self.assertEqual(await self.page.locator('.topics-slider').count(),0)

    async def test_different_chat_routes_before_waiting_for_its_identity(self):
        await self.page.evaluate('''()=>addEventListener('hashchange',()=>{
            document.querySelector('.peer-title').dataset.peerId='-102';
            document.querySelector('[contenteditable]').dataset.peerId='-102';
        })''')
        observed=[];original=self.engine._wait_opened
        async def wait(*args,**kwargs):
            observed.append(self.page.url.split('#')[-1])
            return await original(*args,**kwargs)
        with patch.object(self.engine,'_wait_opened',side_effect=wait):
            await self.engine.open_chat('-102','Other fixture')
        self.assertTrue(observed)
        self.assertTrue(all(fragment=='-102' for fragment in observed))
        self.assertEqual(await self.engine.assert_destination(),'-102')

    async def test_already_open_chat_keeps_its_draft_without_routing_again(self):
        await self.page.locator('[contenteditable]').fill('Fixture draft')
        previous=self.page.url
        await self.engine.open_chat('-101','Target')
        self.assertEqual(self.page.url,previous)
        self.assertEqual(await self.page.locator('[contenteditable]').inner_text(),'Fixture draft')

    async def _navigation_fallback_fixture(self, final_peer):
        await self.page.evaluate('''()=>{
            document.querySelector('.peer-title').dataset.peerId='-999';
            document.querySelector('[contenteditable]').dataset.peerId='-999';
        }''')
        original_wait=self.engine._wait_opened
        async def short_wait(ref, timeout=6):
            return await original_wait(ref,timeout=.02)
        calls=0
        async def search(*args):
            nonlocal calls
            calls+=1
            if calls==2:
                await self.page.evaluate('''peer=>{
                    document.querySelector('.peer-title').dataset.peerId=peer;
                    document.querySelector('[contenteditable]').dataset.peerId=peer;
                }''',final_peer)
            return False
        self.engine._wait_opened=short_wait
        self.engine._open_visible_chat=AsyncMock(return_value=False)
        self.engine._scroll_find=AsyncMock(return_value=None)
        self.engine._open_by_search=search
        self.page.reload=AsyncMock()

    async def test_chat_rendered_after_last_search_budget_is_verified_before_failing(self):
        await self._navigation_fallback_fixture('-101')
        await self.engine.open_chat('-101','Late fixture')
        self.assertEqual(await self.engine.assert_destination(),'-101')
        self.assertEqual(await self.page.locator('[contenteditable]').inner_text(),'')

    async def test_final_navigation_check_rejects_a_different_peer(self):
        await self._navigation_fallback_fixture('-998')
        with self.assertRaisesRegex(RuntimeError,'目标 -101，当前 -998；未发送'):
            await self.engine.open_chat('-101','Same title')
        self.assertEqual(self.engine._last_actual_peer,'-998')
        self.assertEqual(await self.page.locator('[contenteditable]').inner_text(),'')

    async def test_opened_peer_without_ready_composer_reports_input_problem(self):
        await self._navigation_fallback_fixture('-101')
        original_first=self.engine._first
        async def missing_composer(selectors,*args,**kwargs):
            if selectors==SEL_COMPOSER:
                raise RuntimeError('Fixture composer unavailable')
            return await original_first(selectors,*args,**kwargs)
        self.engine._first=missing_composer
        with self.assertRaisesRegex(RuntimeError,'发送输入框尚未就绪或不可用；本次未发送'):
            await self.engine.open_chat('-101','Late fixture')
        self.assertEqual(self.engine._last_actual_peer,'-101')
        self.assertEqual(await self.page.locator('[contenteditable]').inner_text(),'')

    async def test_exact_active_sidebar_row_is_clicked_before_hash_fallback(self):
        await self.page.evaluate('''()=>{
            document.querySelector('.peer-title').dataset.peerId='-999';document.querySelector('[contenteditable]').dataset.peerId='-999';
            const rows=document.createElement('div');rows.innerHTML='<div class="tabs-tab"><button class="chatlist-chat" data-peer-id="-101">Cached</button></div><div class="tabs-tab active"><button class="chatlist-chat" data-peer-id="-999">Same title</button><button class="chatlist-chat" data-peer-id="-101">Same title</button></div>';
            rows.querySelector('.tabs-tab:not(.active) button').onclick=()=>window.wrongRow=true;
            rows.querySelector('[data-peer-id="-999"]').onclick=()=>window.wrongRow=true;
            rows.querySelector('.active [data-peer-id="-101"]').onclick=()=>{window.exactRow=true;document.querySelector('.peer-title').dataset.peerId='-101';document.querySelector('[contenteditable]').dataset.peerId='-101'};
            document.body.append(rows);
        }''')
        previous=self.page.url
        await self.engine.open_chat('-101','Same title')
        self.assertTrue(await self.page.evaluate('!!window.exactRow'))
        self.assertFalse(await self.page.evaluate('!!window.wrongRow'))
        self.assertEqual(self.page.url,previous)
        self.assertEqual(await self.engine.assert_destination(),'-101')

    async def media_fixture(self,send=True,hide_on_send=True):
        image=self.engine.profile/'fixture.png'
        import base64
        image.write_bytes(base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl2nGAAAAAASUVORK5CYII='))
        await self.page.evaluate('''({send,hide})=>{
            window.imagesSent=0;window.previewCloses=0;
            const old=document.createElement('div');old.className='popup popup-new-media';old.style.display='none';
            old.innerHTML='<button class="simple-message-input-confirm">Old cached send</button>';
            old.querySelector('button').onclick=()=>window.wrongPreviewSent=true;document.body.append(old);
            const input=document.createElement('input');input.type='file';input.style.display='none';document.body.append(input);
            const menu=document.createElement('button');menu.className='btn-menu-item';menu.textContent='Photo or Video';menu.hidden=true;
            menu.onclick=()=>input.click();document.body.append(menu);
            const attach=document.createElement('button');attach.className='attach-file';attach.textContent='Attach';
            attach.onclick=()=>menu.hidden=false;document.body.append(attach);
            input.onchange=()=>{
                menu.hidden=true;
                const popup=document.createElement('div');popup.className='popup popup-send-photo popup-new-media active';
                popup.innerHTML='<button class="popup-close" aria-label="Close">Close</button><div contenteditable="true" class="fixture-caption"></div>';
                popup.querySelector('.popup-close').onclick=()=>{window.previewCloses++;popup.remove()};
                if(send){const button=document.createElement('button');button.className='btn-primary simple-message-input-confirm';button.textContent='Send';
                    button.onclick=()=>{window.imagesSent++;window.sentCaption=popup.querySelector('.fixture-caption').textContent;if(hide)popup.remove()};popup.append(button)}
                document.body.append(popup);
            };
        }''',{'send':send,'hide':hide_on_send})
        return image

    async def test_modern_media_button_sends_owned_preview_without_fixed_waits(self):
        image=await self.media_fixture();start=time.monotonic()
        with patch.object(self.engine,'_track_sent_mid',AsyncMock()):
            await self.engine.send_image(image,'Fixture caption')
        self.assertEqual(await self.page.evaluate('imagesSent'),1)
        self.assertEqual(await self.page.evaluate('sentCaption'),'Fixture caption')
        self.assertFalse(await self.page.evaluate('!!window.wrongPreviewSent'))
        self.assertLess(time.monotonic()-start,3)
        self.assertIsNone(self.engine.last_draft_warning)

    async def test_failed_media_preparation_closes_preview_before_next_navigation(self):
        image=await self.media_fixture(send=False)
        with self.assertRaisesRegex(RuntimeError,'发送按钮不可用'):
            await self.engine.send_image(image,None)
        self.assertEqual(await self.page.evaluate('imagesSent'),0)
        self.assertEqual(await self.page.locator(self.engine.MEDIA_PREVIEW_SELECTOR).count(),0)
        self.assertIsNone(self.engine.send_started_at)
        await self.page.evaluate('''()=>addEventListener('hashchange',()=>{
            document.querySelector('.peer-title').dataset.peerId='-102';document.querySelector('.input-message-input').dataset.peerId='-102';
        })''')
        await self.engine.open_chat('-102','Next fixture')
        self.assertEqual(await self.engine.assert_destination(),'-102')

    async def test_cancelled_media_attempt_closes_preview_and_restores_original_draft(self):
        image=await self.media_fixture();composer=self.page.locator('.input-message-input');await composer.fill('Original fixture draft')
        self.engine.before_send=AsyncMock(side_effect=asyncio.CancelledError)
        with self.assertRaises(asyncio.CancelledError):
            async with self.engine.preserve_draft():
                await self.engine.send_image(image,'Fixture caption')
        self.assertEqual(await self.page.evaluate('imagesSent'),0)
        self.assertEqual(await self.page.locator(self.engine.MEDIA_PREVIEW_SELECTOR).count(),0)
        self.assertEqual(await composer.inner_text(),'Original fixture draft')
        self.assertFalse(list(self.engine.profile.glob('checkin-drafts/*.json')))

    async def test_existing_user_media_preview_is_preserved_and_navigation_fails_fast(self):
        image=await self.media_fixture()
        await self.page.evaluate("()=>{const p=document.createElement('div');p.className='popup popup-new-media active';p.id='user-preview';p.textContent='User attachment';document.body.append(p)}")
        with self.assertRaisesRegex(RuntimeError,'未关闭的图片预览'):
            await self.engine.send_image(image,None)
        with self.assertRaisesRegex(RuntimeError,'未关闭的图片预览'):
            await self.engine.open_chat('-102','Next fixture')
        self.assertEqual(await self.page.locator('#user-preview').count(),1)
        self.assertEqual(await self.page.evaluate('imagesSent'),0)

    async def test_media_click_without_acknowledgement_remains_sent_but_uncertain(self):
        image=await self.media_fixture(hide_on_send=False)
        with self.assertRaisesRegex(RuntimeError,'图片发送结果待核对') as caught:
            await self.engine.send_image(image,None)
        self.assertTrue(caught.exception.sent)
        self.assertTrue(caught.exception.uncertain)
        self.assertEqual(await self.page.evaluate('imagesSent'),1)
        self.assertEqual(await self.page.locator(self.engine.MEDIA_PREVIEW_SELECTOR).count(),0)

    async def test_chat_search_uses_exact_peer_instead_of_matching_title(self):
        await self.page.evaluate('''()=>{
            document.querySelector('.peer-title').dataset.peerId='-999';document.querySelector('[contenteditable]').dataset.peerId='-999';
            const d=document.createElement('div');d.innerHTML='<input class="input-search-input"><div class="search-super"><div class="row" data-peer-id="-999">Same title</div><div class="row" data-peer-id="-101">Same title</div></div>';
            d.querySelector('[data-peer-id="-999"]').onclick=()=>window.wrongClicked=true;
            d.querySelector('[data-peer-id="-101"]').onclick=()=>{document.querySelector('.peer-title').dataset.peerId='-101';document.querySelector('[contenteditable]').dataset.peerId='-101'};document.body.append(d);
        }''')
        self.assertTrue(await self.engine._open_by_search('Same title',-101))
        self.assertFalse(await self.page.evaluate('window.wrongClicked===true'))

    async def forum(self, rows=None, peer='-101', delay=0):
        rows=rows or [{'id':'4294967297','title':'讨论区'}]
        await self.page.evaluate('''({rows,peer,delay})=>{
            document.querySelector('.chat-info .peer-title').textContent='Forum';
            document.querySelector('.chat-info .peer-title').dataset.threadId='0';
            const drawer=document.createElement('div');drawer.className='topics-slider';
            drawer.style='margin-left:500px';drawer.innerHTML='<div class="topics-container"><div class="sidebar-header__title"><span class="peer-title"></span></div></div>';
            drawer.querySelector('.sidebar-header__title .peer-title').dataset.peerId=peer;
            window.topicClicks=[];
            for(const row of rows){
                const a=document.createElement('a');a.dataset.peerId=peer;a.dataset.threadId=row.id;
                a.innerHTML='<div class="row-title"><span class="peer-title"></span></div>';a.querySelector('span').textContent=row.title;
                a.onmousedown=()=>{window.topicClicks.push(row.id);setTimeout(()=>{
                    const title=document.querySelector('.chat-info .peer-title');title.dataset.peerId=peer;title.dataset.threadId=row.id;title.textContent=row.title;
                    document.querySelector('[contenteditable]').dataset.peerId=peer;
                },delay)};drawer.querySelector('.topics-container').append(a);
            }
            document.body.append(drawer);
        }''',{'rows':rows,'peer':peer,'delay':delay})

    async def test_topic_selection_is_exact_and_not_based_on_screen_coordinates(self):
        await self.forum([{'id':'20','title':'讨论区2'},{'id':'21','title':'讨论区'}])
        self.engine._expected_topic='讨论区'
        started=time.monotonic()
        self.assertTrue(await self.engine._enter_topic('讨论区'))
        self.assertLess(time.monotonic()-started,1)
        self.assertEqual(await self.page.evaluate('window.topicClicks'),['21'])
        self.assertEqual(self.engine._expected_topic_id,'21')
        self.assertEqual(await self.engine.assert_destination(),'-101')

    async def test_topic_drawer_can_switch_before_old_chat_peer_changes(self):
        await self.forum()
        await self.page.locator('.chat-info .peer-title').evaluate("el=>el.dataset.peerId='-999'")
        await self.page.locator('[contenteditable]').evaluate("el=>el.dataset.peerId='-999'")
        started=time.monotonic()
        await self.engine.open_chat('-101','Forum','讨论区','签到')
        self.assertLess(time.monotonic()-started,1)
        self.assertEqual(await self.page.evaluate('window.topicClicks'),['4294967297'])

    async def test_topic_waits_for_real_header_instead_of_fixed_sleep(self):
        await self.forum(delay=180)
        started=time.monotonic()
        self.assertTrue(await self.engine._enter_topic('讨论区'))
        elapsed=time.monotonic()-started
        self.assertGreaterEqual(elapsed,.16)
        self.assertLess(elapsed,1)

    async def test_same_topic_title_in_other_peer_is_never_clicked(self):
        await self.forum(peer='-999')
        self.assertFalse(await self.engine._enter_topic('讨论区',timeout=.12))
        self.assertEqual(await self.page.evaluate('window.topicClicks'),[])

    async def test_duplicate_topic_names_fail_without_clicking(self):
        await self.forum([{'id':'20','title':'讨论区'},{'id':'21','title':'讨论区'}])
        with self.assertRaisesRegex(RuntimeError,'同名话题'):
            await self.engine._enter_topic('讨论区')
        self.assertEqual(await self.page.evaluate('window.topicClicks'),[])

    async def test_saved_topic_id_selects_exact_duplicate_name(self):
        await self.forum([{'id':'20','title':'讨论区'},{'id':'21','title':'讨论区'}])
        await self.engine.open_chat('-101','Forum','讨论区',None,'21')
        self.assertEqual(await self.page.evaluate('window.topicClicks'),['21'])
        self.assertEqual(self.engine._expected_topic_id,'21')

    async def test_deleted_saved_id_never_falls_back_to_same_topic_name(self):
        await self.forum([{'id':'22','title':'讨论区'}])
        self.engine._configured_topic_id='21'
        with self.assertRaisesRegex(RuntimeError,'话题 ID 已不可用'):
            await self.engine._enter_topic('讨论区')
        self.assertEqual(await self.page.evaluate('window.topicClicks'),[])

    async def test_topic_listing_preserves_draft_and_never_enters_a_topic(self):
        await self.forum([{'id':'20','title':'讨论区'},{'id':'21','title':'讨论区'}])
        await self.page.locator('[contenteditable]').fill('existing draft')
        with patch.object(self.engine,'ensure_login',AsyncMock()):
            result=await self.engine.list_topics('-101')
        self.assertTrue(result['is_forum'])
        self.assertEqual([t['id'] for t in result['topics']],['20','21'])
        self.assertEqual(await self.page.locator('[contenteditable]').inner_text(),'existing draft')
        self.assertEqual(await self.page.evaluate('window.topicClicks'),[])

    async def test_stale_topic_cache_is_resolved_against_live_exact_row(self):
        await self.forum()
        self.engine._topic_ids={'-101':{'讨论区':'old-id'}}
        self.assertTrue(await self.engine._enter_topic('讨论区'))
        self.assertEqual(self.engine._topic_ids['-101']['讨论区'],'4294967297')

    async def test_same_title_with_wrong_thread_id_cannot_be_sent(self):
        await self.forum()
        self.engine._expected_topic='讨论区';self.engine._expected_topic_id='21'
        await self.page.locator('.chat-info .peer-title').evaluate("el=>{el.textContent='讨论区';el.dataset.threadId='22'}")
        with self.assertRaisesRegex(RuntimeError,'话题 ID 核对失败'):
            await self.engine.send_text('must not send')
        self.assertEqual(await self.page.locator('[contenteditable]').inner_text(),'')

    async def test_topic_title_without_thread_id_is_not_a_verified_destination(self):
        await self.page.locator('.chat-info .peer-title').evaluate("el=>el.textContent='讨论区'")
        self.assertFalse(await self.engine._enter_topic('讨论区',timeout=.12))

    async def test_verified_open_topic_does_not_click_or_wait_again(self):
        await self.forum()
        await self.engine.open_chat('-101','Forum','讨论区')
        started=time.monotonic()
        await self.engine.open_chat('-101','Forum','讨论区')
        self.assertLess(time.monotonic()-started,.5)
        self.assertEqual(await self.page.evaluate('window.topicClicks'),['4294967297'])

    async def test_conflicting_composer_and_header_topic_ids_are_rejected(self):
        await self.page.locator('.chat-info .peer-title').evaluate("el=>el.dataset.threadId='21'")
        await self.page.locator('[contenteditable]').evaluate("el=>el.dataset.threadId='22'")
        self.assertIsNone(await self.engine._opened_peer())

    async def test_topic_virtual_row_replacement_keeps_same_identity_and_one_click(self):
        await self.forum()
        await self.page.evaluate('''()=>{
            const row=document.querySelector('.topics-container a');
            row.animate([{transform:'translateX(0px)'},{transform:'translateX(30px)'}],{duration:200});
            setTimeout(()=>{const replacement=row.cloneNode(true);replacement.onmousedown=row.onmousedown;row.replaceWith(replacement)},20);
        }''')
        self.assertTrue(await self.engine._enter_topic('讨论区'))
        self.assertEqual(await self.page.evaluate('window.topicClicks'),['4294967297'])

    async def test_active_topic_in_visible_nested_slider_is_selectable(self):
        await self.forum()
        await self.page.evaluate('''()=>{
            const outer=document.createElement('div');outer.className='tabs-tab';
            const drawer=document.querySelector('.topics-slider');drawer.before(outer);outer.append(drawer);
            drawer.querySelector('.topics-container').classList.add('tabs-tab','active');
        }''')
        self.assertTrue(await self.engine._enter_topic('讨论区'))
        self.assertEqual(await self.page.evaluate('window.topicClicks'),['4294967297'])

    async def test_topic_drawer_disappearing_during_close_does_not_abort_navigation(self):
        await self.page.evaluate('''()=>{const p=document.createElement('div');p.className='topics-slider';p.innerHTML='<button class="sidebar-close-button">Back</button>';document.body.append(p)}''')
        original=ElementHandle.click
        async def click_then_detach(locator,*args,**kwargs):
            if 'sidebar-close-button' in (await locator.get_attribute('class') or ''):
                await locator.evaluate("el=>el.closest('.topics-slider').remove()")
                raise RuntimeError('Element was detached from the DOM')
            return await original(locator,*args,**kwargs)
        with patch.object(ElementHandle,'click',click_then_detach):
            await self.engine._close_topic_panel()
        self.assertEqual(await self.page.locator('.topics-slider').count(),0)

    async def test_offscreen_cached_drawer_does_not_block_closing_active_drawer(self):
        await self.page.evaluate('''()=>{
            for(const offscreen of [false,true]){
                const p=document.createElement('div');p.className='topics-slider';p.dataset.cached=String(offscreen);
                if(offscreen)p.style='position:absolute;left:-2000px;top:0';
                p.innerHTML='<button class="sidebar-close-button">Back</button>';
                p.querySelector('button').onclick=()=>p.remove();document.body.append(p);
            }
        }''')
        await self.engine._close_topic_panel()
        self.assertEqual(await self.page.locator('[data-cached="false"]').count(),0)
        self.assertEqual(await self.page.locator('[data-cached="true"]').count(),1)

    async def test_latest_button_loads_new_view_before_message_snapshot(self):
        await self.bubble('100','old message',age=100)
        await self.page.evaluate('''()=>{
            const button=document.createElement('button');button.className='bubbles-go-down';button.textContent='Latest';
            button.onclick=()=>setTimeout(()=>{
                document.querySelector('.bubbles').innerHTML='<div class="bubble is-in" data-mid="200" data-peer-id="-101"><div class="message">latest message</div></div>';
                document.querySelector('[data-mid="200"]').dataset.timestamp=Math.floor(Date.now()/1000);button.remove();
            },80);document.body.append(button);
        }''')
        boundary=await self.engine.snapshot_incoming()
        self.assertIn('200',boundary['seen'])
        self.assertNotIn('100',boundary['seen'])

    async def test_transparent_latest_button_does_not_block_ready_history(self):
        await self.bubble('100','existing message',age=100)
        await self.page.evaluate('''()=>{
            const b=document.createElement('button');b.className='bubbles-go-down';b.textContent='Latest';b.style.opacity='0';
            b.onclick=()=>{window.hiddenArrowClicked=true};document.body.append(b);
        }''')
        start=time.monotonic();boundary=await self.engine.snapshot_incoming()
        self.assertIn('100',boundary['seen'])
        self.assertFalse(await self.page.evaluate('!!window.hiddenArrowClicked'))
        self.assertLess(time.monotonic()-start,1.5)

    async def test_latest_arrow_retries_then_accepts_faded_button(self):
        await self.page.evaluate('''()=>{
            document.querySelector('.bubbles').innerHTML='<div class="bubbles-inner"></div>';
            const b=document.createElement('button');b.className='bubbles-go-down';b.textContent='Latest';window.arrowClicks=0;
            b.onclick=()=>{if(++window.arrowClicks===2){
                document.querySelector('.bubbles-inner').innerHTML='<div class="bubble is-in" data-mid="200" data-peer-id="-101"><div class="message">latest</div></div>';
                b.style.opacity='0';
            }};document.body.append(b);
        }''')
        boundary=await self.engine.snapshot_incoming()
        self.assertIn('200',boundary['seen'])
        self.assertEqual(await self.page.evaluate('window.arrowClicks'),2)

    async def test_feedback_clock_starts_at_send_instead_of_preparation(self):
        boundary=await self.engine.snapshot_incoming()
        prepared=boundary['cutoff'];await asyncio.sleep(.2)
        await self.page.locator('[contenteditable]').evaluate("el=>el.addEventListener('keydown',e=>{if(e.key==='Enter')window.sentEventAt=Date.now()/1000})")
        with patch.object(self.engine,'_composer_text',AsyncMock(return_value='')),patch.object(self.engine,'_track_sent_mid',AsyncMock()):
            await self.engine.send_text('签到')
        self.assertGreater(self.engine.send_started_at-prepared,.2)
        self.assertLessEqual(boundary['cutoff'],self.engine.send_started_at)
        self.assertGreaterEqual(self.engine.send_started_at+.01,await self.page.evaluate('window.sentEventAt'))
        self.assertEqual(boundary['sent_at'],self.engine.send_started_at)

    async def test_latest_arrow_uses_real_pointer_events(self):
        await self.bubble('100','existing message',age=100)
        await self.page.evaluate('''()=>{
            const b=document.createElement('button');b.className='bubbles-go-down';b.textContent='Latest';
            b.addEventListener('mousedown',e=>{window.trustedArrow=e.isTrusted;b.style.opacity='0'});document.body.append(b);
        }''')
        await self.engine.snapshot_incoming()
        self.assertTrue(await self.page.evaluate('window.trustedArrow'))

    async def test_latest_jump_recovers_empty_virtualized_viewport(self):
        await self.page.locator('.bubbles').evaluate('''el=>{
            el.innerHTML='<div class="bubbles-scrollable" style="height:100px;overflow:auto"><div style="height:900px"></div></div>';
            const viewport=el.querySelector('.bubbles-scrollable');
            const b=document.createElement('button');b.className='bubbles-go-down';b.textContent='Latest';
            b.onmousedown=()=>window.latestRequested=true;
            viewport.onscroll=()=>{if(window.latestRequested&&viewport.scrollTop>700){
                viewport.innerHTML='<div class="bubble is-in" data-mid="200" data-peer-id="-101"><div class="message">latest</div></div>';b.style.opacity='0';
            }};document.body.append(b);
        }''')
        boundary=await self.engine.snapshot_incoming()
        self.assertIn('200',boundary['seen'])

    async def test_latest_arrow_waits_for_initial_history_loading(self):
        await self.page.locator('.bubbles').evaluate('''el=>{
            el.innerHTML='<div class="bubbles-scrollable" style="height:100px;overflow:auto"></div>';
            const viewport=el.querySelector('.bubbles-scrollable');window.historyLoading=true;
            const b=document.createElement('button');b.className='bubbles-go-down';b.textContent='Latest';
            b.onmousedown=()=>{window.clickedBeforeLoad=window.historyLoading;b.style.opacity='0'};document.body.append(b);
            setTimeout(()=>{viewport.innerHTML='<div class="bubble is-in" data-mid="200" data-peer-id="-101"><div class="message">latest</div></div>';window.historyLoading=false},400);
        }''')
        boundary=await self.engine.snapshot_incoming()
        self.assertIn('200',boundary['seen'])
        self.assertFalse(await self.page.evaluate('window.clickedBeforeLoad'))

    async def test_feedback_arriving_during_send_ack_is_read_after_window_elapsed(self):
        boundary=await self.engine.snapshot_incoming()
        await self.bubble('502','签到成功',reply='501')
        start=time.monotonic()
        await self.engine.watch_new_incoming(boundary,.05,sent_mids=['501'],started_at=start-.5)
        self.assertEqual(self.engine.last_replied,['签到成功'])
        self.assertLess(time.monotonic()-start,.4)

    async def test_group_history_initial_render_finishes_before_baseline(self):
        await self.page.locator('.bubbles').evaluate('''el=>{
            el.innerHTML='<div class="bubbles-inner"></div>';
            setTimeout(()=>{const b=document.createElement('div');b.className='bubble is-in';b.dataset.mid='100';b.dataset.peerId='-101';b.dataset.timestamp=Math.floor(Date.now()/1000);b.textContent='existing message';el.querySelector('.bubbles-inner').append(b)},80);
        }''')
        boundary=await self.engine.snapshot_incoming()
        self.assertIn('100',boundary['seen'])

    async def test_explicit_empty_topic_can_establish_empty_baseline(self):
        await self.page.locator('.bubbles').evaluate("el=>el.innerHTML='<div class=\"bubbles-inner no-messages\"></div>'")
        boundary=await self.engine.snapshot_incoming()
        self.assertEqual(boundary['seen'],[])

    async def test_latest_button_is_used_when_history_has_not_rendered(self):
        await self.page.locator('.bubbles').evaluate('''el=>{
            el.innerHTML='<div class="bubbles-inner"></div>';
            const b=document.createElement('button');b.className='bubbles-go-down';b.textContent='Latest';
            b.onclick=()=>{setTimeout(()=>{
                el.querySelector('.bubbles-inner').innerHTML='<div class="bubble is-in" data-mid="200" data-peer-id="-101"><div class="message">latest</div></div>';
                el.querySelector('[data-mid="200"]').dataset.timestamp=Math.floor(Date.now()/1000);b.remove();
            },80)};document.body.append(b);
        }''')
        started=time.monotonic()
        boundary=await self.engine.snapshot_incoming()
        self.assertIn('200',boundary['seen'])
        self.assertLess(time.monotonic()-started,1.5)

    async def test_capture_boundary_remains_json_serializable_during_recheck(self):
        boundary=await self.engine.snapshot_incoming()
        json.dumps(boundary)
        self.engine.last_sent_mids={'501'}
        await self.bubble('502','签到成功',reply='501')
        await self.engine.watch_new_incoming(boundary,1.5,idle_after=.05)
        self.assertIn('502',boundary['seen'])
        json.dumps({'_last':{'actions':[{'boundary':boundary}]}})

    async def test_original_draft_is_cleared_for_action_and_restored_without_send(self):
        composer=self.page.locator('[contenteditable]')
        await composer.fill('原草稿\n第二行🙂')
        original=await composer.inner_text()
        async with self.engine.preserve_draft():
            self.assertEqual((await composer.inner_text()).strip(),'')
        self.assertEqual(await composer.inner_text(),original)
        self.assertFalse(list(self.engine.profile.glob('checkin-drafts/*.json')))
        self.assertIsNone(self.engine.last_draft_warning)

    async def test_draft_is_restored_when_action_raises(self):
        composer=self.page.locator('[contenteditable]');await composer.fill('原草稿')
        with self.assertRaisesRegex(RuntimeError,'send failed'):
            async with self.engine.preserve_draft():
                self.engine._automation_text='签到';await composer.fill('签到')
                raise RuntimeError('send failed')
        self.assertEqual(await composer.inner_text(),'原草稿')

    async def test_cancellation_waits_for_original_draft_restore(self):
        composer=self.page.locator('[contenteditable]');await composer.fill('取消也要恢复')
        ready=asyncio.Event()
        async def work():
            async with self.engine.preserve_draft():
                ready.set();await asyncio.sleep(10)
        task=asyncio.create_task(work());await ready.wait();task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        self.assertEqual(await composer.inner_text(),'取消也要恢复')

    async def test_new_user_draft_is_not_overwritten_and_backup_is_retained(self):
        composer=self.page.locator('[contenteditable]');await composer.fill('原草稿')
        async with self.engine.preserve_draft():await composer.fill('用户新输入')
        self.assertEqual(await composer.inner_text(),'用户新输入')
        backup=list(self.engine.profile.glob('checkin-drafts/*.json'))
        self.assertEqual(len(backup),1)
        self.assertEqual(json.loads(backup[0].read_text(encoding='utf8'))['text'],'原草稿')
        self.assertIn('恢复需核对',self.engine.last_draft_warning)

    async def test_rich_draft_uses_editor_paste_handler(self):
        composer=self.page.locator('[contenteditable]')
        await composer.evaluate('''el=>{el.innerHTML='<strong>重点</strong> 草稿';el.addEventListener('paste',event=>{event.preventDefault();el.innerHTML=event.clipboardData.getData('text/html')})}''')
        async with self.engine.preserve_draft():pass
        self.assertEqual(await composer.locator('strong').inner_text(),'重点')
        self.assertIsNone(self.engine.last_draft_warning)

    async def test_changed_destination_never_receives_original_draft(self):
        composer=self.page.locator('[contenteditable]');await composer.fill('原草稿')
        async with self.engine.preserve_draft():
            await composer.evaluate("el=>el.dataset.peerId='-999'")
            await self.page.locator('.chat-info .peer-title').evaluate("el=>el.dataset.peerId='-999'")
        self.assertEqual((await composer.inner_text()).strip(),'')
        self.assertTrue(list(self.engine.profile.glob('checkin-drafts/*.json')))
        self.assertIn('恢复需核对',self.engine.last_draft_warning)

    async def test_private_chat_temp_mid_confirmed_immediately_when_bot_deletes_and_replies(self):
        self.engine._expected_peer = '701'
        self.engine._mids_chat = '701'
        await self.page.set_content('''
          <div class="chat-info"><span class="peer-title" data-peer-id="701">Bot</span></div>
          <div class="bubbles"><div class="bubbles-scrollable"><div class="bubbles-inner">
            <div class="bubble is-in" data-mid="100" data-peer-id="701" data-timestamp="1700000000"><span class="message">old</span></div>
          </div></div></div>
          <div contenteditable="true" class="input-message-input" data-peer-id="701"></div>
          <script>
            document.querySelector('[contenteditable]').addEventListener('keydown', e => {
              if (e.key !== 'Enter') return;
              e.preventDefault();
              e.target.textContent = '';
              const inner = document.querySelector('.bubbles-inner');
              const now = Math.floor(Date.now() / 1000);
              const out = document.createElement('div');
              out.className = 'bubble is-out is-sending';
              out.dataset.mid = '100.0001'; out.dataset.peerId = '701'; out.dataset.timestamp = String(now);
              out.innerHTML = '<span class="message">/checkin</span>';
              inner.append(out);
              setTimeout(() => {
                out.remove();
                const rep = document.createElement('div');
                rep.className = 'bubble is-in';
                rep.dataset.mid = '200'; rep.dataset.peerId = '701'; rep.dataset.timestamp = String(now + 1);
                rep.innerHTML = '<span class="message">请选择按钮</span>';
                inner.append(rep);
              }, 120);
            });
          </script>
        ''')
        await self.engine.snapshot_incoming()
        started = time.monotonic()
        await self.engine.send_text('/checkin')
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 3.0)
        self.assertTrue(self.engine._last_send_confirmed_by_reply)
        self.assertIn('100.0001', self.engine.last_sent_mids)

    async def test_button_only_reports_older_matching_message_clearly(self):
        self.engine._expected_peer = '701'
        self.engine._mids_chat = '701'
        yesterday = int(time.time()) - 86400
        await self.page.set_content(f'''
          <div class="chat-info"><span class="peer-title" data-peer-id="701">Bot</span></div>
          <div class="bubbles"><div class="bubbles-scrollable"><div class="bubbles-inner">
            <div class="bubble is-in" data-mid="50" data-peer-id="701" data-timestamp="{yesterday}">
              <span class="message">昨日推送</span>
              <div class="reply-markup"><button class="reply-markup-button"><span class="reply-markup-button-text">一键签到</span></button></div>
            </div>
          </div></div></div>
        ''')
        with self.assertRaisesRegex(RuntimeError, '今天尚未收到包含按钮「一键签到」的新消息.*最近匹配消息时间'):
            await self.engine.click_existing_button('一键签到', None, time.time() - 60)

    async def test_collect_feedback_history_exits_quickly_on_empty_nonscrollable_chat(self):
        self.engine._expected_peer = '-101'
        self.engine._mids_chat = '-101'
        await self.page.set_content('''
          <div class="chat-info"><span class="peer-title" data-peer-id="-101">Topic</span></div>
          <div class="bubbles"><div class="bubbles-scrollable" style="height:200px;overflow:auto"><div class="bubbles-inner no-messages"></div></div></div>
        ''')
        started = time.monotonic()
        res = await self.engine.collect_feedback_history({'peer': '-101', 'cutoff': time.time() - 100, 'seen': []}, seconds=6)
        elapsed = time.monotonic() - started
        self.assertEqual(res['items'], [])
        self.assertLess(elapsed, 2.5)


if __name__=='__main__': unittest.main()

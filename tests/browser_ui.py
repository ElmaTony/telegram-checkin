"""Exercise production UI with anonymous, intercepted APIs. Never sends to Telegram."""
import asyncio
import os
import copy
import json
import sys
from urllib.parse import urlparse
from pathlib import Path
from playwright.async_api import async_playwright

ROOT=Path(__file__).resolve().parents[1]
BASE_URL=os.environ.get('CHECKIN_TEST_URL','http://127.0.0.1:8577')
OUT=ROOT/'audit'/'ui-check'
OUT.mkdir(parents=True,exist_ok=True)
sys.stdout.reconfigure(encoding='utf-8')
CFG={'group_delay_seconds':1,'account_delay_seconds':2,'reply_wait_seconds':6,'accounts':[
 {'name':'主账号','groups':[
  {'chat':'-101','title':'开发者社区','topic':'每日签到','folder':'签到','primary':True,'share_group':'社区积分','method':'text','message':'签到','reply_mode':'after','success_keywords':['签到成功，获得积分']},
  {'chat':'-102','title':'摄影交流','method':'command_button','message':['/checkin','/daily'],'click_button':['确认签到','领取奖励'],'reply_mode':'reply_only'},
  {'chat':'-103','title':'读书小组','actions':[{'method':'text','message':'签到'},{'method':'image_text','message':'打卡','image':'images/checkin.jpg'}]}]},
 {'name':'备用账号','groups':[{'chat':'-201','title':'开源讨论','method':'text','message':'签到'}]},
 {'name':'工作账号','groups':[{'chat':'-301','title':'英语角','method':'text','message':'签到'},{'chat':'-302','title':'设计灵感','method':'text','message':'签到'}]}]}


async def main():
 cfg=copy.deepcopy(CFG);posts=[];errors=[];revision='initial';busy=False
 status={'today':'2026-10-30','busy':False,'accounts':[], 'progress':{}, 'probe':{'running':False,'result':None},'job':None}
 for a in cfg['accounts']:
  groups=[]
  for g in a['groups']:
   confirm='failed' if g['chat']=='-101' else 'unconfirmed' if g['chat']=='-102' else 'success' if g['chat'] in ['-301','-302'] else None
   groups.append({'chat':g['chat'],'name':g['title'],'method':g.get('method','text'),'methods':[x['method'] for x in g.get('actions',[{'method':g.get('method','text')}])],'topic':g.get('topic',''),'action_count':len(g.get('actions',[{}])),
                  'share_group':g.get('share_group',''),'confirm':confirm,'done':confirm in ('success','unconfirmed'),
                  'sent':False if confirm=='failed' else True if confirm else None,'can_recheck':confirm=='unconfirmed',
                  'expected_peer':'-101' if confirm=='failed' else None,'actual_peer':'-999' if confirm=='failed' else None,
                  'source':'unknown' if confirm!='success' else 'reply_to_sent','error':'目标会话核对失败，未发送' if confirm=='failed' else '',
                  'replies':['签到成功，获得 10 积分'] if confirm=='success' else [],'detail':''})
   if confirm=='unconfirmed':
    groups[-1]['candidate_replies']=[{'mid':'candidate','t':'签到成功 +5，尚未关联当前发送'}]
    groups[-1]['feedback_hint']={'code':'rule_mismatch','message':'已捕获本轮关联反馈，但当前自定义判定词未命中。请检查该会话的成功／失败词。'}
  status['accounts'].append({'name':a['name'],'status':'上次已登录','total':len(groups),'done':sum(g['confirm']=='success' for g in groups),'groups':groups})

 async def intercept(route):
  nonlocal cfg,revision,busy
  req=route.request;path=urlparse(req.url).path
  if req.method!='GET':
   data=req.post_data_json;posts.append({'path':path,'body':copy.deepcopy(data),'headers':req.headers})
   if path=='/api/config':cfg=copy.deepcopy(data['cfg']);revision='saved-'+str(len(posts));return await route.fulfill(json={'ok':True,'revision':revision})
   if path in ('/api/checkin_now','/api/probe','/api/recheck'):
    busy=True;status.update(busy=True,job={'id':'job','mode':{'/api/checkin_now':'checkin','/api/probe':'probe','/api/recheck':'recheck'}[path],'status':'running'});return await route.fulfill(status=202,json={'ok':True,'job_id':'job'})
   if path=='/api/cancel':busy=False;status.update(busy=False,job={'id':'job','mode':'checkin','status':'cancelled'});return await route.fulfill(json={'ok':True})
   if path=='/api/judge':return await route.fulfill(json={'ok':True,'confirm':'success','hits':{'success_keywords':['签到成功'],'fail_keywords':[]}})
   return await route.fulfill(json={'ok':True})
  if path=='/api/config':return await route.fulfill(json={**cfg,'_revision':revision})
  if path=='/api/status':return await route.fulfill(json=status)
  if path=='/api/dialogs':return await route.fulfill(json={'ok':True,'dialogs':[{'id':'-401','title':"O'Reilly <group>",'type':'群组','is_forum':False},{'id':'-402','title':'新社区','type':'群组','is_forum':False}],'folders':['签到'],'cached':True,'updated':'2026-10-30 20:00'})
  if path=='/api/topics':return await route.fulfill(json={'ok':True,'is_forum':False,'topics':[]})
  if path=='/api/history':return await route.fulfill(json={'ok':True,'rows':[{'time':'2026-10-30 09:32:00','account':'主账号','group':'开发者社区','status':'失败','detail':'目标核对失败'}],'total':1,'page':1,'limit':100})
  if path=='/api/stats':return await route.fulfill(json={'ok':True,'total':{'成功':2,'未确认':1,'失败':1},'per_day':[{'date':'2026-10-30','成功':2,'未确认':1,'失败':1}],'per_acc':[{'account':'主账号','成功':2,'未确认':1,'失败':1}]})
  if path=='/api/logs':return await route.fulfill(json={'ok':True,'text':'2026-10-30 [INFO] 本地测试日志\n'})
  return await route.fulfill(body='fixture data',content_type='text/plain')

 async def confirm_scoped_job(page,account,chats):
  assert await page.get_by_role('checkbox',name='显示签到过程',exact=False).is_checked()
  await page.get_by_role('button',name='确认发送',exact=True).click()
  await page.get_by_role('button',name='取消任务',exact=True).first.wait_for()
  submitted=[p for p in posts if p['path']=='/api/checkin_now'][-1]['body']
  assert submitted=={'account':account,'chats':chats,'show_browser':True},submitted
  assert await page.locator('[data-action="run-account"],[data-action="run-chat"],[data-action="run-account-filter"]').evaluate_all('(els)=>els.every(el=>el.disabled)')
  await page.get_by_role('button',name='取消任务',exact=True).first.click()
  await page.wait_for_function("document.querySelector('#job-banner').hidden")

 async with async_playwright() as pw:
  browser=await pw.chromium.launch(**({'channel':'msedge'} if sys.platform=='win32' else {}),headless=True)
  page=await browser.new_page(viewport={'width':1440,'height':1050})
  page.on('pageerror',lambda e:errors.append(str(e)))
  await page.route('**/api/**',intercept)
  await page.goto(BASE_URL)
  await page.get_by_role('heading',name='今天的签到').wait_for()
  await page.screenshot(path=str(OUT/'overview-desktop.png'),full_page=True)
  assert await page.get_by_text('0 次 · 未发送',exact=True).count()==1
  status['progress']={'主账号':{'chat':'-101','stage':'阶段一'}}
  status.update(busy=True,activity='阶段一',job={'id':'refresh-job','mode':'checkin','status':'running'})
  await page.locator('#job-banner .job-head').get_by_text('阶段一',exact=False).wait_for(timeout=7000)
  await page.locator('.inspector details').evaluate("el=>{el.open=true;window.savedInspector=el.closest('.inspector')}")
  status['activity']='阶段二';status['progress']['主账号']['stage']='阶段二'
  await page.locator('#job-banner .job-head').get_by_text('阶段二',exact=False).wait_for(timeout=4000)
  assert await page.evaluate("document.querySelector('.inspector')===window.savedInspector")
  assert '阶段二' in await page.locator('.task-table .is-running .row-stage').inner_text()
  assert await page.locator('.inspector details').evaluate('(el)=>el.open')
  status['accounts'][0]['groups'][0]['time']='12:34:56';status['activity']='阶段三'
  await page.locator('.task-table').get_by_text('12:34:56',exact=True).wait_for(timeout=4000)
  assert await page.locator('.inspector details').evaluate('(el)=>el.open')
  status.update(busy=False,activity='',job=None)
  await page.wait_for_function("document.querySelector('#job-banner').hidden")
  await page.get_by_role('button',name='需要处理',exact=True).click()
  assert await page.locator('.task-table tbody tr').count()==2
  await page.get_by_role('button',name='全部会话',exact=True).click()
  await page.get_by_role('textbox',name='搜索会话或账号').fill('摄影')
  assert await page.locator('.task-table tbody tr').count()==1
  assert '候选签到反馈' in await page.locator('.candidate-feedback').inner_text()
  assert '自定义判定词未命中' in await page.locator('.feedback-hint').inner_text()
  before_hint=len(posts)
  await page.get_by_role('button',name='检查判定词',exact=True).click()
  await page.get_by_role('textbox',name='本群成功词',exact=False).wait_for()
  assert not posts[before_hint:]
  await page.get_by_role('button',name='取消',exact=True).click()
  await page.get_by_role('textbox',name='账号名称',exact=True).fill('示例草稿账号')
  await page.get_by_role('button',name='签到工作台',exact=True).click()
  await page.get_by_role('button',name='检查判定词',exact=True).click()
  assert await page.locator('[data-account-name="0"]').input_value()=='示例草稿账号'
  await page.get_by_role('button',name='取消',exact=True).click()
  await page.get_by_role('textbox',name='账号名称',exact=True).fill('主账号')
  await page.get_by_role('button',name='签到工作台',exact=True).click()
  await page.get_by_role('button',name='仅补查反馈：主账号 / 摄影交流',exact=True).click()
  assert [p for p in posts if p['path']=='/api/recheck'][-1]['body']=={'account':'主账号','chats':['-102']}
  await page.get_by_role('button',name='取消任务',exact=True).first.click()
  await page.wait_for_function("document.querySelector('#job-banner').hidden")
  await page.get_by_role('textbox',name='搜索会话或账号').fill('')
  await page.get_by_role('combobox',name='筛选账号',exact=True).select_option('备用账号')
  assert await page.locator('.inspector h2').inner_text()=='开源讨论'
  await page.get_by_role('button',name='只检查此群',exact=True).click()
  assert [p for p in posts if p['path']=='/api/probe'][-1]['body']=={'account':'备用账号','chats':['-201']}
  await page.get_by_role('button',name='取消任务',exact=True).first.click()
  await page.wait_for_function("document.querySelector('#job-banner').hidden")
  await page.get_by_role('textbox',name='搜索会话或账号').fill('不存在的会话')
  assert await page.locator('.inspector').count()==0
  await page.get_by_role('textbox',name='搜索会话或账号').fill('')
  await page.get_by_role('combobox',name='筛选账号',exact=True).select_option('')
  await page.get_by_role('button',name='账号与规则',exact=True).click()
  await page.locator('.global-settings summary').click()
  assert await page.get_by_role('spinbutton',name='账号间隔最小值').input_value()=='2'
  assert await page.get_by_role('spinbutton',name='账号间隔最大值').input_value()=='2'
  await page.get_by_role('spinbutton',name='反馈窗口上限（秒）').fill('23')
  await page.locator('[data-action="edit-group"][data-index="0"]').click()
  await page.get_by_role('textbox',name='论坛话题',exact=True).fill('每日签到新话题')
  await page.get_by_role('button',name='保存到草稿',exact=True).click()
  assert await page.get_by_role('spinbutton',name='反馈窗口上限（秒）').input_value()=='23'
  await page.get_by_role('button',name='保存所有配置',exact=True).click()
  await page.wait_for_timeout(100)
  saved=[p for p in posts if p['path']=='/api/config'][-1]['body']['cfg']
  g=saved['accounts'][0]['groups'][0]
  assert g['topic']=='每日签到新话题' and g['folder']=='签到' and g['primary'] and g['reply_mode']=='after'
  assert saved['reply_wait_seconds']==23
  await page.locator('[data-action="edit-group"][data-index="1"]').click()
  assert await page.get_by_role('textbox',name='按钮文字 · 一行一级').input_value()=='确认签到\n领取奖励'
  await page.get_by_role('button',name='保存到草稿',exact=True).click()
  await page.get_by_role('button',name='保存所有配置',exact=True).click()
  await page.wait_for_timeout(100)
  assert cfg['accounts'][0]['groups'][1]['click_button']==['确认签到','领取奖励']
  assert cfg['accounts'][0]['groups'][1]['message']==['/checkin','/daily']
  await page.get_by_role('button',name='添加群',exact=True).click()
  await page.get_by_role('button',name='从会话列表选择',exact=True).click()
  await page.get_by_role('button',name='选用',exact=True).first.click()
  await page.get_by_role('button',name='保存到草稿',exact=True).click()
  await page.get_by_role('button',name='保存所有配置',exact=True).click()
  await page.wait_for_timeout(100)
  assert cfg['accounts'][0]['groups'][-1]['title']=="O'Reilly <group>"
  await page.get_by_role('spinbutton',name='反馈窗口上限（秒）').fill('77')
  await page.locator('.brand').click()
  await page.get_by_role('heading',name='今天的签到').wait_for()
  await page.get_by_role('button',name='账号与规则',exact=True).click()
  assert await page.get_by_role('spinbutton',name='反馈窗口上限（秒）').input_value()=='77'
  await page.get_by_role('button',name='重新载入',exact=True).click()
  await page.get_by_role('button',name='放弃草稿并载入',exact=True).click()
  await page.wait_for_function("document.querySelector('[data-global=reply_wait_seconds]')?.value==='23'")
  assert await page.get_by_role('spinbutton',name='反馈窗口上限（秒）').input_value()=='23'
  await page.evaluate('window.scrollTo(0,0);document.querySelector("#notifications").replaceChildren()')
  await page.screenshot(path=str(OUT/'accounts-desktop.png'),full_page=True)
  await page.get_by_role('button',name='账号签到：主账号',exact=True).click()
  assert await page.locator('.plan-list').inner_text()=='主账号 · 2 个目标\n开发者社区、读书小组'
  await confirm_scoped_job(page,'主账号',['-101','-103'])
  await page.get_by_role('button',name='单群签到：主账号 / 开发者社区',exact=True).click()
  await confirm_scoped_job(page,'主账号',['-101'])
  await page.get_by_role('button',name='签到工作台',exact=True).click()
  await page.get_by_role('combobox',name='筛选账号',exact=True).select_option('备用账号')
  await page.get_by_role('button',name='签到此账号',exact=True).click()
  await confirm_scoped_job(page,'备用账号',['-201'])
  await page.get_by_role('combobox',name='筛选账号',exact=True).select_option('')
  await page.get_by_role('button',name='单群重签：工作账号 / 英语角',exact=True).click()
  assert await page.locator('.plan-list').inner_text()=='工作账号 · 1 个目标\n英语角'
  await confirm_scoped_job(page,'工作账号',['-301'])
  await page.get_by_role('combobox',name='筛选账号',exact=True).select_option('工作账号')
  before=len([p for p in posts if p['path']=='/api/checkin_now'])
  await page.get_by_role('button',name='签到此账号',exact=True).click()
  assert not await page.locator('#modal').is_visible()
  assert len([p for p in posts if p['path']=='/api/checkin_now'])==before
  await page.get_by_role('combobox',name='筛选账号',exact=True).select_option('')
  await page.get_by_role('button',name='全部账号待签到',exact=True).click()
  assert await page.get_by_role('checkbox',name='显示签到过程',exact=False).is_checked()
  await page.get_by_role('button',name='确认发送',exact=True).click()
  assert [p for p in posts if p['path']=='/api/checkin_now'][-1]['body']['show_browser'] is True
  await page.get_by_role('button',name='取消任务',exact=True).first.wait_for()
  await page.get_by_role('button',name='取消任务',exact=True).first.click()
  await page.get_by_role('button',name='执行记录',exact=True).click()
  await page.get_by_role('textbox',name='关键词',exact=True).fill('Target')
  await page.get_by_role('button',name='筛选',exact=True).click()
  await page.get_by_role('button',name='统计',exact=True).click()
  await page.get_by_text('共 4 次记录',exact=False).wait_for()
  await page.get_by_role('button',name='运行日志',exact=True).click()
  await page.get_by_text('本地测试日志',exact=False).wait_for()
  await page.get_by_role('button',name='签到工作台',exact=True).click()
  await page.evaluate('document.querySelector("#notifications").replaceChildren()')
  for width in [1024,736,390,320]:
   await page.set_viewport_size({'width':width,'height':1000})
   await page.wait_for_timeout(80)
   bounds=await page.evaluate('''()=>({width:innerWidth,scroll:document.documentElement.scrollWidth,buttons:[...document.querySelectorAll('main button,.sidebar button')].filter(el=>el.getBoundingClientRect().width && el.getBoundingClientRect().right>innerWidth+1).length})''')
   assert bounds['scroll']<=width+1 and bounds['buttons']==0,bounds
  await page.screenshot(path=str(OUT/'overview-mobile.png'),full_page=True)
  await page.set_viewport_size({'width':390,'height':900})
  await page.get_by_role('button',name='开源讨论',exact=True).click()
  await page.wait_for_function("document.querySelector('.inspector').getBoundingClientRect().top<innerHeight && document.querySelector('.inspector').getBoundingClientRect().top>=-2")
  await page.get_by_role('button',name='返回会话列表',exact=True).click()
  await page.wait_for_function("document.querySelector('.task-table tr.selected').getBoundingClientRect().top<innerHeight && document.querySelector('.task-table tr.selected').getBoundingClientRect().bottom>0")
  await page.set_viewport_size({'width':1440,'height':1050})
  await page.get_by_role('combobox',name='外观').select_option('dark')
  await page.screenshot(path=str(OUT/'overview-dark.png'),full_page=True)
  await page.get_by_role('combobox',name='外观').select_option('light')
  await page.reload();await page.get_by_role('heading',name='今天的签到').wait_for()
  assert await page.evaluate('document.documentElement.dataset.theme')=='light'
  status.update(busy=False,job={'id':'completed-job','mode':'checkin','status':'completed','retry_map':{'主账号':['-101','-102']}})
  await page.get_by_role('button',name='查看待处理',exact=True).wait_for(timeout=7000)
  assert '2 个会话需要核对' in await page.locator('#job-banner').inner_text()
  await page.get_by_role('button',name='查看待处理',exact=True).click()
  assert await page.locator('.task-table tbody tr').count()==2
  await browser.close()
 assert not errors,errors
 result={'passed':True,'js_errors':errors,'tested':['filters','search','filtered_inspector','progress_without_repaint','live_row_stage','single_chat_probe','row_feedback_recheck','evidence_expansion_preserved','completion_summary','scalar_delay','draft_preservation','group_field_roundtrip','multilevel_buttons','random_messages','quoted_chat_title','account_card_checkin','filtered_account_checkin','config_single_chat','successful_chat_recheck_send','processed_account_no_send','busy_execution_buttons','run_preview','cancel','history_filter','stats','logs','responsive_1024_736_390_320','theme_persistence','mobile_inspector_return'],'writes_intercepted':len(posts)}
 (OUT/'results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
 print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':asyncio.run(main())

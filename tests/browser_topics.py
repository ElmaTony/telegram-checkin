"""Forum selection and draft round-trip with anonymous, intercepted APIs only."""
import asyncio
import os
import copy
import json
import sys
from pathlib import Path
from urllib.parse import urlparse,parse_qs
from playwright.async_api import async_playwright

ROOT=Path(__file__).resolve().parents[1]
BASE_URL=os.environ.get('CHECKIN_TEST_URL','http://127.0.0.1:8577')
OUT=ROOT/'audit/ui-topics';OUT.mkdir(parents=True,exist_ok=True)

async def main():
 cfg={'accounts':[{'name':'示例账号','groups':[{'chat':'-101','title':'原有会话','method':'text','message':'签到'}]}]}
 status={'today':'2026-11-06','busy':False,'job':None,'progress':{},'probe':{},'accounts':[{'name':'示例账号','status':'上次已登录','done':0,'total':1,'groups':[{'chat':'-101','name':'原有会话','method':'text','confirm':None}]}]}
 posts=[];reads=[];errors=[];topic_error=False;dialog_error=True
 async def intercept(route):
  nonlocal cfg
  request=route.request;url=urlparse(request.url);query=parse_qs(url.query)
  if request.method!='GET':
   assert url.path=='/api/config',url.path
   cfg=copy.deepcopy(request.post_data_json['cfg']);posts.append(cfg)
   return await route.fulfill(json={'ok':True,'revision':'saved'})
  if url.path=='/api/config':return await route.fulfill(json={**cfg,'_revision':'fixture'})
  if url.path=='/api/status':return await route.fulfill(json=status)
  if url.path=='/api/dialogs' and dialog_error:return await route.fulfill(status=409,json={'ok':False,'error':'Telegram 会话列表尚未加载完成，请刷新重试。'})
  if url.path=='/api/dialogs':return await route.fulfill(json={'ok':True,'dialogs':[
   {'id':'-201','title':'会员俱乐部','type':'论坛群 · 含话题','is_forum':True},
   {'id':'-202','title':'普通讨论群','type':'群组/频道','is_forum':False},
   {'id':'601','title':'签到助手','type':'私聊/机器人','is_forum':False}], 'folders':[],'cached':True})
  if url.path=='/api/topics':
   reads.append(query)
   if topic_error:return await route.fulfill(status=409,json={'ok':False,'error':'话题读取暂时失败'})
   return await route.fulfill(json={'ok':True,'is_forum':query['chat']==['-201'],'topics':[{'id':'21','title':'讨论区'},{'id':'22','title':'讨论区'},{'id':'23','title':'公告'}] if query['chat']==['-201'] else []})
  raise AssertionError(url.path)
 async with async_playwright() as p:
  browser=await p.chromium.launch(**({'channel':'msedge'} if sys.platform=='win32' else {}),headless=True)
  page=await browser.new_page(viewport={'width':1440,'height':1050})
  page.on('pageerror',lambda e:errors.append(str(e)))
  await page.route('**/api/**',intercept)
  await page.goto(BASE_URL+'/#accounts')
  await page.get_by_role('button',name='添加群',exact=True).click()
  close=page.locator('#modal').get_by_role('button',name='关闭',exact=True)
  assert await close.locator('svg').count()==1
  await close.click()
  assert not await page.locator('#modal').is_visible()
  await page.get_by_role('button',name='添加群',exact=True).click()
  await page.get_by_role('button',name='从会话列表选择',exact=True).click()
  await page.get_by_text('会话列表读取失败',exact=True).wait_for()
  assert await page.locator('#chooser [role=alert]').count()==1
  dialog_error=False
  await page.get_by_role('button',name='刷新列表',exact=True).click()
  await page.get_by_role('checkbox',name='勾选 会员俱乐部',exact=True).wait_for()
  assert await page.get_by_role('checkbox',name='勾选 会员俱乐部',exact=True).is_disabled()
  await page.get_by_role('button',name='选择话题',exact=True).click()
  await page.get_by_role('combobox',name='签到发送话题',exact=True).wait_for()
  assert reads[-1]['chat']==['-201']
  assert await page.get_by_role('combobox',name='签到发送话题',exact=True).input_value()==''
  await page.get_by_role('button',name='保存到草稿',exact=True).click()
  assert '请先选择签到发送话题' in await page.locator('#editor-error').inner_text()
  await page.get_by_role('combobox',name='签到发送话题',exact=True).select_option('22')
  assert await page.evaluate("document.activeElement.id==='editor-topic'")
  await page.locator('#modal').evaluate('el=>el.scrollTop=0')
  await page.screenshot(path=str(OUT/'forum-selection.png'))
  for width in [390,320]:
   await page.set_viewport_size({'width':width,'height':950})
   assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
   assert await page.locator('#modal').evaluate('e=>e.scrollWidth<=e.clientWidth+1')
  await page.screenshot(path=str(OUT/'forum-mobile.png'))
  await page.set_viewport_size({'width':1440,'height':1050})
  await page.get_by_role('button',name='保存到草稿',exact=True).click()
  await page.get_by_role('button',name='保存配置',exact=True).click()
  await page.wait_for_function("!document.querySelector('#draft-indicator') || document.querySelector('#draft-indicator').hidden")
  group=cfg['accounts'][0]['groups'][1]
  assert group['chat']=='-201' and group['is_forum'] and group['topic']=='讨论区' and group['topic_id']=='22',group
  await page.get_by_role('button',name='编辑',exact=True).nth(1).click()
  topic_error=True
  await page.get_by_role('button',name='识别 / 刷新话题',exact=True).click()
  await page.get_by_text('识别未完成：话题读取暂时失败。请重试后选择发送位置。',exact=True).wait_for()
  await page.get_by_role('button',name='保存到草稿',exact=True).click()
  assert '请先重试会话识别' in await page.locator('#editor-error').inner_text()
  topic_error=False
  await page.get_by_role('button',name='从会话列表选择',exact=True).click()
  await page.locator('.chooser-row').filter(has_text='普通讨论群').get_by_role('button',name='选用',exact=True).click()
  await page.get_by_text('已识别为普通会话 · 无话题',exact=True).wait_for()
  await page.get_by_role('button',name='保存到草稿',exact=True).click()
  await page.get_by_role('button',name='保存配置',exact=True).click()
  await page.wait_for_function("document.querySelector('#draft-indicator').hidden")
  group=cfg['accounts'][0]['groups'][1]
  assert group['chat']=='-202' and not group['is_forum'] and 'topic' not in group and 'topic_id' not in group,group
  await page.get_by_role('button',name='添加群',exact=True).click()
  editor=page.locator('#modal')
  before=len(reads)
  await editor.locator('[data-editor="chat"]').fill('704')
  await editor.locator('[data-editor="chat"]').press('Tab')
  await editor.get_by_text('机器人或私聊 · 无话题',exact=True).wait_for()
  assert await editor.get_by_role('button',name='识别 / 刷新话题',exact=True).count()==0
  assert len(reads)==before
  await editor.get_by_role('combobox',name='动作 1 签到方式').select_option('button_only')
  assert await editor.locator('[data-action-field="message"]').count()==0
  await editor.get_by_role('textbox',name='按钮完整文字',exact=True).fill('✅ 点我签到')
  await editor.get_by_role('button',name='保存到草稿',exact=True).click()
  await page.get_by_role('button',name='保存配置',exact=True).click()
  await page.wait_for_function("document.querySelector('#draft-indicator').hidden")
  group=cfg['accounts'][0]['groups'][-1]
  assert group['method']=='button_only' and group['click_button']=='✅ 点我签到' and 'message' not in group,group
  await browser.close()
 assert not errors,errors
 result={'passed':True,'api_writes_intercepted':len(posts),'topic_reads_intercepted':len(reads),'js_errors':errors,'checks':['forum_auto_detection','required_topic','exact_duplicate_title_id','topic_round_trip','ordinary_chat_clears_stale_topic','failed_scan_blocks_save','private_chat_has_no_topics','button_only_rule','mobile_dialog']}
 (OUT/'results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8')
 print(json.dumps(result))

if __name__=='__main__':asyncio.run(main())

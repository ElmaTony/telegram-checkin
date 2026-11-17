"""Exercise the production automation UI using intercepted APIs; no real notifications."""
import asyncio
import os
import copy
import json
import sys
from pathlib import Path
from urllib.parse import urlparse
from playwright.async_api import async_playwright

ROOT=Path(__file__).resolve().parents[1]
BASE_URL=os.environ.get('CHECKIN_TEST_URL','http://127.0.0.1:8577')
OUT=ROOT/'audit'/'ui-automation'
OUT.mkdir(parents=True,exist_ok=True)
KEY='SCT'+'x'*24


async def main():
 cfg={'accounts':[{'name':'A','groups':[{'chat':'-101','title':'示例社区','message':'签到'}]}, {'name':'B','groups':[{'chat':'-102','title':'示例论坛','message':'签到'}]}]}
 settings={'enabled':False,'time':'09:00','timezone':'Asia/Hong_Kong','headless':True,'catch_up':True,'accounts':[]}
 data={'ok':True,'settings':settings,'next_run':None,'last_run':{},'error':'','active':True,'service_error':'','notifications':{'enabled':False,'only_on_issues':False},'key':{'configured':False,'source':'','error':''},'notification':{},'revision':'r1'}
 status={'today':'2026-11-04','busy':False,'progress':{},'job':None,'probe':{},'accounts':[{'name':a['name'],'status':'上次已登录','done':0,'total':1,'groups':[{'chat':a['groups'][0]['chat'],'name':a['groups'][0]['title'],'method':'text','action_count':1,'confirm':None}]} for a in cfg['accounts']]}
 posts=[];errors=[]
 async def intercept(route):
  nonlocal data
  req=route.request;path=urlparse(req.url).path
  if req.method=='GET':
   if path=='/api/status':return await route.fulfill(json={**status,'automation':{k:data[k] for k in ('settings','next_run','last_run','error','active','service_error')},'notification':data['notification']})
   if path=='/api/config':return await route.fulfill(json={**cfg,'_revision':data['revision']})
   if path=='/api/automation':return await route.fulfill(json=data)
  else:
   body=req.post_data_json;posts.append({'path':path,'body':copy.deepcopy(body)})
   if path=='/api/automation':
    assert body['revision']==data['revision']
    data={**data,'settings':body['schedule'],'notifications':body['notifications'],'revision':data['revision']+'x','next_run':'2026-11-05T00:10:00+08:00'}
    cfg.update(schedule=copy.deepcopy(body['schedule']),notifications=copy.deepcopy(body['notifications']))
    if body.get('sendkey'):data['key']={'configured':True,'source':'local','error':''}
    return await route.fulfill(json=data)
   if path=='/api/notifications/test':
    data['notification']={'time':'2026-11-04T10:00:00+08:00','ok':True,'message':'Server酱已接受，微信送达请以接收端为准'}
    return await route.fulfill(json=data['notification'])
  raise AssertionError('Unexpected API: '+path)
 async with async_playwright() as p:
  browser=await p.chromium.launch(**({'channel':'msedge'} if sys.platform=='win32' else {}),headless=True)
  page=await browser.new_page(viewport={'width':1440,'height':1000})
  page.on('pageerror',lambda error:errors.append(str(error)))
  await page.route('**/api/**',intercept)
  await page.goto(BASE_URL+'/#automation')
  await page.get_by_role('heading',name='定时与微信通知',exact=True).wait_for()
  assert await page.locator('#serverchan-key').input_value()==''
  await page.locator('[data-schedule="enabled"]').check()
  await page.get_by_label('每天执行时间',exact=True).fill('00:10')
  await page.get_by_role('radio',name='指定账号',exact=True).check()
  await page.get_by_role('checkbox',name='A',exact=True).uncheck()
  await page.get_by_role('checkbox',name='B',exact=True).uncheck()
  await page.get_by_role('button',name='保存设置',exact=True).click()
  await page.get_by_text('请至少勾选一个定时账号，或明确选择全部账号',exact=True).wait_for()
  assert not posts
  await page.get_by_role('checkbox',name='B',exact=True).check()
  await page.locator('[data-notification="enabled"]').check()
  await page.get_by_label('SendKey',exact=True).fill(KEY)
  assert await page.get_by_role('button',name='发送测试通知',exact=True).is_disabled()
  await page.get_by_role('button',name='保存设置',exact=True).click()
  await page.get_by_text('密钥已配置',exact=True).wait_for()
  assert await page.locator('#serverchan-key').input_value()==''
  assert data['settings']['time']=='00:10' and data['settings']['accounts']==['B']
  await page.get_by_label('运行时区',exact=True).fill('Asia/Tokyo')
  await page.wait_for_timeout(4500)
  assert await page.get_by_label('运行时区',exact=True).input_value()=='Asia/Tokyo'
  await page.get_by_role('button',name='账号与规则',exact=True).click()
  await page.locator('[data-account-name="0"]').fill('A draft')
  await page.get_by_role('button',name='定时与通知',exact=True).click()
  assert await page.get_by_label('运行时区',exact=True).input_value()=='Asia/Tokyo'
  await page.get_by_role('button',name='保存设置',exact=True).click()
  await page.get_by_role('button',name='发送测试通知',exact=True).wait_for(state='visible')
  await page.get_by_role('button',name='发送测试通知',exact=True).click()
  await page.wait_for_function("document.querySelector('.notification-summary').textContent.includes('已接受')")
  await page.get_by_role('button',name='账号与规则',exact=True).click()
  assert await page.locator('[data-account-name="0"]').input_value()=='A draft'
  await page.get_by_role('button',name='定时与通知',exact=True).click()
  assert not any(row['path'] in ('/api/checkin_now','/api/probe','/api/recheck') for row in posts)
  await page.evaluate('document.querySelector("#notifications").replaceChildren()')
  await page.screenshot(path=str(OUT/'automation-desktop.png'))
  for width in (1024,390,320):
   await page.set_viewport_size({'width':width,'height':950})
   assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
  await page.screenshot(path=str(OUT/'automation-mobile.png'),full_page=True)
  await page.set_viewport_size({'width':1440,'height':1000})
  await page.get_by_role('combobox',name='外观',exact=True).select_option('dark')
  await page.screenshot(path=str(OUT/'automation-dark.png'))
  await browser.close()
 assert not errors,errors
 result={'passed':True,'js_errors':errors,'writes':len(posts),'telegram_calls':0,'real_notifications':0,'checks':['settings_scope','private_key_not_echoed','poll_preserves_input','account_draft_preserved','test_notification','responsive','dark_theme']}
 (OUT/'results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
 print(json.dumps(result))


if __name__=='__main__':asyncio.run(main())

"""Catalog filters, multi-action metadata and responsive navigation; no API writes."""
import asyncio
import os
import json
import sys
from pathlib import Path
from urllib.parse import urlparse
from playwright.async_api import async_playwright

ROOT=Path(__file__).resolve().parents[1]
BASE_URL=os.environ.get('CHECKIN_TEST_URL','http://127.0.0.1:8577')
OUT=ROOT/'audit'/'ui-catalog'
OUT.mkdir(parents=True,exist_ok=True)


async def main():
 cfg={'accounts':[
  {'name':'主账号','groups':[
   {'chat':'-101','title':'文字群','method':'text','message':'签到'},
   {'chat':'-102','title':'论坛群','topic':'讨论区','method':'text','message':'签到'},
   {'chat':'601','title':'按钮助手','method':'command_button','message':'/checkin','click_button':'确认'},
   {'chat':'-103','title':'图片群','method':'image_only','image':'images/example.png'},
   {'chat':'602','title':'多步图文助手','share_group':'系统甲','actions':[{'method':'text','message':'签到'},{'method':'image_text','image':'images/example.png','message':'打卡'}]}]},
  {'name':'备用账号','groups':[
   {'chat':'-201','title':'口令群','share_group':'系统乙','method':'command','message':'/checkin'},
   {'chat':'@demo_contact','title':'用户名会话示例','method':'command','message':'/checkin'},
   {'chat':'-202','title':'共享群','share_group':'系统乙','method':'text','message':'签到'}]}]}
 status={'today':'2026-11-05','busy':False,'job':None,'progress':{},'probe':{},'accounts':[]}
 for account in cfg['accounts']:
  groups=[]
  for group in account['groups']:
   chat=group['chat'];confirm='failed' if chat=='-103' else 'unconfirmed' if chat=='-102' else 'success' if chat in ('-101','601','-201','-202') else None
   groups.append({'chat':chat,'name':group['title'],'topic':group.get('topic',''),'method':group.get('method','text'),'action_count':len(group.get('actions',[{}])),'share_group':group.get('share_group',''),'confirm':confirm,'done':confirm=='success','sent':False if confirm=='failed' else True if confirm else None,'source':'shared' if chat=='-202' else 'reply_to_sent' if confirm=='success' else 'unknown','can_recheck':confirm=='unconfirmed'})
  status['accounts'].append({'name':account['name'],'status':'上次已登录','done':sum(g['confirm']=='success' for g in groups),'total':len(groups),'groups':groups})
 errors=[];writes=[]
 async def intercept(route):
  request=route.request
  if request.method!='GET':writes.append(request.url);return await route.abort()
  path=urlparse(request.url).path
  data={'/api/config':{**cfg,'_revision':'fixture'},'/api/status':status}.get(path)
  if data is None:raise AssertionError(path)
  await route.fulfill(json=data)
 async with async_playwright() as p:
  browser=await p.chromium.launch(**({} if Path(p.chromium.executable_path).is_file() else ({'channel':'msedge'} if sys.platform=='win32' else {})),headless=True)
  page=await browser.new_page(viewport={'width':1440,'height':1080})
  page.on('pageerror',lambda error:errors.append(str(error)))
  await page.route('**/api/**',intercept)
  await page.goto(BASE_URL)
  await page.get_by_role('heading',name='今天的签到',exact=True).wait_for()
  async def rows():return await page.locator('.task-table tbody tr').count()
  assert await rows()==8
  assert await page.locator('#catalog-panel').is_hidden()
  assert (await page.locator('.task-surface').bounding_box())['y']<500
  for query,expected in [('讨论区','论坛群'),('系统甲','多步图文助手'),('图片＋文字','多步图文助手')]:
   await page.get_by_role('textbox',name='搜索会话或账号').fill(' '+query+' ')
   assert await rows()==1 and expected in await page.locator('.task-table').inner_text()
  await page.get_by_role('textbox',name='搜索会话或账号').fill('')
  await page.get_by_role('button',name='分类筛选',exact=True).click()
  await page.get_by_role('combobox',name='按会话类型筛选').focus()
  await page.get_by_role('combobox',name='按会话类型筛选').select_option('private')
  assert await page.evaluate("document.activeElement.id==='catalog-kind'")
  assert await rows()==2
  await page.get_by_role('combobox',name='按签到方式筛选').select_option('image_text')
  assert await rows()==1
  assert '多步图文助手' in await page.locator('.task-table').inner_text()
  assert '图文' in await page.locator('.inspector').inner_text()
  await page.get_by_role('combobox',name='按动作结构筛选').select_option('multi')
  await page.get_by_role('combobox',name='按共用系统筛选').select_option('named:系统甲')
  assert await rows()==1
  await page.get_by_role('combobox',name='按会话类型筛选').select_option('forum')
  await page.get_by_role('button',name='清除筛选',exact=True).wait_for()
  assert await page.locator('.inspector').count()==0
  await page.get_by_role('button',name='清除筛选',exact=True).click()
  assert await rows()==8
  await page.get_by_role('combobox',name='筛选账号',exact=True).select_option('主账号')
  assert '2 / 5' in ' '.join((await page.locator('.metric-success').inner_text()).split())
  await page.get_by_role('combobox',name='按会话类型筛选').select_option('forum')
  assert await rows()==1
  await page.get_by_role('button',name='清除分类',exact=True).click()
  assert await rows()==5
  assert await page.get_by_role('combobox',name='筛选账号',exact=True).input_value()=='主账号'
  await page.locator('.metric-failed').click()
  assert await rows()==1 and '图片群' in await page.locator('.task-table').inner_text()
  await page.get_by_role('button',name='全部会话',exact=True).click()
  await page.get_by_role('combobox',name='筛选账号',exact=True).select_option('')
  await page.get_by_role('combobox',name='按会话类型筛选').select_option('private')
  status['job']={'id':'completed','mode':'checkin','status':'completed','retry_map':{'主账号':['-102','-103']}}
  await page.get_by_role('button',name='查看待处理',exact=True).wait_for(timeout=7000)
  await page.get_by_role('button',name='查看待处理',exact=True).click()
  assert await rows()==2 and await page.get_by_role('combobox',name='按会话类型筛选').input_value()==''
  await page.get_by_role('button',name='全部会话',exact=True).click()
  await page.get_by_role('combobox',name='按会话类型筛选').select_option('unknown')
  assert await rows()==1 and '用户名会话示例' in await page.locator('.task-table').inner_text()
  await page.get_by_role('button',name='清除分类',exact=True).click()
  await page.get_by_role('button',name='异常优先',exact=True).click()
  assert '图片群' in await page.locator('.task-table tbody tr').first.inner_text()
  assert '论坛群' in await page.locator('.task-table tbody tr').nth(1).inner_text()
  await page.get_by_role('button',name='异常优先',exact=True).click()
  await page.screenshot(path=str(OUT/'catalog-desktop.png'))
  await page.set_viewport_size({'width':1024,'height':950})
  await page.get_by_role('button',name='多步图文助手',exact=True).click()
  await page.wait_for_function("document.querySelector('.inspector').getBoundingClientRect().top<innerHeight && document.querySelector('.inspector').getBoundingClientRect().top>=-2")
  await page.get_by_role('button',name='返回会话列表',exact=True).click()
  for width in (390,320):
   await page.set_viewport_size({'width':width,'height':950})
   assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
   assert await page.locator('.mobile-row-account').first.is_visible()
   assert '主账号' in await page.locator('.mobile-row-account').first.inner_text()
  await page.screenshot(path=str(OUT/'catalog-mobile.png'),full_page=True)
  await page.set_viewport_size({'width':1440,'height':1080})
  await page.evaluate('window.scrollTo(0,0)')
  await page.get_by_role('combobox',name='外观').select_option('dark')
  assert await page.locator('.row-run button').first.evaluate("b=>getComputedStyle(b).backgroundColor===getComputedStyle(document.querySelector('.inspector')).backgroundColor")
  assert await page.locator('.metric-success').evaluate("b=>getComputedStyle(b).color!==getComputedStyle(b).backgroundColor")
  await page.screenshot(path=str(OUT/'catalog-dark.png'))
  await browser.close()
 assert not errors and not writes,(errors,writes)
 result={'passed':True,'js_errors':errors,'api_writes':writes,'checks':['conversation_kinds','five_methods','multi_step_any_method','combined_filters','account_scoped_counts','reset_keeps_account','unknown_username','metric_filter','attention_sort','tablet_details_return','mobile_account_identity','dark_theme']}
 (OUT/'results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
 print(json.dumps(result))


if __name__=='__main__':asyncio.run(main())

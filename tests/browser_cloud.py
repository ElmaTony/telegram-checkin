"""Cloud preview and live task navigation using anonymous, intercepted APIs."""
import asyncio
import json
import os
from pathlib import Path
import sys
from urllib.parse import urlparse
from playwright.async_api import async_playwright

ROOT=Path(__file__).resolve().parents[1]
async def main():
    cfg={'accounts':[{'name':'示例账号','groups':[{'chat':'-101','title':'社区甲','message':'签到'},{'chat':'-102','title':'社区乙','message':'签到'}]}]}
    groups=[{'chat':g['chat'],'name':g['title'],'method':'text','done':False,'confirm':None} for g in cfg['accounts'][0]['groups']]
    status={'today':'2026-11-08','busy':False,'accounts':[{'name':'示例账号','total':2,'done':0,'groups':groups}],'browser':{'visible_available':False},'server_time':1000,'progress':{},'job':None}
    posts=[];errors=[]
    async def intercept(route):
        request=route.request;path=urlparse(request.url).path
        if request.method=='POST':
            data=request.post_data_json;posts.append({'path':path,'data':data})
            if path=='/api/checkin_now':
                status.update(busy=True,job={'id':'fixture','status':'running','mode':'checkin','show_browser':False},progress={'示例账号':{'chat':'-102','name':'社区乙','stage':'等待反馈','sent_at':997}})
                return await route.fulfill(status=202,json={'ok':True,'job_id':'fixture'})
            if path=='/api/cancel':status.update(busy=False,job=None,progress={})
            return await route.fulfill(json={'ok':True})
        if path=='/api/config':return await route.fulfill(json={**cfg,'_revision':'fixture'})
        if path=='/api/status':return await route.fulfill(json=status)
        return await route.fulfill(json={'ok':True})
    async with async_playwright() as pw:
        browser=await pw.chromium.launch(**({'channel':'msedge'} if sys.platform=='win32' else {}),headless=True)
        page=await browser.new_page(viewport={'width':1440,'height':1000})
        page.on('pageerror',lambda error:errors.append(str(error)))
        await page.route('**/api/**',intercept)
        await page.goto(os.environ.get('CHECKIN_TEST_URL','http://127.0.0.1:8577'))
        await page.get_by_role('button',name='全部账号待签到',exact=True).click()
        control=page.get_by_role('checkbox',name='显示签到过程',exact=False)
        assert not await control.is_checked() and await control.is_disabled()
        assert await page.get_by_text('当前服务器没有可视桌面，本轮使用后台浏览器。',exact=True).is_visible()
        await page.get_by_role('button',name='确认发送',exact=True).click()
        await page.locator('.is-running .row-stage').filter(has_text='发送后 3 秒').wait_for()
        assert posts[0]['data']=={'show_browser':False}
        await page.get_by_role('textbox',name='搜索会话或账号').fill('社区甲')
        await page.get_by_role('button',name='查看当前会话',exact=True).click()
        await page.locator('.inspector').get_by_role('heading',name='社区乙',exact=True).wait_for()
        assert await page.get_by_role('textbox',name='搜索会话或账号').input_value()==''
        assert await page.locator('.task-table tbody tr').count()==2
        output=ROOT/'audit/ui-check';output.mkdir(parents=True,exist_ok=True)
        await page.screenshot(path=str(output/'running-navigation.png'),full_page=True)
        await page.set_viewport_size({'width':390,'height':844})
        assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
        await page.get_by_role('button',name='取消任务',exact=True).first.click()
        assert not errors,errors
        await browser.close()
    print(json.dumps({'passed':True,'checks':['headless_preview','server_clock_elapsed','follow_running_chat','mobile_width'],'api_writes_intercepted':len(posts),'telegram_calls':0,'js_errors':errors}))

if __name__=='__main__':asyncio.run(main())

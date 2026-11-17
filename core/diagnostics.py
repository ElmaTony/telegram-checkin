"""Local deployment checks; never open account profiles or contact external sites."""
import platform
import tempfile
from pathlib import Path
from .paths import BASE_DIR, RESOURCE_DIR


async def diagnose(browser=False):
    checks = []
    def add(name, ok, detail):
        checks.append({'name':name, 'ok':bool(ok), 'detail':detail})
    add('web_resources', (RESOURCE_DIR/'gui.html').is_file() and (RESOURCE_DIR/'static/app.js').is_file(), '网页资源完整' if (RESOURCE_DIR/'gui.html').is_file() else '网页资源缺失')
    try:
        BASE_DIR.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryFile(dir=BASE_DIR) as stream:
            stream.write(b'local write check')
        add('data_writable', True, str(BASE_DIR))
    except OSError:
        add('data_writable', False, '数据目录不可写，请设置 CHECKIN_HOME')
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo('Asia/Hong_Kong')
        add('timezone', True, 'Asia/Hong_Kong')
    except Exception:
        add('timezone', False, '时区数据不可用')
    try:
        from playwright.async_api import async_playwright
        async with async_playwright() as p:
            installed = Path(p.chromium.executable_path).is_file()
            add('chromium_installed', installed, 'Chromium 已安装' if installed else '先运行 --install-browser，Linux 还需安装浏览器系统依赖')
            if browser and installed:
                instance = await p.chromium.launch(headless=True)
                try:
                    page = await instance.new_page()
                    await page.set_content('<title>local-check</title><p>ready</p>')
                    add('browser_launch', await page.title() == 'local-check', '无界面浏览器可运行')
                finally:
                    await instance.close()
    except Exception:
        add('browser_launch' if browser else 'playwright_driver', False, '浏览器或驱动不可运行，请检查安装和 Linux 系统依赖')
    return {'ok':all(row['ok'] for row in checks), 'version':(RESOURCE_DIR/'VERSION').read_text(encoding='utf-8').strip(),
            'system':platform.system(), 'architecture':platform.machine(), 'data_home':str(BASE_DIR),
            'network_checked':False, 'checks':checks}

"""The same entry point is used by source installations and standalone packages."""
import argparse
import asyncio
import json
import os
import sys


def main():
    parser = argparse.ArgumentParser(description='Telegram 签到工作台')
    parser.add_argument('--home', help='配置、登录会话和状态的数据目录')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--gui', action='store_true', help='启动网页工作台（默认）')
    mode.add_argument('--login', metavar='ACCOUNT', help='为已保存账号打开扫码浏览器')
    mode.add_argument('--run', action='store_true', help='执行一轮签到')
    mode.add_argument('--schedule', action='store_true', help='常驻每日调度服务')
    mode.add_argument('--install-browser', action='store_true', help='下载 Chromium 浏览器')
    mode.add_argument('--install-browser-deps', action='store_true', help='安装 Linux 浏览器系统依赖（需要管理员权限）')
    mode.add_argument('--doctor', action='store_true', help='检查本机运行环境，不连接 Telegram')
    mode.add_argument('--selftest', action='store_true', help='执行纯逻辑自检')
    mode.add_argument('--notify-test', action='store_true', help='测试 Server酱微信推送')
    mode.add_argument('--version', action='store_true', help='显示版本')
    parser.add_argument('--browser', action='store_true', help='诊断时实际启动无界面浏览器')
    parser.add_argument('--account', help='只执行指定账号，与 --run 配合使用')
    args = parser.parse_args()
    if args.browser and not args.doctor:
        parser.error('--browser 需要同时使用 --doctor')
    if args.account and not args.run:
        parser.error('--account 需要同时使用 --run')
    if args.home:
        os.environ['CHECKIN_HOME'] = args.home
    from core.paths import RESOURCE_DIR, configure_browser_path
    configure_browser_path()
    if args.version:
        print((RESOURCE_DIR/'VERSION').read_text(encoding='utf-8').strip())
    elif args.install_browser or args.install_browser_deps:
        from playwright.__main__ import main as playwright_main
        sys.argv = ['playwright', 'install-deps' if args.install_browser_deps else 'install', 'chromium']
        return playwright_main() or 0
    elif args.doctor:
        from core.diagnostics import diagnose
        result = asyncio.run(diagnose(browser=args.browser))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result['ok'] else 1
    elif args.login:
        from web_engine import cmd_login
        asyncio.run(cmd_login(args.login))
    elif args.selftest:
        from checkin import selftest
        return selftest()
    elif args.notify_test:
        from core.config import load_config
        from core.log import setup_logging
        from core.state import load_state
        from core import notifications
        setup_logging()
        result = notifications.send_preview(load_config(), load_state())
        return 0 if result['ok'] else 1
    elif args.run or args.schedule:
        from checkin import main as checkin_main, scheduled_main
        sys.argv = ['checkin', *(['--schedule'] if args.schedule else []), *(['--account', args.account] if args.account else [])]
        try:
            return asyncio.run(scheduled_main() if args.schedule else checkin_main(args.account)) or 0
        except KeyboardInterrupt:
            print('\n已中断')
            return 130
    else:
        from gui import run
        run()
    return 0


if __name__ == '__main__':
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='replace')
    raise SystemExit(main())

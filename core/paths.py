"""Read-only application resources and persistent user data have separate roots."""
import os
import sys
from pathlib import Path


RESOURCE_DIR = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parents[1])).resolve()


def data_home():
    configured = os.environ.get('CHECKIN_HOME', '').strip()
    if configured:
        return Path(configured).expanduser().resolve()
    if not getattr(sys, 'frozen', False):
        return Path(__file__).resolve().parents[1]
    if sys.platform == 'win32':
        return Path(os.environ.get('LOCALAPPDATA') or Path.home()/'AppData'/'Local')/'TelegramCheckin'
    if sys.platform == 'darwin':
        return Path.home()/'Library'/'Application Support'/'TelegramCheckin'
    return Path(os.environ.get('XDG_DATA_HOME') or Path.home()/'.local'/'share')/'telegram-checkin'


BASE_DIR = data_home().resolve()


def visible_browser_available():
    return sys.platform != 'linux' or bool(os.environ.get('DISPLAY', '').strip() or os.environ.get('WAYLAND_DISPLAY', '').strip())


def account_lock_path(base_dir, safe_account_name):
    return Path(base_dir) / 'data' / 'locks' / (str(safe_account_name).casefold() + '.lock')


def login_command(account):
    if getattr(sys, 'frozen', False):
        return [sys.executable, '--login', account]
    return [sys.executable, str(RESOURCE_DIR/'web_engine.py'), 'login', account]


def configure_browser_path():
    # Source installations keep the existing Playwright cache. Frozen applications
    # download to the persistent data home so replacing the program keeps browsers.
    if getattr(sys, 'frozen', False):
        os.environ.setdefault('PLAYWRIGHT_BROWSERS_PATH', str(BASE_DIR/'data'/'browsers'))

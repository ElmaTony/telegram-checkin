"""日志初始化与日志路径。"""
import logging
from logging.handlers import RotatingFileHandler
import sys

from . import BASE_DIR

LOG_PATH = BASE_DIR / "logs" / "checkin.log"


def setup_logging():
    """初始化根日志器（文件 + 控制台，UTF-8）。"""
    if sys.platform == "win32":
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if root.handlers:  # 幂等：重复调用（GUI/CLI 混用）不重复挂 handler
        return
    file_handler = RotatingFileHandler(LOG_PATH, maxBytes=5*1024*1024, backupCount=3, encoding="utf-8")
    file_handler.setFormatter(fmt)
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(file_handler)
    root.addHandler(console)

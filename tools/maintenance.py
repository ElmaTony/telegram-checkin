"""Conservative cleanup. Preview by default; never traverse profiles or user data."""
import argparse
import json
import os
import shutil
import sys
import time
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core.locks import FileLock

EXCLUDED = {'.git', '.venv', '.audit-venv', '.audit-browsers', 'node_modules', 'data', 'history', 'images'}


def candidates(root=ROOT, current=None):
    root = root.resolve()
    current = current or time.time()
    items = []
    for folder, directories, _ in os.walk(root, followlinks=False):
        directories[:] = [d for d in directories if d not in EXCLUDED and not (Path(folder)/d).is_symlink()]
        if '__pycache__' in directories:
            items.append((Path(folder)/'__pycache__', 'Python 缓存'))
            directories.remove('__pycache__')
        for cache in ('.pytest_cache', '.ruff_cache'):
            if cache in directories:
                items.append((Path(folder)/cache, '开发工具缓存'))
                directories.remove(cache)
    for path in (root/'logs').glob('*'):
        if path.is_file() and (path.name.startswith('capture-') and path.suffix == '.json' or path.name in ('checkin.log.1', 'checkin.log.2', 'checkin.log.3')) and current-path.stat().st_mtime > 30*86400:
            items.append((path, '超过 30 天的诊断日志'))
    for path in (root/'audit').iterdir() if (root/'audit').exists() else []:
        if path.is_dir() and path.name.startswith(('live-review-', 'real-run-', 'regression-', 'draft-test-')):
            if current-path.stat().st_mtime > 14*86400:
                items.append((path, '超过 14 天的审计临时目录'))
            elif path.name.startswith('live-review-') and not (path/'events.jsonl').exists():
                items.append((path, '未启动任务的重复审计快照'))
    # Preserve the running audit entry and reusable visual review helpers.
    for path in (root/'audit').glob('*.py'):
        if path.name not in {'live_review_gui.py','review_gui_live.py','refined-ui-capture.py'}:
            items.append((path, '已完成审计的一次性辅助脚本；保留审计结果'))
    if (root/'build'/'.telegram-checkin-build').is_file():
        items.append((root/'build', '已识别的独立程序构建中间文件'))
    # Current artifacts are temporary: publishing removes them after remote verification.
    version=(root/'VERSION').read_text(encoding='utf-8').strip() if (root/'VERSION').is_file() else None
    for path in (root/'dist').glob('telegram-checkin-v*'):
        match=re.fullmatch(r'telegram-checkin-v(\d+\.\d+\.\d+)(?:-(?:windows|linux|macos)-(?:x64|arm64))?\.(?:zip|tar\.gz)(?:\.sha256)?',path.name)
        if path.is_file() and match and version and match[1]!=version:
            items.append((path, '仅保留当前版本的源码和多平台发行包'))
    return items


def checked_target(root, path):
    root, target = root.resolve(), path.resolve()
    if target == root or not target.is_relative_to(root) or path.is_symlink():
        raise ValueError('清理目标不在允许的项目范围内')
    relative = target.relative_to(root)
    if any(part in EXCLUDED for part in relative.parts):
        raise ValueError('禁止清理配置、运行依赖或用户数据目录')
    return target


def apply(items, root=ROOT):
    for path, _ in items:
        target = checked_target(root, path)
        if target.is_dir():
            shutil.rmtree(target)
        elif target.exists():
            target.unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    with FileLock(ROOT/'data'/'locks'/'round.lock'):
        items = candidates()
        print(json.dumps({'apply': args.apply, 'items': [{'path':str(p.relative_to(ROOT)), 'reason':reason} for p,reason in items]}, ensure_ascii=False, indent=2))
        if args.apply:
            apply(items)


if __name__ == '__main__':
    main()

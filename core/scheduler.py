"""Persistent, once-per-calendar-day scheduling shared by GUI and headless CLI."""
import asyncio
import json
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from . import BASE_DIR
from .config import validate_config
from .locks import FileLock
from .storage import atomic_write
from .timeutil import now

STATE_PATH = BASE_DIR / 'data' / 'schedule-state.json'
DEFAULTS = {'enabled': False, 'time': '09:00', 'timezone': 'Asia/Hong_Kong', 'headless': True, 'catch_up': True, 'accounts': []}
log = logging.getLogger('checkin')


def read_runtime():
    if not STATE_PATH.exists():
        return {}
    try:
        result = json.loads(STATE_PATH.read_text(encoding='utf-8'))
        if not isinstance(result, dict) or (result and (not isinstance(result.get('last_date'), str) or not isinstance(result.get('job_id'), str))):
            raise ValueError()
        if result:
            datetime.strptime(result['last_date'], '%Y-%m-%d')
        return result
    except (OSError, ValueError) as error:
        raise RuntimeError('定时任务状态无法读取，已停止自动执行；请保留文件并人工核对') from error


def write_runtime(value):
    atomic_write(STATE_PATH, json.dumps(value, ensure_ascii=False, indent=2))


def describe(cfg, current=None):
    options = {**DEFAULTS, **cfg.get('schedule', {})}
    try:
        runtime = read_runtime()
        current = current or now(cfg)
        current = current.astimezone(ZoneInfo(options['timezone']))
        hour, minute = map(int, options['time'].split(':'))
        target = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
        claimed = runtime.get('last_date', '') >= current.date().isoformat()
        if claimed or (target <= current and not options['catch_up'] and (current-target).total_seconds() >= 60):
            target += timedelta(days=1)
        next_run = (current if target < current else target).isoformat(timespec='seconds') if options['enabled'] else None
        return {'settings': options, 'next_run': next_run, 'last_run': runtime, 'error': ''}
    except (ValueError, RuntimeError, KeyError) as error:
        return {'settings': options, 'next_run': None, 'last_run': {}, 'error': str(error)}


def is_due(cfg, runtime, current):
    options = {**DEFAULTS, **cfg.get('schedule', {})}
    if not options['enabled']:
        return False
    current = current.astimezone(ZoneInfo(options['timezone']))
    if runtime.get('last_date', '') >= current.date().isoformat():
        return False
    hour, minute = map(int, options['time'].split(':'))
    target = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
    delay = (current-target).total_seconds()
    return delay >= 0 and (options['catch_up'] or delay < 60)


def claim(cfg, job_id, current):
    runtime = read_runtime()
    if not is_due(cfg, runtime, current):
        return False
    write_runtime({'last_date': current.astimezone(ZoneInfo(cfg.get('schedule', {}).get('timezone', DEFAULTS['timezone']))).date().isoformat(),
                   'job_id': job_id, 'status': 'queued', 'started': current.isoformat(timespec='seconds'), 'error': ''})
    return True


def complete(job_id, status, error='', cfg=None):
    runtime = read_runtime()
    if runtime.get('job_id') == job_id:
        runtime.update(status=status, error=error)
        if status != 'running':
            runtime['ended'] = now(cfg).isoformat(timespec='seconds')
        write_runtime(runtime)


class DailyScheduler:
    def __init__(self, read_config, submit):
        self.read_config = read_config
        self.submit = submit
        self.active = False
        self.error = ''

    async def tick(self):
        try:
            cfg = self.read_config()
            error = validate_config(cfg)
            if error:
                raise ValueError(error)
            current = now(cfg)
            runtime = read_runtime()
            if is_due(cfg, runtime, current):
                # submit reserves GUI/CLI and durably claims before it can send.
                await self.submit(cfg, current)
            self.error = ''
        except (Exception, SystemExit) as error:
            message = str(error)
            if message != self.error:
                log.warning('定时任务：%s', message)
            self.error = message

    async def run(self):
        lock = FileLock(BASE_DIR / 'data' / 'locks' / 'scheduler.lock')
        try:
            lock.acquire()
        except RuntimeError:
            self.error = '其他进程正在管理定时任务'
            log.info(self.error)
            return
        self.active = True
        try:
            try:
                runtime = read_runtime()
                if runtime.get('status') in ('queued', 'running'):
                    complete(runtime['job_id'], 'interrupted', '上次进程退出，今日自动任务不会重发；请核对已发送内容')
            except RuntimeError as error:
                self.error = str(error)
            while True:
                await self.tick()
                await asyncio.sleep(10)
        finally:
            self.active = False
            lock.release()

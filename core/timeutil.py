"""One calendar for check-ins, reports and the scheduler, independent of server TZ."""
from contextvars import ContextVar
from datetime import datetime
from zoneinfo import ZoneInfo

_zone = ContextVar('checkin_timezone', default='Asia/Hong_Kong')


def timezone_name(cfg):
    return cfg.get('schedule', {}).get('timezone', 'Asia/Hong_Kong')


def now(cfg=None):
    return datetime.now(ZoneInfo(timezone_name(cfg) if cfg is not None else _zone.get()))


def set_timezone(cfg):
    return _zone.set(timezone_name(cfg))


def reset_timezone(token):
    _zone.reset(token)

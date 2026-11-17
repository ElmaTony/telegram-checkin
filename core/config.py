"""One configuration schema shared by the editor, CLI and browser engine."""
import math
import re
import sys
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import yaml
from . import BASE_DIR
from .storage import atomic_write

CONFIG_PATH = BASE_DIR / "config.yaml"
VALID_METHODS = ("text", "command", "command_button", "image_text", "image_only")
_DEVICES = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def safe_name(name):
    result = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", str(name).strip())
    if (not result or result in (".", "..") or result.endswith((".", " "))
            or result.startswith("_") or result.split(".")[0].upper() in _DEVICES):
        raise ValueError("账号名不能为空、点目录、系统保留名，或以下划线开头")
    return result


def _words(value):
    return isinstance(value, list) and all(isinstance(word, str) and word.strip() for word in value)


def _range(value):
    values = value if isinstance(value, list) else [value]
    if len(values) not in (1, 2):
        return False
    if any(isinstance(n, bool) or not isinstance(n, (int, float)) or not math.isfinite(n) or not 0 <= n <= 3600 for n in values):
        return False
    return len(values) == 1 or values[0] <= values[1]


def validate_config(cfg):
    """Return a readable error, never raise for malformed API input. Empty setup is valid."""
    if not isinstance(cfg, dict):
        return "配置必须是对象"
    accounts = cfg.get("accounts", [])
    if not isinstance(accounts, list):
        return "accounts 必须是账号数组"
    schedule = cfg.get('schedule', {})
    notifications = cfg.get('notifications', {})
    if not isinstance(schedule, dict) or not isinstance(notifications, dict):
        return 'schedule 和 notifications 必须是对象'
    for key in ('enabled', 'headless', 'catch_up'):
        if key in schedule and not isinstance(schedule[key], bool):
            return f'schedule.{key} 必须是布尔值'
    if not isinstance(schedule.get('time', '09:00'), str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', schedule.get('time', '09:00')):
        return '定时签到时间必须为 HH:MM（00:00–23:59）'
    try:
        if not isinstance(schedule.get('timezone', 'Asia/Hong_Kong'), str):
            raise ValueError()
        ZoneInfo(schedule.get('timezone', 'Asia/Hong_Kong'))
    except (ValueError, ZoneInfoNotFoundError):
        return '时区无效，请使用 Asia/Hong_Kong 等 IANA 时区；Windows 请安装 tzdata'
    selected = schedule.get('accounts', [])
    if not isinstance(selected, list) or any(not isinstance(name, str) for name in selected) or len(set(selected)) != len(selected):
        return '定时账号必须为不重复的账号名数组（空数组代表全部）'
    if any(name not in {a.get('name') for a in accounts if isinstance(a, dict) and isinstance(a.get('name'), str)} for name in selected):
        return '定时账号不存在，请重新选择'
    if schedule.get('enabled') and not any(a.get('groups') for a in accounts if isinstance(a, dict) and (not selected or a.get('name') in selected)):
        return '启用定时签到前请配置至少一个签到会话'
    for key in ('enabled', 'only_on_issues'):
        if key in notifications and not isinstance(notifications[key], bool):
            return f'notifications.{key} 必须是布尔值'
    if any(key in notifications for key in ('sendkey', 'send_key', 'key')):
        return 'SendKey 请在定时与通知页单独保存，或使用 SERVERCHAN_SENDKEY 环境变量'
    for key in ('web_proxy', 'web_proxy_host', 'gui_host', 'gui_token'):
        if key in cfg and not isinstance(cfg[key], str):
            return f'{key} 必须是字符串'
    for key in ("group_delay_seconds", "account_delay_seconds"):
        if key in cfg and not _range(cfg[key]):
            return f"{key} 必须是 0–3600 秒，范围下限不能大于上限"
    if "reply_wait_seconds" in cfg and (isinstance(cfg["reply_wait_seconds"], list) or not _range(cfg["reply_wait_seconds"])):
        return "reply_wait_seconds 必须是 0–3600 秒的数字"
    for key in ("success_keywords", "fail_keywords"):
        if key in cfg and not _words(cfg[key]):
            return f"{key} 必须是字符串数组"
    if cfg.get("reply_mode", "reply_only") not in ("after", "reply_only"):
        return "reply_mode 无效（after / reply_only）"
    for key in ("web_headless", "reply_second_pass"):
        if key in cfg and not isinstance(cfg[key], bool):
            return f"{key} 必须是布尔值"
    if cfg.get('web_browser_channel') not in (None, '', 'chrome', 'msedge'):
        return 'web_browser_channel 只能为 chrome 或 msedge'
    if "gui_port" in cfg and (isinstance(cfg["gui_port"], bool) or not isinstance(cfg["gui_port"], int) or not 1 <= cfg["gui_port"] <= 65535):
        return "gui_port 必须是 1–65535 的整数"
    seen = set()
    for index, account in enumerate(accounts, 1):
        if not isinstance(account, dict) or not isinstance(account.get("name"), str):
            return f"账号 {index} 必须包含字符串 name"
        try:
            profile = safe_name(account["name"]).casefold()
        except ValueError as error:
            return str(error)
        if profile in seen:
            return f"账号「{account['name']}」与其他账号的会话文件名冲突，请换一个账号名"
        seen.add(profile)
        groups = account.get("groups", [])
        if not isinstance(groups, list):
            return f"账号「{account['name']}」的 groups 必须是数组"
        chats = set()
        primary_systems = set()
        for group in groups:
            if not isinstance(group, dict):
                return "每个群配置必须是对象"
            chat = str(group.get("chat") or "").strip().lstrip("#")
            if not re.fullmatch(r"-?\d+|@[A-Za-z0-9_]{3,}", chat):
                return f"账号「{account['name']}」有群没填有效 chat（@用户名 或数字 ID）"
            if chat.casefold() in chats:
                return f"账号「{account['name']}」重复配置了会话 {chat}，请合并为多步动作"
            chats.add(chat.casefold())
            if group.get("reply_mode", cfg.get("reply_mode", "reply_only")) not in ("after", "reply_only"):
                return "群的 reply_mode 无效（after / reply_only）"
            for key in ("title", "topic", "folder", "share_group"):
                if key in group and not isinstance(group[key], str):
                    return f"群的 {key} 必须是字符串"
            if 'is_forum' in group and not isinstance(group['is_forum'], bool):
                return 'is_forum 必须是布尔值'
            if group.get('is_forum') and not group.get('topic', '').strip():
                return '论坛群必须选择发送话题'
            if group.get('topic_id') is not None and (not isinstance(group['topic_id'], str) or not re.fullmatch(r'[1-9]\d*', group['topic_id']) or not group.get('topic', '').strip()):
                return 'topic_id 必须是正整数字符串，并同时填写话题名称'
            if 'primary' in group and not isinstance(group['primary'], bool):
                return 'primary 必须是布尔值'
            if group.get('primary') and group.get('share_group'):
                if group['share_group'] in primary_systems:
                    return '每个共用系统只能指定一个主签到群'
                primary_systems.add(group['share_group'])
            for key in ("success_keywords", "fail_keywords"):
                if key in group and not _words(group[key]):
                    return f"群的 {key} 必须是字符串数组"
            actions = group.get("actions")
            if actions is not None and (not isinstance(actions, list) or not actions or any(not isinstance(a, dict) for a in actions)):
                return "actions 必须是非空动作数组"
            for action in actions or [group]:
                method = action.get("method")
                if method is not None and method not in VALID_METHODS:
                    return f"method「{method}」无效（{'/'.join(VALID_METHODS)}）"
                for key in ("message", "image", "click_button"):
                    value = action.get(key)
                    if value is not None and not isinstance(value, str) and not _words(value):
                        return f"动作的 {key} 必须是字符串或字符串数组"
                if method in ("image_text", "image_only") and not action.get("image"):
                    return "图片签到必须配置图片路径"
                if method == "command_button" and not action.get("click_button"):
                    return "口令＋按钮签到必须配置按钮文字"
                if method in ("command", "command_button") and not action.get("message"):
                    return "口令签到必须配置口令"
    return None


def load_config():
    try:
        with open(CONFIG_PATH, encoding="utf-8") as stream:
            cfg = yaml.safe_load(stream) or {}
    except (OSError, yaml.YAMLError) as error:
        sys.exit(f"无法读取配置：{error}")
    error = validate_config(cfg)
    if error:
        sys.exit(error)
    return cfg


def sync_group_keywords(cfg):
    """Fill unset copies only; never overwrite explicit per-account rules."""
    by_chat = {}
    for account in cfg.get("accounts", []):
        for group in account.get("groups", []):
            by_chat.setdefault((str(group.get("chat")), group.get("topic", "")), []).append(group)
    for groups in by_chat.values():
        for key in ("success_keywords", "fail_keywords"):
            source = next((g[key] for g in groups if g.get(key)), None)
            if source:
                for group in groups:
                    if key not in group:
                        group[key] = list(source)


def save_config(cfg):
    error = validate_config(cfg)
    if error:
        raise ValueError(error)
    if CONFIG_PATH.exists():
        previous = CONFIG_PATH.read_text(encoding="utf-8")
        try:
            if isinstance(yaml.safe_load(previous), dict):
                atomic_write(CONFIG_PATH.with_suffix(".yaml.bak"), previous)
        except yaml.YAMLError:
            pass
    atomic_write(CONFIG_PATH, yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False))

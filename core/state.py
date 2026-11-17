"""运行状态持久化（已签标记 / 最近结果 / 群名 / 会话列表缓存）。

本工具是手动触发的单次执行：state 只用于「今天哪个群已经签过」，
避免同一天重复发消息。想看强制重签，在 GUI 里点单群重签即可。
"""
import json
import logging
import threading
from datetime import datetime

from . import BASE_DIR
from .storage import atomic_write
from .timeutil import now as local_now

STATE_PATH = BASE_DIR / "state.json"

log = logging.getLogger("checkin")
_lock = threading.RLock()


def load_state():
    if STATE_PATH.exists() or STATE_PATH.with_suffix('.json.bak').exists():
        for path in (STATE_PATH, STATE_PATH.with_suffix(".json.bak")):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(value, dict):
                    raise ValueError("状态不是对象")
                if path != STATE_PATH:
                    log.warning("状态文件损坏，使用最近有效备份")
                return value
            except (OSError, ValueError):
                continue
        raise RuntimeError("state.json 与备份均无法读取，已停止执行以避免重复发送")
    return {}


def save_state(state):
    with _lock:
        data = json.dumps(state, ensure_ascii=False, indent=2)
        atomic_write(STATE_PATH, data)
        atomic_write(STATE_PATH.with_suffix(".json.bak"), data)


def mark_done(state, acc_name, chat, today):
    state.setdefault(acc_name, {})[chat] = today
    save_state(state)


def record_last(state, account, chat, r, *, mark_date=None):
    """记录今天每个群最近一次的结果（GUI 展示与重签用）。"""
    today = local_now().strftime("%Y-%m-%d")
    last_acc = state.setdefault("_last", {}).setdefault(account, {})
    for k in [k for k, v in last_acc.items() if v.get("date") != today]:
        del last_acc[k]
    error = str(r.get("error") or "")
    detail = error or str(r.get("result_text") or "").strip() \
        or " | ".join(r.get("replies") or [])
    last_acc[chat] = {"date": today, "time": f"{local_now():%H:%M:%S}",
                      "confirm": r.get("confirm") or "failed", "detail": detail,
                      "error": error, "sent": r.get("sent"),
                      "draft_warning": r.get("draft_warning") or '',
                      "stage": r.get("stage", "completed"),
                      "source": r.get("source", "unknown"),
                      "shared_from": r.get("shared_from"),
                      "replies": r.get("replies") or [],
                      "evidence": r.get("evidence") or [],
                      "actions": r.get("actions") or [],
                      "expected_peer": r.get("expected_peer"),
                      "actual_peer": r.get("actual_peer")}
    if mark_date is not None:
        state.setdefault(account, {})[chat] = mark_date
    save_state(state)


def record_title(state, chat, title):
    """记录群聊 ID → 名称（运行时从页面抓取），供所有界面人性化显示。"""
    if not title:
        return
    changed = state.setdefault("_titles", {}).get(chat) != title
    state.setdefault("_titles", {})[chat] = title
    if changed:
        save_state(state)


def prune_state(cfg, state):
    """清掉 state 里已不在配置中的账号/群条目（改配置后旧数据不干扰判断）。"""
    valid = {a["name"]: {str(g.get("chat")) for g in a["groups"]} for a in cfg["accounts"]}
    all_chats = set().union(*valid.values()) if valid else set()
    # _chat_cache 按账号名保留（重加同名账号还能用）
    keep_special = {"_last", "_titles", "_chat_cache"}
    changed = False
    for acc in list(state.keys()):
        if acc in keep_special:
            continue
        if acc not in valid:
            del state[acc]
            changed = True
            continue
        for chat in list(state[acc].keys()):
            if chat not in valid[acc]:
                del state[acc][chat]
                changed = True
    last = state.get("_last")
    if isinstance(last, dict):
        for acc in list(last.keys()):
            if acc not in valid:
                del last[acc]
                changed = True
                continue
            for chat in list(last[acc].keys()):
                if chat not in valid[acc]:
                    del last[acc][chat]
                    changed = True
    titles = state.get("_titles")
    if isinstance(titles, dict):
        for chat in list(titles.keys()):
            if chat not in all_chats:
                del titles[chat]
                changed = True
    # 旧版本留下的当日重试计数：本工具已不再自动重试，直接清掉
    if "_retries" in state:
        del state["_retries"]
        changed = True
    if changed:
        save_state(state)

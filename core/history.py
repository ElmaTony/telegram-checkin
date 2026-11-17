"""打卡记录（CSV 落盘）与文本报告。"""
import csv
import logging
from datetime import datetime

from . import BASE_DIR
from .logic import status_of
from .timeutil import now as local_now

HISTORY_DIR = BASE_DIR / "history"

log = logging.getLogger("checkin")


def result_detail(r):
    """Keep evidence/transport warnings distinct from an absence of replies."""
    if r.get('error'):
        return str(r['error'])
    if r.get('result_text'):
        return str(r['result_text'])
    if r.get('replies'):
        return ' | '.join(r['replies'])
    if r.get('raw_replies') or r.get('evidence'):
        return '已捕获消息，但尚无可确认归属的签到反馈'
    return '已发送，尚无可信关联反馈' if r.get('sent') or r.get('ok') else '未发送，原因未记录'


def append_history(account, r):
    """每条结果立刻追加进 history/年-月.csv，中途崩溃也不丢。"""
    try:
        HISTORY_DIR.mkdir(exist_ok=True)
        path = HISTORY_DIR / f"{local_now():%Y-%m}.csv"
        is_new = not path.exists()
        detail = result_detail(r)
        with open(path, "a", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            if is_new:
                w.writerow(["时间", "账号", "群聊", "状态", "详情"])
            w.writerow([f"{local_now():%Y-%m-%d %H:%M:%S}", account, r["name"],
                        status_of(r), detail])
    except Exception:
        log.exception("写入签到记录失败")


def build_report(acc_name, results):
    """生成文本版报告（日志用；不会向任何会话发送）。"""
    if not results:
        return None
    counts = {"成功": 0, "未确认": 0, "失败": 0}
    lines = [f"📋 签到报告 [{acc_name}]  {local_now():%Y-%m-%d %H:%M}", ""]
    for r in results:
        st = status_of(r)
        counts[st] += 1
        extras = ("（含图片）" if r.get("with_image") else "") + \
                 ("（已点按钮）" if r.get("clicked") else "")
        icon = {"成功": "✅", "未确认": "⚠️", "失败": "❌"}[st]
        lines.append(f"{icon} {r['name']}：{st}{extras}")
        if r.get('error'):
            lines.append(f"　↳ {result_detail(r)}")
        elif r.get("replies"):
            for rep in r["replies"][:3]:
                lines.append(f"　↳ {rep.replace(chr(10), ' / ')}")
        else:
            lines.append(f"　↳ {result_detail(r)}")
    lines.append("")
    lines.append(f"合计 {len(results)} 个群：成功 {counts['成功']}，"
                 f"未确认 {counts['未确认']}，失败 {counts['失败']}")
    return "\n".join(lines)

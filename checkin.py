#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Telegram 自动签到（手动单次执行 / 每日定时服务）。

不需要申请 api_id/api_hash：Playwright 驱动真实浏览器操作 web.telegram.org/k/，
扫码登录一次即可。多账号各自独立会话。

默认跑一次后退出；--schedule 常驻执行每日定时签到。

隐私保证：除正式签到的发送动作外，脚本不会向任何群聊/频道/私聊发送消息。

用法：
    python checkin.py                        跑一轮全部账号，跑完退出
    python checkin.py --account 主号          只跑指定账号
    python checkin.py --gui                  打开网页控制台（等价 python gui.py）
    python checkin.py --selftest             跑逻辑自检（不连接 Telegram）
    python checkin.py --schedule             每日定时服务（无需打开 GUI）
    python checkin.py --notify-test          测试 Server酱微信推送
首次登录：python web_engine.py login 账号名（或 GUI 总览页点「扫码登录」）

代码结构：
    core/config.py    配置读写校验          core/state.py   运行状态持久化
    core/logic.py     纯逻辑（判定/归一）    core/history.py 打卡记录
    core/runner.py    签到执行器（多账号并行）
    web_engine.py     网页版引擎（Playwright）
    gui.py/gui.html   本地网页控制台
"""

import asyncio
import logging
import sys
import uuid

from core.config import load_config, validate_config
from core.log import setup_logging
from core.logic import (click_steps, group_actions, infer_method,
                        judge_replies, merge_action_results)
from core.runner import run_web_round, clean_title
from core.state import load_state
from core import scheduler, notifications

log = logging.getLogger("checkin")


def selftest():
    """纯逻辑回归自检（不连接 Telegram）：python checkin.py --selftest"""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ok = []

    def check(name, actual, expected):
        good = actual == expected
        ok.append(good)
        print(("  ✔ " if good else "  ✘ ") + f"{name}: {actual!r}"
              + ("" if good else f"（期望 {expected!r}）"))

    g_kw = {"success_keywords": ["签到成功，获得简介推广奖励",
                                 "签到失败，您今天已经签到过了"]}
    print("[判定] 群自定义词优先：")
    check("自定义成功词命中", judge_replies(g_kw, {}, ["签到成功，获得简介推广奖励；以账户记录为准"]), "success")
    check("自定义已签到=成功", judge_replies(g_kw, {}, ["签到失败，您今天已经签到过了。"]), "success")
    check("无词兜底全局失败", judge_replies({}, {}, ["签到失败"]), "failed")
    check("无词兜底全局成功", judge_replies({}, {}, ["签到成功"]), "success")
    check("都没命中=未确认", judge_replies({}, {}, ["你好呀"]), "unconfirmed")
    check("空反馈=未确认", judge_replies({}, {}, []), "unconfirmed")
    check("兜底全池忽略泛失败词", judge_replies({}, {}, ["本轮游戏失败扣款名单", "闲聊"]), "unconfirmed")
    check("回复我的池泛失败词生效", judge_replies({}, {}, [], ["操作失败，请重试"]), "failed")
    check("回复我的池成功词命中", judge_replies({}, {}, [], ["签到成功，积分加20"]), "success")
    print("[标题] 页面抓取的群名清洗：")
    check("PUA 图标字形清空", clean_title("\ue92b\ue921\ue92d"), "")
    check("正常群名保留", clean_title(" 示例助手 "), "示例助手")
    check("图标+文字只留文字", clean_title("\ue92b示例讨论群\ue92d"), "示例讨论群")

    print("[判定] 判定词支持 {n} 通配（数字每天变）：")
    real = ["你今天已经签到过了，是今天第 172 位签到。",
            "本次原奖励：+10 群金币",
            "🏅 今天第 172 位签到｜🔥 连续签到 2 天｜💰群金币余额 62389"]
    check("{n} 匹配今天的名次",
          judge_replies({"success_keywords": ["第 {n} 位签到"]}, {}, real), "success")
    check("{n} 匹配明天的名次",
          judge_replies({"success_keywords": ["第 {n} 位签到"]}, {},
                        ["你今天已经签到过了，是今天第 173 位签到。"]), "success")
    check("写死数字会失效（对照组）",
          judge_replies({"success_keywords": ["第 172 位签到"]}, {},
                        ["你今天已经签到过了，是今天第 173 位签到。"]), "unconfirmed")
    check("打卡说法也能通配",
          judge_replies({"success_keywords": ["第 {n} 位打卡"]}, {},
                        ["你今天已经打过卡了，是今天第 38 位打卡，明天再来即可。"]), "success")
    check("普通词仍是包含匹配",
          judge_replies({"success_keywords": ["已经签到过了"]}, {}, real), "success")
    check("{n} 也用于失败词",
          judge_replies({"fail_keywords": ["距下次签到还有 {n} 小时"]}, {},
                        ["签到失败，距下次签到还有 6 小时"]), "failed")
    check("X 与 {n} 等价",
          judge_replies({"success_keywords": ["第 X 位签到"]}, {}, real), "success")
    check("X 后面没空格也能命中",
          judge_replies({"success_keywords": ["第 X位签到"]}, {}, real), "success")
    check("X 不会被当占位符误伤字母词",
          judge_replies({"success_keywords": ["Xbox 群"]}, {}, ["Xbox 群 签到成功"]), "success")

    print("[归一] actions/老配置：")
    check("老单方式", group_actions({"chat": 1, "method": "text", "message": "签到"}),
          [{"method": "text", "message": "签到"}])
    check("多步 actions", [a["method"] for a in group_actions(
        {"chat": 1, "actions": [{"method": "image_text", "message": "打卡", "image": "x.png"},
                                {"method": "text", "message": "签到"}]})],
          ["image_text", "text"])
    check("空 actions 兜底", group_actions({"chat": 1, "actions": ["oops"], "method": "text"}),
          [{"method": "text"}])
    check("老配置识别图片", infer_method({"image": "a.jpg", "message": "打卡"}), "image_text")

    print("[汇总] 多动作合并：")
    m = merge_action_results([
        {"ok": True, "clicked": False, "with_image": True, "replies": ["打卡成功"],
         "error": None, "confirm": "success"},
        {"ok": True, "clicked": False, "with_image": False, "replies": ["签到成功"],
         "error": None, "confirm": "success"}])
    check("全 ok", m["ok"], True)
    check("带判定取最坏(全成功)", m["confirm"], "success")
    m_worst = merge_action_results([
        {"ok": True, "clicked": False, "with_image": False, "replies": [], "error": None,
         "confirm": "success"},
        {"ok": True, "clicked": False, "with_image": False, "replies": [], "error": None,
         "confirm": "failed"}])
    check("带判定取最坏(一败)", m_worst["confirm"], "failed")
    check("无判定字段按未确认", merge_action_results([
        {"ok": True, "clicked": False, "with_image": False, "replies": [], "error": None},
        {"ok": True, "clicked": False, "with_image": False, "replies": [], "error": None},
    ])["confirm"], "unconfirmed")
    m2 = merge_action_results([
        {"ok": True, "clicked": False, "with_image": False, "replies": [], "error": None},
        {"ok": False, "clicked": False, "with_image": False, "replies": [], "error": "超时"}])
    check("一步失败整体失败", m2["ok"], False)
    check("错误带动作序号", m2["error"], "动作2: 超时")
    check("按钮未找到的诊断不被吞", merge_action_results([
        {"ok": True, "clicked": False, "with_image": False, "error": None,
         "replies": ["⚠ 未找到按钮「确定签到」（第 1/1 级）"]}])["replies"],
          ["⚠ 未找到按钮「确定签到」（第 1/1 级）"])

    print("[校验] 配置：")
    check("method 拼写报错", "method" in (validate_config({"accounts": [
        {"name": "A", "groups": [{"chat": 1, "method": "txt"}]}]}) or ""), True)
    check("账号名冲突报错", "冲突" in (validate_config({"accounts": [
        {"name": "A/B", "groups": [{"chat": 1, "method": "text"}]},
        {"name": "A_B", "groups": [{"chat": 2, "method": "text"}]}]}) or ""), True)
    check("空群账号允许", validate_config({"accounts": [
        {"name": "T", "groups": []}]}) is None, True)

    print("[按钮] 多级按钮序列：")
    check("单按钮", click_steps({"click_button": "确定签到"}), ["确定签到"])
    check("多级按钮", click_steps({"click_button": ["确定签到", "领取奖励"]}),
          ["确定签到", "领取奖励"])
    check("空按钮", click_steps({}), [])

    print("[共用系统] 主签到群优先：")
    from core.runner import order_chain_members as _ord
    check("主签到群排到队头",
          [g["chat"] for g in _ord([{"chat": "1"}, {"chat": "2", "primary": True},
                                    {"chat": "3"}])],
          ["2", "1", "3"])
    check("没有主群时保持配置顺序",
          [g["chat"] for g in _ord([{"chat": "1"}, {"chat": "2"}])], ["1", "2"])
    check("主群本来就在最前时顺序不变",
          [g["chat"] for g in _ord([{"chat": "1", "primary": True}, {"chat": "2"}])],
          ["1", "2"])

    print("[共用系统] 一个签中 → 全组标记成功：")
    import core.runner as _R
    import core.state as _S
    _save, _hist = _S.save_state, _R.append_history
    _S.save_state = lambda st: None      # 纯内存，不落盘
    _R.append_history = lambda *a, **k: None
    try:
        from datetime import datetime as _dt
        _t = _dt.now().strftime("%Y-%m-%d")
        _st = {}
        # A 先尝试并失败，B 成功，C 还没轮到
        _S.record_last(_st, "ACC", "A", {"ok": False, "confirm": "failed", "replies": []})
        _S.mark_done(_st, "ACC", "B", _t)
        _S.record_last(_st, "ACC", "B", {"ok": True, "confirm": "success",
                                         "replies": ["签到成功"]})
        _R.share_result_to_members(_st, "ACC", _t, "sys",
                                   [{"chat": "A"}, {"chat": "B"}, {"chat": "C"}], "B")
        _conf = {c: _st["_last"]["ACC"][c]["confirm"] for c in ("A", "B", "C")}
        check("之前失败的成员被覆盖成成功", _conf["A"], "success")
        check("成功者本身保持成功", _conf["B"], "success")
        check("没跑到的成员也标记成功", _conf["C"], "success")
        check("全组都记成今日已完成",
              all(_st["ACC"][c] == _t for c in ("A", "B", "C")), True)
        check("代签保留来源与成功反馈",
              _st["_last"]["ACC"]["A"]["shared_from"]["chat"] == "B"
              and "签到成功" in _st["_last"]["ACC"]["A"]["detail"], True)
    finally:
        _S.save_state, _R.append_history = _save, _hist

    print()
    if all(ok):
        print(f"✅ 自检全部通过（{len(ok)} 项）")
        return 0
    print(f"❌ 自检有 {ok.count(False)} 项失败")
    return 1


def parse_only(argv):
    """解析 --account 参数。"""
    if "--account" in argv:
        i = argv.index("--account")
        if i + 1 >= len(argv):
            sys.exit("--account 后面要跟账号名，例如：python checkin.py --account 主号")
        return argv[i + 1]
    return None


async def main():
    setup_logging()
    cfg = load_config()
    only = parse_only(sys.argv[1:])
    if not cfg.get("accounts"):
        sys.exit("尚未配置账号，请先打开控制台添加账号与会话")
    if only and only not in {a["name"] for a in cfg["accounts"]}:
        sys.exit(f"找不到账号：{only}")
    state = load_state()

    log.info("开始签到%s（单次执行，跑完即退出）",
             f"（账号：{only}）" if only else "")
    try:
        retry_map = await run_web_round(cfg, state, only)
    except asyncio.CancelledError:
        await notifications.notify_round(cfg, state, only=only, cancelled=True)
        raise
    except Exception:
        await notifications.notify_round(cfg, state, only=only, error='任务异常')
        raise
    await notifications.notify_round(cfg, state, only=only)
    if retry_map:
        log.warning("有未成功的群（可在 GUI 总览点单个群重签）：%s", retry_map)
    else:
        log.info("本轮全部完成")
    return 1 if retry_map else 0


async def scheduled_main():
    setup_logging()

    async def submit(cfg, current):
        job_id = uuid.uuid4().hex
        # Reserve the round before claiming. Another GUI/CLI round can finish first.
        from core.locks import FileLock
        from core import BASE_DIR
        try:
            reserve = FileLock(BASE_DIR / 'data' / 'locks' / 'round.lock').acquire()
        except RuntimeError:
            return False
        try:
            if not scheduler.claim(cfg, job_id, current):
                return False
        finally:
            reserve.release()
        cfg = dict(cfg)
        cfg['web_headless'] = cfg.get('schedule', {}).get('headless', True)
        selected = cfg.get('schedule', {}).get('accounts', [])
        state = {}
        status, error = 'completed', ''
        try:
            scheduler.complete(job_id, 'running', cfg=cfg)
            state = load_state()
            await run_web_round(cfg, state, accounts=selected)
        except asyncio.CancelledError:
            status = 'cancelled'
            await notifications.notify_round(cfg, state, accounts=selected, cancelled=True, source='每日定时签到')
            raise
        except Exception as failure:
            status, error = 'failed', str(failure)
            await notifications.notify_round(cfg, state, accounts=selected, error='任务异常', source='每日定时签到')
        else:
            await notifications.notify_round(cfg, state, accounts=selected, source='每日定时签到')
        finally:
            scheduler.complete(job_id, status, error, cfg)
        return True

    log.info('每日定时服务已启动；按配置时间执行，Ctrl+C 停止')
    await scheduler.DailyScheduler(load_config, submit).run()


if __name__ == "__main__":
    argv = sys.argv[1:]
    if "--gui" in argv:
        import gui
        gui.run()
    elif "--selftest" in argv:
        sys.exit(selftest())
    elif "--notify-test" in argv:
        setup_logging()
        result = notifications.send_preview(load_config(), load_state())
        sys.exit(0 if result['ok'] else 1)
    else:
        try:
            sys.exit(asyncio.run(scheduled_main() if '--schedule' in argv else main()))
        except KeyboardInterrupt:
            print("\n已中断")

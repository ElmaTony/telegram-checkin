"""纯逻辑：签到方式归一 / 结果判定 / 关键词优先级 / 数据提取（可独立单测）。"""
import random
import re
from functools import lru_cache
from pathlib import Path

from . import BASE_DIR

VALID_METHODS = ("text", "command", "command_button", "image_text", "image_only")
DEFAULT_SUCCESS_KW = ["签到成功", "打卡成功", "签到完成", "已签到", "成功"]
DEFAULT_FAIL_KW = ["签到失败", "打卡失败", "失败", "禁言", "被禁", "封禁", "错误", "无法", "异常"]
_GENERIC_RESULT_WORDS = {"成功", "失败", "禁言", "被禁", "封禁", "错误", "无法", "异常"}
_CONFIRMATION_PROMPT = re.compile(r"(?:是否|要不要)[^。！？\n]{0,16}(?:继续|确认)[^。！？\n]{0,12}(?:签到|打卡)")
_EXPLICIT_RESULT = re.compile(r"(?:签到|打卡)(?:已)?(?:成功|完成|失败|错误|异常)|已签到|(?:无法|不能|禁止)(?:完成)?(?:签到|打卡)")


def _pending_confirmation(text):
    return bool(_CONFIRMATION_PROMPT.search(text) and not _EXPLICIT_RESULT.search(text))


def _result_keyword_hit(text, keyword):
    # A question about continuing is an intermediate step. Its background error
    # words must not outrank a later result; explicit/custom phrases still apply.
    if str(keyword).strip() in _GENERIC_RESULT_WORDS and _pending_confirmation(text):
        return False
    return _kw_hit(text, keyword)


def pick(value):
    """值是列表则随机选一条，否则原样返回。"""
    if isinstance(value, (list, tuple)):
        return random.choice(value) if value else None
    return value


def resolve_path(p):
    """图片等资源路径：绝对路径原样，相对路径基于项目目录。"""
    path = Path(str(p))
    return path if path.is_absolute() else (BASE_DIR / path)


def delay_range(value, default):
    """把配置里的间隔（数字或 [最小, 最大]）归一成 (lo, hi)。"""
    if isinstance(value, (int, float)):
        return float(value), float(value)
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        return float(value[0]), float(value[1])
    return float(default[0]), float(default[1])


def infer_method(g):
    """method 字段优先（添加群时用户自己选的）；老配置按字段自动识别。"""
    method = g.get("method")
    if method in VALID_METHODS:
        return method
    if pick(g.get("image")):
        return "image_text" if g.get("message") else "image_only"
    return "command_button" if pick(g.get("click_button")) else "text"


def group_actions(g):
    """把群配置归一成动作列表：actions（多步签到）优先，老单方式配置转成单动作。"""
    acts = g.get("actions")
    if isinstance(acts, list) and acts:
        result = [dict(a) for a in acts if isinstance(a, dict)]
        if result:
            return result
    flat = {k: g[k] for k in ("method", "message", "image", "click_button") if k in g}
    return [flat]


def click_steps(act):
    """归一点按钮序列：字符串=单步；列表=多级按钮按顺序逐个点击。"""
    cb = act.get("click_button")
    if not cb:
        return []
    if isinstance(cb, (list, tuple)):
        return [str(x).strip() for x in cb if str(x).strip()]
    return [part.strip() for part in re.split(r"[\n,，]", str(cb)) if part.strip()]


def merge_action_results(action_results):
    """把一个群多个动作的结果汇总：全发送成功才算 ok（反馈在群级统一收集）。"""
    order = {"success": 0, "unconfirmed": 1, "failed": 2}
    multi = len(action_results) > 1
    replies = []
    for i, a in enumerate(action_results, 1):
        pfx = f"[动作{i}] " if multi else ""
        replies.extend(pfx + rep for rep in a.get("replies", []))
    errs = [f"动作{i}: {a['error']}" for i, a in enumerate(action_results, 1) if a["error"]]
    return {"ok": bool(action_results) and all(a["ok"] for a in action_results),
            "clicked": any(a["clicked"] for a in action_results),
            "with_image": any(a["with_image"] for a in action_results),
            "replies": replies,
            "confirm": max((a.get("confirm", "unconfirmed") for a in action_results),
                           key=lambda c: order.get(c, 2), default="failed"),
            "error": "；".join(errs) if errs else None}


def _kw_hit(text, keyword):
    """单条判定词是否命中文本。

    占位符（`{n}` 或独立 `X`）按模板匹配，数字可变；纯文本等价于包含匹配
    （转义后 search）。模板写错则退回包含匹配——绝不因一条词写坏就整轮判失败。
    """
    k = str(keyword).strip()
    if not k:
        return False
    try:
        return bool(compile_template(k).search(text))
    except re.error:
        return k in text


def judge_replies(g, cfg, replies, replied=None):
    """把机器人反馈的全部消息（可能不止一条）合并做判定。

    优先级（高→低）：
      1. 群自定义成功词（用户明确指定的成功信号，如「您今天已经签到过了」）
      2. 群自定义失败词
      3. 全局失败词（继续确认提示中的泛词不作为结果）
      4. 全局成功词
    都没命中 = unconfirmed。群级词表填写即整体覆盖对应全局词表。

    `replied` 是**明确回复给本账号**的那几条消息。群里常有别人也在签到，
    他们的结果同样会出现在窗口里（尤其 bot 用「您」不复述用户名时）。
    传了这个参数就**只采信回复给我的消息**，别人的结果不再参与判定。
    未提供时可用于规则沙盒；显式传入空数组表示没有可信回复，必须未确认。

    注意两种池的严格度不同：
    - 回复给我的池：全部失败词生效（指向明确，误伤极小）
    - 兜底全池：失败词必须带签到语境（含「签」或「打卡」）才生效——
      繁忙群一轮能抓上百条闲聊，「失败/无法/错误」这类泛词必然随机命中
      （如游戏结算里的「失败扣款名单」），会把没等到回复的群误判成红色失败。
      群级自定义失败词不受此限（是用户为这个群明确配置的）。

    每条词支持两种写法：
      - 纯文本：包含匹配，如 `已经签到过了`
      - 带 `{n}`：模板匹配，任意数字都算命中，如 `第 {n} 位签到`
        （今天的第 172 位、明天的第 173 位都能命中，不用天天改词）
    """
    if not replies and not replied:
        return "unconfirmed"
    if replied is not None and not replied:
        return "unconfirmed"
    exact = bool(replied)  # True=池里全是明确回复我的；False=兜底全池（混有别人的消息）
    pool = list(replied) if replied else list(replies or [])
    text = " ".join(str(x) for x in pool)
    terminal_text = " ".join(str(x) for x in pool if not _pending_confirmation(str(x)))
    g_succ = list(g.get("success_keywords") or [])
    g_fail = list(g.get("fail_keywords") or [])
    succ = g_succ or list(cfg.get("success_keywords") or DEFAULT_SUCCESS_KW)
    fail = g_fail or list(cfg.get("fail_keywords") or DEFAULT_FAIL_KW)
    if g_succ and any(_kw_hit(text, k) for k in g_succ):
        return "success"
    if g_fail and any(_kw_hit(text, k) for k in g_fail):
        return "failed"
    # 兜底全池时全局失败词必须带签到语境：繁忙群闲聊里「失败/无法/错误」
    # 这类泛词随机出现（游戏结算、日常吐槽），命中即误判成失败
    eff_fail = fail if exact else [k for k in fail
                                   if ("签" in str(k) or "打卡" in str(k))]
    if any(_kw_hit(terminal_text if str(k).strip() in _GENERIC_RESULT_WORDS else text, k) for k in eff_fail):
        return "failed"
    if any(_kw_hit(terminal_text if str(k).strip() in _GENERIC_RESULT_WORDS else text, k) for k in succ):
        return "success"
    return "unconfirmed"


def feedback_hint(group, cfg, result):
    """Explain pending results without weakening attribution or configured rules."""
    if result.get('confirm') != 'unconfirmed':
        return None
    pending = [action for action in result.get('actions') or [] if action.get('confirm') == 'unconfirmed']
    for action in pending:
        if action.get('buttons_complete') is False:
            return {'code':'buttons_pending', 'message':'必需按钮尚未完成。请先核对原消息，避免重复发送口令。'}
    for action in pending:
        if action.get('sent') is not False and not action.get('sent_mids'):
            return {'code':'missing_message_id', 'message':'发送状态待核对，尚未登记消息 ID。请先在 Telegram 检查，避免重复发送。'}
    for action in pending:
        trusted = action.get('trusted_replies') or []
        if action.get('error') or not action.get('sent') or not action.get('boundary', {}).get('peer') or not trusted:
            continue
        custom = any(settings.get(key) for settings in (group, cfg) for key in ('success_keywords','fail_keywords'))
        if (custom and judge_replies(group, cfg, trusted, trusted) == 'unconfirmed'
                and judge_replies({}, {}, trusted, trusted) != 'unconfirmed'):
            return {'code':'rule_mismatch', 'message':'已捕获本轮关联反馈，但当前自定义判定词未命中。请检查该会话的成功／失败词；可先修改规则，再仅补查反馈。'}
    return None


def pick_result_reply(g, cfg, replies, confirm):
    """从捕获到的多条消息里挑出「最能代表结果」的那条。

    一个群里可能有多条新消息（群公告、群友聊天、多个 bot 的回复），
    总览/日志应该显示**命中判定词的那条**，而不是碰巧排在最前面的那条。
    按时间正序取**第一条**命中项：签到结果通常先于「已签到，请勿重复」这类后续提示。
    找不到命中项时退回最后一条。
    """
    reps = [str(x) for x in (replies or []) if str(x).strip()]
    if not reps:
        return ""
    g_succ = list((g or {}).get("success_keywords") or [])
    g_fail = list((g or {}).get("fail_keywords") or [])
    succ = g_succ or list((cfg or {}).get("success_keywords") or DEFAULT_SUCCESS_KW)
    fail = g_fail or list((cfg or {}).get("fail_keywords") or DEFAULT_FAIL_KW)
    words = succ if confirm == "success" else fail
    custom = g_succ if confirm == "success" else g_fail
    for r in reps:
        for w in words:
            if (_kw_hit(r, w) if custom else _result_keyword_hit(r, w)):
                return r
    return reps[-1]


def status_of(r):
    """结果三态：成功（发送且回复确认）/ 未确认（发出但没等到确认）/ 失败。"""
    if not r.get("ok"):
        return "失败"
    return {"success": "成功", "unconfirmed": "未确认"}.get(r.get("confirm"), "失败")


# ---------------------------------------------------------------------------
# 判定词模板：把 `第 X 位签到` 这类写法编译成正则（只服务判定，不做数据提取）
# ---------------------------------------------------------------------------

# 占位符：`{n}` 或独立的 `X`/`x`（前后不是字母数字，避免误伤 HDSky、Xbox 这类词）
_PLACEHOLDER = re.compile(r"\{n\}|(?<![A-Za-z0-9])[Xx](?![A-Za-z0-9])")


def _esc_seg(seg):
    """片段按字面量转义；空白（含换行/全角空格）一律宽松成 \\s*。"""
    out = []
    for ch in seg:
        if ch in " \u00a0\u3000\t\n\r":
            out.append(r"\s*")
        else:
            out.append(re.escape(ch))
    return "".join(out)


@lru_cache(maxsize=256)
def _compile_template(tpl):
    """把 `第 {n} 位签到` / `第 X 位签到` 编译成 `第\\s*(\\d{1,9})\\s*位签到`。

    - 占位符：`{n}` 或独立 `X`（前后非字母数字），都捕获 1~9 位数字
    - 空格 / 换行 / 全角空格 / Tab → `\\s*`（bot 排版常变，不该因空白差异失配）
    - 其余字符按字面量转义，因此只要复制原话、把数字换成 X 就行，不用懂正则
    """
    tpl = str(tpl)
    out, pos = [], 0
    for m in _PLACEHOLDER.finditer(tpl):
        out.append(_esc_seg(tpl[pos:m.start()]))
        # 数字两侧都允许空白：`X位` 与 `172 位`、`172位` 都能对上
        out.append(r"\s*(\d{1,9})\s*")
        pos = m.end()
    out.append(_esc_seg(tpl[pos:]))
    return re.compile("".join(out), re.S)


def compile_template(tpl):
    return _compile_template(str(tpl))


"""星瑶桌宠「无聊时自己说话」的调度与台词生成。

这是什么：一个后台轻量线程，每 30 秒醒来一次，判断用户是否长时间没碰键鼠；是的话
就让模型用星瑶的语气生成一句短台词，经 `core.pet_manager.push_bubble` 冒到桌宠上。

为什么四类触发条件这样设计：
- 开关（pet_chat_enabled）：不想要就永远不开口，用户在设置页随时能关，关掉时线程
  即使还在跑也一次都不会说话；
- 桌宠在跑 + 系统空闲 ≥ 阈值：`GetLastInputInfo` 看的是「最后一次键鼠输入」，比
  「星瑶有没有在跑任务」更贴近「人是不是不在电脑前」这个真实语义；人一走开就冒一
  句，回来就自然停下，这是不打扰工作的唯一可靠信号；
- 间隔长尾随机：说完一句就抽定下一次的时间点（阈值的 0.4~2.6 倍，众数在 0.7 倍），
  于是有时五六分钟就冒一句、有时二十多分钟不吭声；固定周期或「固定值 × 小抖动」听感
  上都是一个钟，只有真随机才像「自己忽然想起来」；
- 当日上限 40 条：既防模型抽风，也防用户把阈值调成 60 秒时刷屏，顺手兜住成本；
- 星瑶没有任务在跑：任务执行期后端会推「正在读文件…」这类气泡，两条气泡抢同一个
  位置只会互相打断，所以有回合在跑时宁可不说话（详见 `_should_speak` 的说明）。

为什么不读文件内容、不读对话记录：桌宠卖的是「陪伴感」而不是「监视感」。把工作区
内容塞进闲聊提示词，等于给一句无关紧要的台词附上隐私风险；所以提示词只带时间、
当前目录名、宠物形象名三项公开事实，并明确要求不提问、不解释、不带 emoji。
"""

from __future__ import annotations

import collections
import ctypes
import datetime as _dt
import logging
import os
import random
import re
import sys
import threading
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

# 调度节拍：30 秒足够贴合「人离开电脑」这种分钟级事件，又不至于让 daemon 线程
# 频繁抢 GIL（tick 本身只读配置 + 查空闲，不做网络请求）。
_TICK_SECONDS = 30
# 默认空闲阈值 10 分钟：短于这个值，喝口水回来就发现她说了句话，显得吵闹。
_DEFAULT_IDLE_SECONDS = 600
# 单日上限：按条数而不是按 token，口径直观，用户能自己估出来。
_DAILY_LIMIT = 40
# 台词长度上限（字符）：气泡宽度有限，超过一行半就换行溢出，宁可截断也不要糊成一团。
_MAX_CHARS = 40
# 去重样本容量：只保留最近 8 条做「别重复」参考，够压住短期复读，又不占提示词预算。
_RECENT_KEEP = 8

# 设置页下拉能选到的阈值范围（秒）：太短会打扰，太长等于永远不说。
_IDLE_MIN_SECONDS = 60
_IDLE_MAX_SECONDS = 86400

# 气泡停留时长：比普通状态气泡（2600ms）略长，因为这句话用户可能没盯着看。
_BUBBLE_HOLD_MS = 3200

# thinking 标签：非原生思考通道有时把推理过程混进正文，必须先剔掉再截断，
# 否则截断后剩下的全是思考碎片。
_THINK_RE = re.compile(r"<(?:think|thinking)>.*?</(?:think|thinking)>", re.DOTALL | re.IGNORECASE)

# 包裹式引号：中英混排时模型最常"礼貌地"加一层引号，去掉才像自己嘟囔。
_QUOTE_PAIRS = (
    ('"', '"'),
    ("'", "'"),
    ("\u201c", "\u201d"),  # “ ”
    ("\u2018", "\u2019"),  # ‘ ’
    ("\u300c", "\u300d"),  # 「 」
    ("\u300e", "\u300f"),  # 『 』
)

# ---- 运行态（模块级单例：星瑶是单进程应用，闲聊调度只应有一条） ----
_state_lock = threading.RLock()
_last_spoken_at: float = 0.0
# 下一次允许开口的绝对时间戳：说完那一刻由 _roll_next_gap 抽定，0 表示「还没抽过」
_next_speak_at: float = 0.0
_today_count: int = 0
_today_day: str = ""
_recent: "collections.deque[str]" = collections.deque(maxlen=_RECENT_KEEP)
_thread: Optional[threading.Thread] = None
_stop_event = threading.Event()


# --------------------------------------------------------------------- 配置
def _config_value(key: str, default: Any = None) -> Any:
    """读单个配置项（先看模块级属性，再回落 config.json 的载荷）。

    写法照 `core/pet_manager._config_value`：函数内 import 是为了避开模块顶层
    import config 带来的循环导入；config 的 `__getattr__` 对未注册名字会抛
    AttributeError，一律吞掉——读不到配置只该退化成默认值，不该让整个调度线程崩。
    """
    try:
        import config
    except Exception:
        return default
    try:
        value = getattr(config, key, None)
    except Exception:
        value = None
    if value not in (None, ""):
        return value
    try:
        return config._load().get(key, default)
    except Exception:
        return default


def _setting_enabled() -> bool:
    """开关默认关闭：用户没明确勾选过，就不该有模型请求偷偷发出去。"""
    try:
        return bool(_config_value("pet_chat_enabled", False))
    except Exception:
        return False


def _setting_idle_seconds() -> int:
    """空闲阈值：读不到或非法值都回落到默认 10 分钟，并钳进合理区间。"""
    try:
        raw = int(_config_value("pet_chat_idle_seconds", _DEFAULT_IDLE_SECONDS))
    except Exception:
        raw = _DEFAULT_IDLE_SECONDS
    return max(_IDLE_MIN_SECONDS, min(_IDLE_MAX_SECONDS, raw))


def _setting_model_id() -> str:
    """用户指定的模型 id；空串表示「自动」（副模型 → 主模型）。"""
    try:
        return str(_config_value("pet_chat_model_id", "") or "").strip()
    except Exception:
        return ""


# ----------------------------------------------------------------- 空闲检测
class _LASTINPUTINFO(ctypes.Structure):
    """Win32 LASTINPUTINFO：cbSize 必须由调用方填成结构体大小，否则 API 直接失败。"""

    _fields_ = [
        ("cbSize", ctypes.c_uint),
        ("dwTime", ctypes.c_uint),  # 最后一次输入时的 GetTickCount 值（低 32 位）
    ]


def _system_idle_seconds() -> float:
    """距最后一次键鼠输入的秒数；非 Windows 或取不到时返回 0.0（等于永远不触发）。

    用 `GetTickCount64` 而不是 `GetTickCount`：后者 32 位、49.7 天回绕，机器长期
    不关机时算出来的空闲时长会突然变成负数或几十万小时。`dwTime` 只有低 32 位，
    所以两者相减按 2^32 取模——这既抹平回绕，又不受系统运行天数影响。
    """
    if not sys.platform.startswith("win"):
        return 0.0
    try:
        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        info = _LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(_LASTINPUTINFO)
        if not user32.GetLastInputInfo(ctypes.byref(info)):
            return 0.0
        ticks = int(kernel32.GetTickCount64()) & 0xFFFFFFFF
        elapsed_ms = (ticks - int(info.dwTime)) & 0xFFFFFFFF
        return max(0.0, elapsed_ms / 1000.0)
    except Exception:
        # 取不到空闲时长只意味着「这一条永远不满足」，不影响主流程，故只留 debug。
        logger.debug("读取系统空闲时长失败，本次按 0 处理", exc_info=True)
        return 0.0


# ------------------------------------------------------------- 外部状态探测
def _pet_running() -> bool:
    """桌宠进程是否在跑：没起来时气泡通道不存在，先判掉能省下一次模型调用。"""
    try:
        from core import pet_manager

        return bool(pet_manager.get_status().get("running"))
    except Exception:
        logger.debug("查询桌宠状态失败，按未运行处理", exc_info=True)
        return False


def _agent_busy() -> bool:
    """星瑶此刻是否还有任务在跑（近似判定，宁松勿紧）。

    现实约束：回合状态没有进程级权威信号——GUI 的 `_active_turns` 挂在 ApiBridge
    实例上，核心模块拿不到那个实例。这里退一步用两个已有的、只读的登记表近似：
    - `core.turn_undo.coordinator` 的活跃回合登记（回合开始 recording 时会登记）；
    - `core.task_manager.list_active_tasks()`（后台任务是否还在跑）。
    任何异常都按「没有任务」处理：多说一句气泡只是略吵，永久不说话才是功能坏掉。
    """
    try:
        from core.turn_undo import coordinator as coordinator_mod

        coordinator = coordinator_mod.get_coordinator()
        if coordinator.has_active_turn(coordinator_mod.thread_workspace_root()):
            return True
    except Exception:
        logger.debug("查询撤销协调器活跃回合失败，跳过该项", exc_info=True)
    try:
        from core import task_manager

        if task_manager.list_active_tasks():
            return True
    except Exception:
        logger.debug("查询后台任务表失败，跳过该项", exc_info=True)
    return False


# ------------------------------------------------------------------- 模型解析
def _model_entry(provider: dict, model: dict) -> dict:
    """把 providers[].models[] 的一项整理成与 `get_secondary_model()` 同形状的请求参数。"""
    from core.model_manager import effective_provider_api_key

    return {
        "litellm_model": str(model.get("litellm_model") or ""),
        # 多 Key provider 只取「当前该用」的那一把，与 get_active_model 口径一致
        "api_key": effective_provider_api_key(provider),
        "base_url": str(provider.get("base_url") or ""),
        "name": str(model.get("name") or model.get("id") or ""),
        "id": str(model.get("id") or ""),
    }


def _model_by_id(model_id: str) -> Optional[dict]:
    """按 id 在 providers[].models[] 里找模型；找不到返回 None。

    model_manager 现有的 `get_model_config_by_id` 只返回模型条目本身（不含
    provider 的 base_url / api_key），发请求还差一半信息，所以在内部补一个遍历。
    """
    from core import model_manager

    wanted = str(model_id or "").strip()
    if not wanted:
        return None
    data = model_manager.load_config()
    for provider in data.get("providers") or []:
        for model in provider.get("models") or []:
            if str(model.get("id") or "") == wanted:
                entry = _model_entry(provider, model)
                return entry if entry["litellm_model"] else None
    return None


def _resolve_model() -> Optional[dict]:
    """按「指定模型 → 副模型 → 当前激活模型」解析出这次要用的模型；解析不到返回 None。

    这个顺序的理由：用户显式指定就用她的选择（哪怕贵也是她的决定）；没指定时借用
    副模型——闲聊是典型的廉价后台任务，用主模型纯属浪费；连副模型都没配才退回主模型。
    """
    wanted = _setting_model_id()
    if wanted:
        try:
            entry = _model_by_id(wanted)
        except Exception:
            entry = None
        if entry:
            return entry
        # 指定模型被删掉/改名时静默回退，而不是整轮放弃：用户不会想因为一次删除
        # 就让桌宠永久闭嘴。
        logger.debug("pet_chat_model_id=%r 在配置中找不到，回退副模型/主模型", wanted)
    try:
        from core import model_manager

        secondary = model_manager.get_secondary_model()
    except Exception:
        logger.debug("读取副模型失败，回退主模型", exc_info=True)
        secondary = None
    if secondary and secondary.get("litellm_model"):
        return {
            "litellm_model": str(secondary.get("litellm_model") or ""),
            "api_key": secondary.get("api_key") or "",
            "base_url": str(secondary.get("base_url") or ""),
            "name": str(secondary.get("name") or ""),
            "id": str(secondary.get("id") or ""),
        }
    try:
        from core import model_manager

        active = model_manager.get_active_model()
    except Exception:
        logger.debug("读取当前激活模型失败", exc_info=True)
        return None
    if active and active.get("litellm_model"):
        return {
            "litellm_model": str(active.get("litellm_model") or ""),
            "api_key": active.get("api_key") or "",
            "base_url": str(active.get("base_url") or ""),
            "name": str(active.get("name") or ""),
            "id": str(active.get("id") or ""),
        }
    return None


# --------------------------------------------------------------------- 提示词
def _time_phrase(now: _dt.datetime) -> str:
    """把小时折成中文时段：模型对「傍晚」的语感比「18:20」准得多。"""
    hour = now.hour
    if hour < 5:
        return "凌晨"
    if hour < 11:
        return "上午"
    if hour < 13:
        return "中午"
    if hour < 17:
        return "下午"
    if hour < 19:
        return "傍晚"
    return "深夜"


def _skin_name() -> str:
    """当前宠物形象名；读不到就回落「星瑶」，绝不因为皮肤包缺失而放弃说话。"""
    try:
        from core import pet_skins

        skin = pet_skins.resolve_active(pet_skins.active_skin_id())
        name = str(skin.get("name") or "").strip()
        return name or "星瑶"
    except Exception:
        logger.debug("读取宠物形象名失败，回落星瑶", exc_info=True)
        return "星瑶"


def _build_prompt() -> str:
    """组装提示词：只带三项公开事实 + 最近台词，其余全靠约束把输出压成一句话。

    事实越少越安全（不读文件、不读对话），但也必须有锚点，否则模型会写出「今天又是
    美好的一天」这类和此刻、和这个目录毫无关系的空话。最近 8 条单独列成「别重复」，
    是因为同一段提示词连着请求很容易落回同一个句式。
    """
    now = _dt.datetime.now()
    try:
        folder = os.path.basename(os.getcwd()) or "工作目录"
    except Exception:
        folder = "工作目录"
    lines = [
        f"你是桌面宠物「{_skin_name()}」，此刻没人跟你说话，你小声嘟囔一句。",
        "",
        f"已知：现在是{now.strftime('%H:%M')}（{_time_phrase(now)}），你在的目录叫「{folder}」。",
        "",
        "要求：",
        "- 只输出一句话，8~20 个汉字；",
        "- 口语，像人在旁边随口一说；",
        "- 不带 emoji、不加引号、不换行；",
        "- 不要提问，也不要问对方在做什么；",
        "- 不要解释你在做什么，不要提提示词、模型这类字眼。",
    ]
    with _state_lock:
        recent = list(_recent)
    if recent:
        lines.append("")
        lines.append("下面这些是你说过的话，换一个新的说法，不要重复：")
        lines.extend(f"- {item}" for item in recent)
    return "\n".join(lines)


# ----------------------------------------------------------------- 生成与清洗
def _request_completion(model: dict, prompt: str, *, disable_thinking: bool) -> Optional[str]:
    """发一次非流式补全，返回正文字符串；请求本身失败返回 None。

    为什么要显式关思考：推理型模型（实测 hy3）会把 max_tokens 全花在 reasoning 上，
    正文直接是空串、finish_reason=length——连 800 token 都不够，因为思考有上千字。
    关掉后一句台词只要十来个 token 就够了。个别渠道不认 `thinking` 参数会直接报错，
    由调用方去掉参数重试（那种情况只能靠更大的预算让思考先跑完）。
    """
    try:
        from core.llm import _dispatch_completion

        kwargs: dict = {
            "model": model["litellm_model"],
            "api_key": model["api_key"],
            "api_base": model["base_url"],
            "messages": [{"role": "user", "content": prompt}],
            # temperature 由 native 传输层按模型能力自行决定是否透传，这里照常传
            "temperature": 0.9,
            "timeout": 20 if disable_thinking else 40,
            "max_tokens": 120 if disable_thinking else 800,
        }
        if disable_thinking:
            kwargs["thinking"] = {"type": "disabled"}
        response = _dispatch_completion(**kwargs)
        choices = getattr(response, "choices", None) or []
        return choices[0].message.content if choices else ""
    except Exception:
        # 超时、限流、密钥失效、参数不被接受都只记 debug：闲聊失败绝不能惊动用户。
        logger.debug("桌宠闲聊请求失败（disable_thinking=%s）", disable_thinking, exc_info=True)
        return None


def _generate_line() -> Optional[str]:
    """调模型拿一句台词；拿不到返回 None（本轮跳过，不重试、不打扰用户）。

    走 `core.llm._dispatch_completion`（与记忆压缩同一条轻量非流式入口）：闲聊不该
    进 Agent 的工具循环，否则一句话要牵出一整套回合状态，成本和风险都不对等。

    两次尝试的顺序有讲究：先关思考（快、便宜、正文稳定），拿不到正文才退回「开着
    思考 + 大预算」——后者更慢更贵，但能兼容不认 `thinking` 参数的渠道。
    """
    model = _resolve_model()
    if not model:
        logger.debug("没有可用模型，跳过本轮桌宠闲聊")
        return None
    prompt = _build_prompt()
    text = _request_completion(model, prompt, disable_thinking=True)
    if text is None or not str(text).strip():
        text = _request_completion(model, prompt, disable_thinking=False)
    if text is None:
        return None
    return _clean_line(text)


def _strip_wrapping_quotes(text: str) -> str:
    """剥掉包裹式引号；一层层剥，避免「“『…』”」这种套娃漏网。"""
    value = text
    while len(value) >= 2:
        for left, right in _QUOTE_PAIRS:
            if value.startswith(left) and value.endswith(right):
                value = value[1:-1].strip()
                break
        else:
            return value
    return value


def _clean_line(text: Any) -> Optional[str]:
    """把模型输出压成一行干净台词；空、无意义或与最近重复时返回 None。

    顺序是刻意的：先剔 thinking 标签（否则截断后只剩思考碎片）→ 取第一个非空行
    （模型常在标签后留空行）→ 去引号 → 截断 → 判重。判重放在最后，比的是「真正
    会冒泡的那一句」，避免同一条台词换个引号就绕过去重。
    """
    try:
        raw = str(text or "")
    except Exception:
        return None
    raw = _THINK_RE.sub("", raw)
    line = ""
    for segment in raw.splitlines():
        segment = segment.strip()
        if segment:
            line = segment
            break
    line = _strip_wrapping_quotes(line).strip()
    if not line:
        return None
    if len(line) > _MAX_CHARS:
        line = line[:_MAX_CHARS].strip()
    if not line:
        return None
    with _state_lock:
        if line in _recent:
            return None
    return line


# ---------------------------------------------------------------------- 触发
def _today_stamp() -> str:
    """本地日期字符串：跨天清零按「用户感知的今天」，不能用 UTC。"""
    return time.strftime("%Y-%m-%d")


def _refresh_day_locked() -> None:
    """跨天把当日计数清零（调用方须持 `_state_lock`）。"""
    global _today_count, _today_day
    stamp = _today_stamp()
    if stamp != _today_day:
        _today_day = stamp
        _today_count = 0


def _should_speak(now_idle: float) -> bool:
    """是否该在此刻开口：五条全满足才算。

    五条分别是：开关开、桌宠在跑、空闲 ≥ 阈值、已到「随机抽定的下一次时间」、
    当日未达 40 条；另外「星瑶没有任务在跑」一票否决。

    间隔为什么改成「抽一次、用完再抽」：原来的「阈值 × 0.8~1.2」是每个 tick 现算，
    等于每次重新掷骰子，说与不说取决于当天第几次骰子，既不好复现也不像活物——听感
    上仍是每十分钟一句的钟。现在说完就把下一次的时间点算好写进 `_next_speak_at`，
    判断只剩「到点没有」，而每次间隔本身服从长尾随机，节奏自然不齐。

    说明：「无任务在跑」这条目前只有近似信号（见 `_agent_busy`：撤销协调器的活跃
    回合登记 + 后台任务表），核心模块拿不到 ApiBridge 实例上那份权威的 `_active_turns`，
    所以未接入完整的回合状态；探测不到时按「没有回合」放行，只按系统空闲判断。
    """
    if not _setting_enabled():
        return False
    if not _pet_running():
        return False
    try:
        idle_value = float(now_idle)
    except (TypeError, ValueError):
        return False
    idle_seconds = _setting_idle_seconds()
    if idle_value < idle_seconds:
        return False
    with _state_lock:
        _refresh_day_locked()
        count = _today_count
        last = _last_spoken_at
        next_at = _next_speak_at
    if count >= _DAILY_LIMIT:
        return False
    # 说过话就等到抽定的那一刻；从没说过（last<=0）时到达阈值即可开一次头
    if last > 0 and time.time() < next_at:
        return False
    if _agent_busy():
        return False
    return True


# ---------------------------------------------------------------------- 说话
def _roll_next_gap(base_seconds: int) -> float:
    """抽定「下一次开口」要等多久（秒）。

    用三角分布而不是「固定值 × 小抖动」：抖动只在均值附近来回晃，听感还是一个钟。
    这里众数压在 0.7×阈值、尾巴拖到 2.6×阈值——多数时候七八分钟就冒一句，偶尔
    二十多分钟都不吭声；下限留 0.4×阈值，免得刚说完立刻又说，变成碎嘴子。
    """
    try:
        base = max(float(base_seconds), 1.0)
    except (TypeError, ValueError):
        base = float(_DEFAULT_IDLE_SECONDS)
    return base * random.triangular(0.4, 2.6, 0.7)


def _record_spoken(line: str) -> None:
    """记账：上次开口时间、当日条数、最近台词，并顺手抽定下一次开口的时间点。

    下次时间点在「说完这一刻」就定死，而不是等下一次 tick 现算：定死之后这一轮间隔
    就完全确定，`_should_speak` 只比时间戳，行为可解释、也能写单测；现算则等于每个
    tick 重新掷骰子，节奏不可复现，听感上仍是死板的固定周期。
    """
    global _last_spoken_at, _today_count, _next_speak_at
    with _state_lock:
        _refresh_day_locked()
        _last_spoken_at = time.time()
        _today_count += 1
        _recent.append(line)
        _next_speak_at = _last_spoken_at + _roll_next_gap(_setting_idle_seconds())


def _speak(line: str) -> bool:
    """把一句台词冒到桌宠上；推送成功才记账，避免「没显示也算说过了」。

    停留 3200ms 比状态气泡长一点：这句话没有上下文，一闪而过就白生成一次。
    """
    try:
        from core import pet_manager

        result = pet_manager.push_bubble(line, _BUBBLE_HOLD_MS)
    except Exception:
        logger.debug("推送桌宠气泡失败，跳过记账", exc_info=True)
        return False
    if not isinstance(result, dict) or not result.get("ok"):
        logger.debug("桌宠气泡未送达：%s", result)
        return False
    _record_spoken(line)
    return True


def _tick_once() -> Optional[str]:
    """调度线程的一拍：判断要不要开口，要就生成一句并推送；返回说出口的台词。

    抽成独立函数是为了能在测试/手工验证里「只跑一拍」，不必真的启动线程。
    """
    if not _should_speak(_system_idle_seconds()):
        return None
    line = _generate_line()
    if not line:
        return None
    return line if _speak(line) else None


def _loop() -> None:
    """调度主循环：每 `_TICK_SECONDS` 醒一次，任何异常都只记 debug 且绝不退出。

    宁可这一拍什么都不做，也不能让一条未预料的异常（皮肤包损坏、配置半写、模型
    返回怪结构）把线程弄死——线程死了表现就是「她再也不说话了」，而用户无从察觉。
    """
    while not _stop_event.wait(_TICK_SECONDS):
        try:
            _tick_once()
        except Exception:
            logger.debug("桌宠闲聊调度异常（已忽略，循环继续）", exc_info=True)


# ---------------------------------------------------------------------- 接口
def start() -> None:
    """幂等启动调度线程：重复调用（设置页点两次、应用重启后自动恢复）只保留一条。"""
    global _thread
    with _state_lock:
        if _thread is not None and _thread.is_alive():
            return
        _stop_event.clear()
        thread = threading.Thread(target=_loop, name="pet-chat", daemon=True)
        _thread = thread
    thread.start()
    logger.debug("桌宠闲聊调度线程已启动")


def stop() -> None:
    """停止调度线程（测试/退出用）：事件通知 + 短等待，不阻塞调用方太久。"""
    global _thread
    with _state_lock:
        thread = _thread
        _thread = None
    _stop_event.set()
    if thread is not None and thread.is_alive():
        thread.join(timeout=2.0)


def status() -> dict:
    """给设置页/诊断用的当前状态快照。

    `last_spoken_at` 用 epoch 秒（从没说过就是 None）：前端要显示「几分钟前」比显示
    格式化字符串更省事，也避开了时区口径不一致。
    """
    with _state_lock:
        _refresh_day_locked()
        thread = _thread
        return {
            "enabled": _setting_enabled(),
            "idle_seconds": _setting_idle_seconds(),
            "model_id": _setting_model_id(),
            "last_spoken_at": _last_spoken_at if _last_spoken_at > 0 else None,
            "today_count": int(_today_count),
            "thread_alive": bool(thread is not None and thread.is_alive()),
        }


def update_settings(
    enabled: Optional[bool] = None,
    idle_seconds: Optional[Any] = None,
    model_id: Optional[Any] = None,
) -> dict:
    """写设置并即时生效，返回最新 `status()`（附带 `saved` 表示是否写盘成功）。

    立即生效靠的是每次 tick 现读配置，而不是把值缓存在线程里：缓存会和「另一个
    窗口/另一台设备改了配置」打架，现读永远以磁盘为准。写盘失败也不抛异常——设置
    页只该提示一句「保存失败」，不该让按钮把界面卡死。
    """
    updates: dict = {}
    if enabled is not None:
        updates["pet_chat_enabled"] = bool(enabled)
    if idle_seconds is not None:
        try:
            value = int(idle_seconds)
        except (TypeError, ValueError):
            value = _DEFAULT_IDLE_SECONDS
        updates["pet_chat_idle_seconds"] = max(_IDLE_MIN_SECONDS, min(_IDLE_MAX_SECONDS, value))
    if model_id is not None:
        # 空串是合法值，语义是「自动」；不要把它当成「没传」
        updates["pet_chat_model_id"] = str(model_id or "").strip()

    saved = True
    if updates:
        try:
            from config import save_config_value

            for key, value in updates.items():
                if not save_config_value(key, value):
                    saved = False
        except Exception:
            logger.warning("保存桌宠闲聊设置失败", exc_info=True)
            saved = False
    result = status()
    result["saved"] = saved
    return result

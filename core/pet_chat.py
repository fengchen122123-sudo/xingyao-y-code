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
_DEFAULT_IDLE_SECONDS = 600
_DAILY_LIMIT = 40
# 台词长度上限（字符）：气泡宽度有限，超过一行半就换行溢出，宁可截断也不要糊成一团。
_MAX_CHARS = 40
_RECENT_KEEP = 8

_IDLE_MIN_SECONDS = 60
_IDLE_MAX_SECONDS = 86400

# 气泡停留时长：比普通状态气泡（2600ms）略长，因为这句话用户可能没盯着看。
_BUBBLE_HOLD_MS = 3200

# thinking 标签：非原生思考通道有时把推理过程混进正文，必须先剔掉再截断，
# 否则截断后剩下的全是思考碎片。
_THINK_RE = re.compile(r"<(?:think|thinking)>.*?</(?:think|thinking)>", re.DOTALL | re.IGNORECASE)

_QUOTE_PAIRS = (
    ('"', '"'),
    ("'", "'"),
    ("\u201c", "\u201d"),
    ("\u2018", "\u2019"),
    ("\u300c", "\u300d"),
    ("\u300e", "\u300f"),
)

_state_lock = threading.RLock()
_last_spoken_at: float = 0.0
_next_speak_at: float = 0.0
_today_count: int = 0
_today_day: str = ""
_recent: "collections.deque[str]" = collections.deque(maxlen=_RECENT_KEEP)
_thread: Optional[threading.Thread] = None
_stop_event = threading.Event()


def _config_value(key: str, default: Any = None) -> Any:
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
    try:
        return bool(_config_value("pet_chat_enabled", False))
    except Exception:
        return False


def _setting_idle_seconds() -> int:
    try:
        raw = int(_config_value("pet_chat_idle_seconds", _DEFAULT_IDLE_SECONDS))
    except Exception:
        raw = _DEFAULT_IDLE_SECONDS
    return max(_IDLE_MIN_SECONDS, min(_IDLE_MAX_SECONDS, raw))


def _setting_model_id() -> str:
    try:
        return str(_config_value("pet_chat_model_id", "") or "").strip()
    except Exception:
        return ""


class _LASTINPUTINFO(ctypes.Structure):
    """Win32 LASTINPUTINFO：cbSize 必须由调用方填成结构体大小，否则 API 直接失败。"""

    _fields_ = [
        ("cbSize", ctypes.c_uint),
        ("dwTime", ctypes.c_uint),
    ]


def _system_idle_seconds() -> float:
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


def _pet_running() -> bool:
    try:
        from core import pet_manager

        return bool(pet_manager.get_status().get("running"))
    except Exception:
        logger.debug("查询桌宠状态失败，按未运行处理", exc_info=True)
        return False


def _agent_busy() -> bool:
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


def _model_entry(provider: dict, model: dict) -> dict:
    from core.model_manager import effective_provider_api_key

    return {
        "litellm_model": str(model.get("litellm_model") or ""),
        "api_key": effective_provider_api_key(provider),
        "base_url": str(provider.get("base_url") or ""),
        "name": str(model.get("name") or model.get("id") or ""),
        "id": str(model.get("id") or ""),
    }


def _model_by_id(model_id: str) -> Optional[dict]:
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


def _time_phrase(now: _dt.datetime) -> str:
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


def _skin_profile() -> tuple[str, str]:
    try:
        from core import pet_skins

        skin = pet_skins.resolve_active(pet_skins.active_skin_id())
        name = str(skin.get("name") or "").strip() or "星瑶"
        persona = str(skin.get("persona") or skin.get("description") or "").strip()
        return name, persona
    except Exception:
        logger.debug("读取宠物形象信息失败，回落星瑶", exc_info=True)
        return "星瑶", ""


def _skin_name() -> str:
    """兼容旧调用的形象名读取入口。"""
    return _skin_profile()[0]


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
    skin_name, skin_persona = _skin_profile()
    lines = [
        f"你是桌面宠物「{skin_name}」，此刻没人跟你说话，你小声嘟囔一句。",
    ]
    if skin_persona:
        lines.append(f"性格与语气：{skin_persona}")
    lines.extend([
        "",
        f"已知：现在是{now.strftime('%H:%M')}（{_time_phrase(now)}），你在的目录叫「{folder}」。",
        "",
        "要求：",
        "- 只输出一句话，8~20 个汉字；",
        "- 口语，像人在旁边随口一说；",
        "- 不带 emoji、不加引号、不换行；",
        "- 不要提问，也不要问对方在做什么；",
        "- 不要解释你在做什么，不要提提示词、模型这类字眼。",
    ])
    with _state_lock:
        recent = list(_recent)
    if recent:
        lines.append("")
        lines.append("下面这些是你说过的话，换一个新的说法，不要重复：")
        lines.extend(f"- {item}" for item in recent)
    return "\n".join(lines)


def _request_completion(model: dict, prompt: str, *, disable_thinking: bool) -> Optional[str]:
    try:
        from core.llm import _dispatch_completion

        kwargs: dict = {
            "model": model["litellm_model"],
            "api_key": model["api_key"],
            "api_base": model["base_url"],
            "messages": [{"role": "user", "content": prompt}],
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


def _today_stamp() -> str:
    """本地日期字符串：跨天清零按「用户感知的今天」，不能用 UTC。"""
    return time.strftime("%Y-%m-%d")


def _refresh_day_locked() -> None:
    global _today_count, _today_day
    stamp = _today_stamp()
    if stamp != _today_day:
        _today_day = stamp
        _today_count = 0


def _should_speak(now_idle: float) -> bool:
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
    if last > 0 and time.time() < next_at:
        return False
    if _agent_busy():
        return False
    return True


def _roll_next_gap(base_seconds: int) -> float:
    try:
        base = max(float(base_seconds), 1.0)
    except (TypeError, ValueError):
        base = float(_DEFAULT_IDLE_SECONDS)
    return base * random.triangular(0.4, 2.6, 0.7)


def _record_spoken(line: str) -> None:
    global _last_spoken_at, _today_count, _next_speak_at
    with _state_lock:
        _refresh_day_locked()
        _last_spoken_at = time.time()
        _today_count += 1
        _recent.append(line)
        _next_speak_at = _last_spoken_at + _roll_next_gap(_setting_idle_seconds())


def _speak(line: str) -> bool:
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

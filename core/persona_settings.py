"""智能体人设设置持久化。

设置页「人设设置」分区的后端存储：智能体名称与智能体人设文本，
持久化到 ~/.ycode/persona.json（YCODE_DATA_DIR 优先）。

设计约束：
- 空人设文本表示"使用内置默认人设"，不视为错误；
- 读失败一律回落默认值，绝不影响启动与正常会话流程；
- 保存前做显式校验（长度上限），非法值拒绝且不写盘。
"""

from __future__ import annotations

import threading
from typing import Any

_STORE_LOCK = threading.Lock()

_VERSION = 1

# 名称与人设文本长度上限：防止误粘贴超大文本撑爆配置文件
MAX_NAME_LEN = 50
MAX_PERSONA_LEN = 20000

DEFAULT_AGENT_NAME = "星瑶"


class PersonaSettingsError(ValueError):
    """Raised when persona settings input fails validation."""


def _store_path():
    try:
        import config

        return config.YCODE_DIR / "persona.json" if hasattr(config.YCODE_DIR, "__truediv__") else config.YCODE_DIR + "/persona.json"
    except Exception:
        import os
        from pathlib import Path

        data_dir = os.environ.get("YCODE_DATA_DIR") or os.path.join(str(Path.home()), ".ycode")
        return os.path.join(data_dir, "persona.json")


def _defaults() -> dict[str, Any]:
    return {
        "version": _VERSION,
        "agent_name": DEFAULT_AGENT_NAME,
        "persona": "",
        "user_title": "",
    }


def get_persona_settings() -> dict[str, Any]:
    """读取人设设置快照；任何读取异常回落默认值。

    旧版本文件里可能残留的 gender/age 字段一律忽略（该功能已移除）。
    """
    try:
        import config

        data = config._read_json_with_backup(_store_path(), default=None)
    except Exception:
        data = None
    if not isinstance(data, dict):
        return _defaults()
    agent_name = data.get("agent_name")
    persona = data.get("persona")
    user_title = data.get("user_title")
    # 读取路径与保存路径同样做 strip：手改配置文件混入空白时不让
    # 界面显示与提示词判断（name != 默认名）出现两套口径。
    name_value = agent_name.strip() if isinstance(agent_name, str) and agent_name.strip() else DEFAULT_AGENT_NAME
    return {
        "version": _VERSION,
        "agent_name": name_value,
        "persona": persona if isinstance(persona, str) else "",
        "user_title": user_title.strip() if isinstance(user_title, str) else "",
    }


def save_persona_settings(agent_name: str, persona: str, user_title: str = "") -> dict[str, Any]:
    """校验并保存人设设置；返回保存后的完整快照。

    非法输入抛 PersonaSettingsError，调用方（bridge 层）转 ok=False 响应。
    user_title（智能体对用户的称呼）允许为空 = 不使用特定称呼。
    """
    if not isinstance(agent_name, str) or not agent_name.strip():
        raise PersonaSettingsError("智能体名称不能为空")
    name = agent_name.strip()
    if len(name) > MAX_NAME_LEN:
        raise PersonaSettingsError(f"智能体名称过长（最多 {MAX_NAME_LEN} 字符）")
    if not isinstance(persona, str):
        raise PersonaSettingsError("人设内容必须是文本")
    if len(persona) > MAX_PERSONA_LEN:
        raise PersonaSettingsError(f"人设内容过长（最多 {MAX_PERSONA_LEN} 字符）")
    if not isinstance(user_title, str):
        raise PersonaSettingsError("称呼必须是文本")
    title = user_title.strip()
    if len(title) > MAX_NAME_LEN:
        raise PersonaSettingsError(f"称呼过长（最多 {MAX_NAME_LEN} 字符）")

    with _STORE_LOCK:
        try:
            import config

            payload = {
                "version": _VERSION,
                "agent_name": name,
                "persona": persona,
                "user_title": title,
            }
            config._atomic_write_json(_store_path(), payload, indent=2)
        except PersonaSettingsError:
            raise
        except Exception as exc:  # pragma: no cover - 磁盘故障等极端情况
            raise PersonaSettingsError(f"保存失败：{exc}") from exc

    _invalidate_cache()
    return {"version": _VERSION, "agent_name": name, "persona": persona, "user_title": title}


# ---------------------------------------------------------------------------
# 高频读取缓存（GUI 状态文案 / 每轮请求组装都会调用）
# 缓存键为配置文件 mtime + 大小：外部手改文件后自动失效，保存路径主动失效。
# ---------------------------------------------------------------------------

_CACHE_LOCK = threading.Lock()
_CACHE_MTIME: float | None = None
_CACHE_SIZE: int | None = None
_CACHE_VALUE: dict[str, Any] | None = None


def _invalidate_cache() -> None:
    global _CACHE_MTIME, _CACHE_SIZE, _CACHE_VALUE
    with _CACHE_LOCK:
        _CACHE_MTIME = None
        _CACHE_SIZE = None
        _CACHE_VALUE = None


def _cached_snapshot() -> dict[str, Any]:
    global _CACHE_MTIME, _CACHE_SIZE, _CACHE_VALUE
    try:
        import os

        stat = os.stat(_store_path())
        mtime, size = stat.st_mtime, stat.st_size
    except OSError:
        mtime, size = None, None

    with _CACHE_LOCK:
        if _CACHE_VALUE is not None and _CACHE_MTIME == mtime and _CACHE_SIZE == size:
            return _CACHE_VALUE
    snapshot = get_persona_settings()
    with _CACHE_LOCK:
        _CACHE_MTIME, _CACHE_SIZE, _CACHE_VALUE = mtime, size, snapshot
    return snapshot


def get_agent_display_name() -> str:
    """返回当前智能体显示名（GUI 文案与提示词共用的事实源）。"""
    return str(_cached_snapshot().get("agent_name") or DEFAULT_AGENT_NAME)


def get_user_title() -> str:
    """返回智能体对用户的称呼；空串表示不使用特定称呼。"""
    return str(_cached_snapshot().get("user_title") or "")


def build_persona_prompt_block() -> str:
    """构建并入稳定系统前缀的人设块；默认配置（名称未改、无人设、无称呼）返回空串。

    返回空串可保证默认安装下系统提示词字节与旧版完全一致（byte-stability 回归不破）。
    自定义后注入：名称覆盖、对用户的称呼、自定义人设补充。
    """
    snapshot = _cached_snapshot()
    lines: list[str] = []
    name = str(snapshot.get("agent_name") or "").strip()
    title = str(snapshot.get("user_title") or "").strip()
    persona = str(snapshot.get("persona") or "").strip()
    if name and name != DEFAULT_AGENT_NAME:
        lines.append(
            f"名字覆盖：你的名字是「{name}」，不是默认名「{DEFAULT_AGENT_NAME}」。"
            "所有自我指涉（自我介绍、被问你是谁、执行过程说明等）一律使用这个名字。"
        )
    if title:
        lines.append(f"称呼约定：请用「{title}」称呼用户。")
    if persona:
        lines.append(
            "自定义人设取代（以下内容是用户为你设定的完整人设，取代内置默认人设中的全部"
            "身份事实与性格设定——年龄、属相、生日、来历、说话风格等个人设定一律不再沿用"
            "（名字由「智能体名称」设置单独控制）；被问及个人信息时只按自定义人设回答，"
            "自定义人设未提及的就如实说没有设定过，不得拿内置默认人设的设定来凑数。"
            "安全红线、诚实原则与工具使用规范不受影响）：\n" + persona
        )
    if not lines:
        return ""
    return "# 人设设置（来自设置页，用户自定义）\n\n" + "\n".join(lines)

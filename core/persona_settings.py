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
    """读取人设设置快照；任何读取异常回落默认值。"""
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
    return {
        "version": _VERSION,
        "agent_name": agent_name if isinstance(agent_name, str) and agent_name.strip() else DEFAULT_AGENT_NAME,
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

    return {"version": _VERSION, "agent_name": name, "persona": persona, "user_title": title}

"""外观与人设管理工具（theme_manage / persona_manage）。

职责边界：
- 主题相关全部委托给 core.gui_theme.ThemeRepository（Schema 校验、内置主题
  只读、ZIP 导入导出、资源读取都复用它的公开方法），本模块只做参数解析、
  统一 JSON 封装与错误中文转述，绝不自己实现校验规则；
- 人设读写复用 core.persona_settings（长度校验与落盘都在那一层）；
- 刻意不 import gui_bridge / main / pywebview：GUI 侧只需要"尽力而为"的
  通知，通过 core.app_context 探测已注册的上下文是否提供对应方法，
  CLI / headless 下探测不到就直接跳过。
"""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from core.persona_settings import (
    PersonaSettingsError,
    get_persona_settings,
    save_persona_settings,
)
from core.tool_protocol import ToolSpec


# 主题仓库延迟导入：core.gui_theme 会连带拉起 PIL/config（冷启动实测约 26ms），
# 而 CLI 下用户往往整局都不碰皮肤主题，没必要在启动期付这个成本。
def _theme_repo():
    from core.gui_theme import get_repository

    return get_repository()


def _theme_error_class():
    from core.gui_theme import ThemeError

    return ThemeError


# ---------------------------------------------------------------------------
# 统一 JSON 输出
# ---------------------------------------------------------------------------
def _dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _ok(**payload: Any) -> str:
    data: dict[str, Any] = {"ok": True}
    data.update(payload)
    return _dumps(data)


def _fail(error: Any, **extra: Any) -> str:
    data: dict[str, Any] = {"ok": False, "error": str(error)}
    data.update({key: value for key, value in extra.items() if value is not None})
    return _dumps(data)


def _fail_theme(exc: ThemeError) -> str:
    """把 ThemeError 的 code/path 一并回传给模型，便于自我修正参数。"""
    return _fail(exc.message or str(exc), code=exc.code, path=exc.path)


# ---------------------------------------------------------------------------
# 参数辅助
# ---------------------------------------------------------------------------
def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _payload_mapping(payload: Any) -> dict[str, Any]:
    """payload 允许传对象，也容忍模型把 JSON 序列化成字符串传进来。"""
    if payload is None:
        return {}
    if isinstance(payload, Mapping):
        return dict(payload)
    if isinstance(payload, str):
        if not payload.strip():
            return {}
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ValueError("payload 不是合法 JSON 文本") from exc
        if not isinstance(data, dict):
            raise ValueError("payload 必须是 JSON 对象")
        return data
    raise ValueError("payload 必须是对象")


def _theme_manifest(payload: Mapping[str, Any]) -> dict[str, Any]:
    """取一份可用于保存的主题清单。

    repository.get_theme() / save_theme() 返回的是"派生 DTO"，多带一个只读的
    ``builtin`` 标记；持久化 Schema 不含该字段（与 GUI 侧保存路径处理一致），
    这里只剥掉这一个派生字段，其余内容原样交给 Repository 校验。
    """
    return {key: value for key, value in payload.items() if key != "builtin"}


def _local_path(value: Any, label: str) -> Path:
    text = _text(value)
    if not text:
        raise ValueError(f"需要提供 {label}（本地文件路径）")
    return Path(text).expanduser()


# ---------------------------------------------------------------------------
# GUI 通知（best-effort）
# ---------------------------------------------------------------------------
def _notify_theme_activated(theme_id: str) -> dict[str, Any]:
    """激活主题后尽力通知已注册的 GUI 上下文重绘；失败不影响工具结果。

    用 getattr 探测方法而不是 import gui_bridge：CLI / headless 进程里根本
    没有这些方法，探测不到就静默跳过。
    """
    try:
        from core import app_context

        ctx = app_context.get()
    except Exception as exc:  # noqa: BLE001 - 通知失败绝不影响主结果
        return {"notified": False, "reason": f"读取运行时上下文失败：{exc}"}
    if ctx is None:
        return {"notified": False, "reason": "无 GUI 上下文（CLI/headless）"}
    for name in ("set_active_gui_theme", "refresh_gui_theme", "refresh_gui_themes"):
        method = getattr(ctx, name, None)
        if not callable(method):
            continue
        try:
            try:
                method(theme_id)
            except TypeError:
                # 某些刷新方法不接受参数（无参刷新）
                method()
        except Exception as exc:  # noqa: BLE001 - GUI 侧失败只记录
            return {
                "notified": False,
                "method": name,
                "reason": f"GUI 通知失败：{exc}",
            }
        return {"notified": True, "method": name}
    return {"notified": False, "reason": "GUI 上下文未提供主题刷新方法"}


# ---------------------------------------------------------------------------
# theme_manage 各动作
# ---------------------------------------------------------------------------
_THEME_ACTIONS = (
    "list", "get", "active", "activate", "save", "copy", "rename", "delete",
    "import", "export", "read_asset",
)


def _action_list() -> str:
    repository = _theme_repo()
    themes = repository.list_themes()
    active_id = None
    try:
        active_id = repository.get_active().get("id")
    except Exception:  # noqa: BLE001 - 活动主题读取失败不影响列表
        active_id = None
    return _ok(count=len(themes), themes=themes, active_id=active_id)


def _action_get(theme_id: str) -> str:
    if not theme_id:
        return _fail("需要提供 theme_id")
    return _ok(theme=_theme_repo().get_theme(theme_id))


def _action_active() -> str:
    theme = _theme_repo().get_active()
    return _ok(theme=theme, active_id=theme.get("id"))


def _action_activate(theme_id: str) -> str:
    if not theme_id:
        return _fail("需要提供 theme_id")
    repository = _theme_repo()
    theme = repository.set_active(theme_id)
    notification = _notify_theme_activated(theme_id)
    return _ok(
        theme=theme,
        active_id=theme.get("id"),
        notified=bool(notification.get("notified")),
        # 成功时报告走了哪个刷新方法，失败时报告原因（便于排查 GUI 侧问题）
        notify_detail=(
            notification.get("method")
            if notification.get("notified")
            else notification.get("reason")
        ),
    )


def _action_save(payload: Any) -> str:
    manifest = _theme_manifest(_payload_mapping(payload))
    if not manifest:
        return _fail("需要提供 payload（完整主题对象）")
    saved = _theme_repo().save_theme(manifest)
    return _ok(theme=saved, theme_id=saved.get("id"))


def _action_copy(theme_id: str, payload: Any) -> str:
    if not theme_id:
        return _fail("需要提供 theme_id（要复制的源主题）")
    data = _payload_mapping(payload)
    new_id = _text(data.get("id")) or _text(data.get("theme_id"))
    if not new_id:
        return _fail("需要在 payload 中提供新主题的 id")
    new_name = _text(data.get("name")) or new_id
    copied = _theme_repo().copy_theme(theme_id, new_id, new_name)
    return _ok(theme=copied, theme_id=copied.get("id"))


def _action_rename(theme_id: str, payload: Any) -> str:
    if not theme_id:
        return _fail("需要提供 theme_id（要重命名的主题）")
    data = _payload_mapping(payload)
    new_id = _text(data.get("id")) or _text(data.get("theme_id")) or theme_id
    new_name = _text(data.get("name"))
    if not new_name:
        return _fail("需要在 payload 中提供新的主题名称 name")
    renamed = _theme_repo().rename_theme(theme_id, new_id, new_name)
    return _ok(theme=renamed, theme_id=renamed.get("id"))


def _action_delete(theme_id: str) -> str:
    if not theme_id:
        return _fail("需要提供 theme_id")
    _theme_repo().delete_theme(theme_id)
    return _ok(deleted=theme_id)


def _action_import(source_path: Any) -> str:
    source = _local_path(source_path, "source_path")
    if not source.is_file():
        return _fail(f"主题归档文件不存在：{source}")
    imported = _theme_repo().import_zip(source)
    return _ok(theme=imported, theme_id=imported.get("id"))


def _action_export(theme_id: str, output_path: Any) -> str:
    if not theme_id:
        return _fail("需要提供 theme_id")
    destination = _local_path(output_path, "output_path")
    if destination.suffix.lower() != ".zip":
        destination = destination.with_suffix(".zip")
    exported = _theme_repo().export_zip(theme_id, destination)
    return _ok(theme_id=theme_id, path=str(exported))


def _action_read_asset(theme_id: str, asset_path: str) -> str:
    if not theme_id:
        return _fail("需要提供 theme_id")
    if not asset_path:
        return _fail("需要提供 asset_path（主题内的资源相对路径，例如 assets/xxx.png）")
    mime_type, data = _theme_repo().read_asset(theme_id, asset_path)
    return _ok(
        theme_id=theme_id,
        asset_path=asset_path,
        mime=mime_type,
        size=len(data),
        base64=base64.b64encode(data).decode("ascii"),
    )


def theme_manage(
    action: str,
    theme_id: str = "",
    payload: Any = None,
    source_path: str = "",
    output_path: str = "",
    asset_path: str = "",
) -> str:
    """GUI 主题管理统一入口，返回 JSON 字符串。"""
    try:
        action = _text(action)
        theme_id = _text(theme_id)
        asset_path = _text(asset_path)
        if not action:
            return _fail("action 不能为空，可选：" + "/".join(_THEME_ACTIONS))
        if action == "list":
            return _action_list()
        if action == "get":
            return _action_get(theme_id)
        if action == "active":
            return _action_active()
        if action == "activate":
            return _action_activate(theme_id)
        if action == "save":
            return _action_save(payload)
        if action == "copy":
            return _action_copy(theme_id, payload)
        if action == "rename":
            return _action_rename(theme_id, payload)
        if action == "delete":
            return _action_delete(theme_id)
        if action == "import":
            return _action_import(source_path)
        if action == "export":
            return _action_export(theme_id, output_path)
        if action == "read_asset":
            return _action_read_asset(theme_id, asset_path)
        return _fail(f"不支持的 action: {action}，可选：" + "/".join(_THEME_ACTIONS))
    except _theme_error_class() as exc:
        # 内置主题只读、删除活动主题、ZIP 不安全等都在这一层报出
        return _fail_theme(exc)
    except ValueError as exc:
        return _fail(str(exc) or "参数校验失败")
    except OSError as exc:
        return _fail(f"文件读写失败：{exc}")
    except Exception as exc:  # noqa: BLE001 - 工具边界必须吞掉全部异常
        return _fail(f"theme_manage 执行失败：{exc}")


# ---------------------------------------------------------------------------
# persona_manage
# ---------------------------------------------------------------------------
_PERSONA_ACTIONS = ("get", "set")


def persona_manage(
    action: str,
    agent_name: str = "",
    persona: str = "",
    user_title: str = "",
) -> str:
    """智能体人设（名称 / 自定义人设 / 对用户称呼）读写，返回 JSON 字符串。"""
    try:
        action = _text(action)
        if not action:
            return _fail("action 不能为空，可选：" + "/".join(_PERSONA_ACTIONS))
        if action == "get":
            return _ok(**get_persona_settings())
        if action == "set":
            if not _text(agent_name):
                return _fail("set 需要提供 agent_name")
            snapshot = save_persona_settings(
                _text(agent_name),
                persona if isinstance(persona, str) else "",
                user_title if isinstance(user_title, str) else "",
            )
            return _ok(**snapshot)
        return _fail(f"不支持的 action: {action}，可选：" + "/".join(_PERSONA_ACTIONS))
    except PersonaSettingsError as exc:
        return _fail(str(exc) or "人设设置校验失败")
    except Exception as exc:  # noqa: BLE001 - 工具边界必须吞掉全部异常
        return _fail(f"persona_manage 执行失败：{exc}")


# ---------------------------------------------------------------------------
# ToolSpec 装配
# ---------------------------------------------------------------------------
_THEME_ARGUMENTS = frozenset({
    "action", "theme_id", "payload", "source_path", "output_path", "asset_path",
})
_PERSONA_ARGUMENTS = frozenset({"action", "agent_name", "persona", "user_title"})


def _execute_theme(**arguments: Any) -> str:
    clean = {key: value for key, value in arguments.items() if key in _THEME_ARGUMENTS}
    return theme_manage(**clean)


def _execute_persona(**arguments: Any) -> str:
    clean = {key: value for key, value in arguments.items() if key in _PERSONA_ARGUMENTS}
    return persona_manage(**clean)


_THEME_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": list(_THEME_ACTIONS),
            "description": (
                "操作类型：list 列出全部主题（含内置）；get 读取指定主题完整内容；"
                "active 读取当前活动主题；activate 切换并启用主题；save 校验并保存"
                "完整主题对象；copy 复制为主题（新 id/name 从 payload 取）；"
                "rename 重命名主题（新 id/name 从 payload 取）；delete 删除主题"
                "（不能删除活动主题）；import 从本地 ZIP 导入；export 导出为本地 ZIP；"
                "read_asset 读取主题内图片资源（返回 base64）"
            ),
        },
        "theme_id": {
            "type": "string",
            "description": "主题 ID（如 builtin.dark 或自定义 id，只能含小写字母/数字/连字符）。"
                           "get/activate/copy/rename/delete/export/read_asset 必填；"
                           "copy 时表示源主题，rename 时可省略（仅改名称）",
        },
        "payload": {
            "type": "object",
            "description": (
                "JSON 对象。save 时为完整主题对象（schema_version/id/name/appearance/"
                "extends/tokens/native_titlebar/app_image/regions，extends 必须是内置主题 id）；"
                "copy 时提供新主题的 id 与 name；rename 时提供新 name（可选新 id）"
            ),
            "additionalProperties": True,
        },
        "source_path": {
            "type": "string",
            "description": "import 使用的本地 ZIP 文件路径（直接传路径，不使用文件选择框）",
        },
        "output_path": {
            "type": "string",
            "description": "export 使用的本地 ZIP 输出路径（自动补 .zip 后缀）",
        },
        "asset_path": {
            "type": "string",
            "description": "read_asset 使用的主题内资源相对路径，形如 assets/xxxx.png",
        },
    },
    "required": ["action"],
}


_PERSONA_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": list(_PERSONA_ACTIONS),
            "description": "操作类型：get 读取当前人设设置；set 保存人设设置",
        },
        "agent_name": {
            "type": "string",
            "description": "智能体名称，set 时必填（最多 50 字符）",
        },
        "persona": {
            "type": "string",
            "description": "自定义人设正文，可空（空表示使用内置默认人设，最多 20000 字符）",
        },
        "user_title": {
            "type": "string",
            "description": "智能体对你的称呼，可空（空表示不使用特定称呼，最多 50 字符）",
        },
    },
    "required": ["action"],
}


def build_specs() -> list[ToolSpec]:
    """返回 theme_manage / persona_manage 两个工具契约。"""
    return [
        ToolSpec(
            name="theme_manage",
            description=(
                "管理与查询界面主题（GUI 外观皮肤）。支持：列出/读取主题、查询当前活动主题、"
                "切换启用主题、保存完整主题对象、复制/重命名/删除主题、从本地 ZIP 导入、"
                "导出为本地 ZIP、读取主题内图片资源（base64）。内置主题只能启用或复制，"
                "不能编辑或删除；删除活动主题会被拒绝。导入导出使用传入的本地路径，"
                "不弹出文件选择框。"
            ),
            input_schema=_THEME_SCHEMA,
            execute=_execute_theme,
            read_only=False,
            requires_permission=True,
            side_effect_scope="system",
            activity_description="管理界面主题",
        ),
        ToolSpec(
            name="persona_manage",
            description=(
                "读取或保存智能体人设设置：智能体名称、自定义人设正文、对你的称呼。"
                "改动会写入本地人设配置，并在后续对话的系统提示词中生效。"
            ),
            input_schema=_PERSONA_SCHEMA,
            execute=_execute_persona,
            read_only=False,
            requires_permission=True,
            side_effect_scope="system",
            activity_description="管理智能体人设",
        ),
    ]

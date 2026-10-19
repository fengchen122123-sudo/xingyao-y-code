"""管理类 Agent 工具：技能 / 插件 / Hook。

设计约束（务必保持）：
- **只做"能力复用"**：本模块不实现任何业务逻辑，全部转调既有 core 服务
  （``core.skills`` / ``core.plugin_loader`` / ``core.hooks.trust`` /
  ``core.hooks.config_mutation``），避免 GUI、CLI 与本工具三份实现分叉。
- **不暴露信任/授权动作**：插件信任（``plugin_loader.trust_plugin``）与 hook
  信任（``hooks.trust.apply_hook_trust_action``）是"人类确认"语义的操作，模型
  无权代劳，因此本模块既没有也不允许新增 trust/grant/deny/revoke 类 action。
- **统一返回 JSON 字符串**（``ensure_ascii=False``）：成功 ``{"ok": true, ...}``，
  失败 ``{"ok": false, "error": "中文原因"}``；所有异常在 execute 内兜底捕获，
  绝不让异常穿透到调度层。
- 依赖边界：禁止 import ``gui_bridge`` / ``main`` / ``pywebview``（GUI 层在
  GUI 进程里不可用，且会把工具注册拖进 UI 依赖）。core 模块一律函数内惰性
  导入：本模块被工具注册表 import 时不应触发插件扫描、hook 读盘等副作用。
"""

import json
import os

from core.tool_protocol import ToolSpec


# ── 统一返回与参数校验小工具 ──────────────────────────────────


def _ok(**payload):
    """成功结果：``{"ok": true, ...}``。"""
    data = {"ok": True}
    data.update(payload)
    return json.dumps(data, ensure_ascii=False)


def _err(message):
    """失败结果：``{"ok": false, "error": <中文原因>}``。"""
    return json.dumps({"ok": False, "error": str(message or "未知错误")}, ensure_ascii=False)


def _workspace_root():
    """当前线程绑定的工作目录（项目级 hook 配置的定位基准）。

    用 ``workspace_context`` 而不是 ``os.getcwd()``：多会话并行时各会话工作目录
    可能不同，项目级 hooks.json 必须落在发起调用的那个工作区。
    """
    try:
        from core.workspace_context import get_workdir

        return str(get_workdir())
    except Exception:
        return os.getcwd()


def _require_str(value, field_name):
    """返回 ``(文本, "")`` 或 ``(None, 中文原因)``。"""
    if not isinstance(value, str) or not value.strip():
        return None, f"缺少必填参数 {field_name}"
    return value.strip(), ""


# ── 技能管理 ────────────────────────────────────────────────


def _skill_view(skill, active_ids, released_builtin):
    """把 ``scan_all_skills`` 的原始记录压成工具输出用的精简视图。

    ``scan_all_skills`` 的每项含完整正文（content/body），直接回传会白烧上下文，
    模型只需要 id / 名称 / 描述 / 启停状态 / 来源。
    """
    skill_id = str(skill.get("id") or "")
    # 技能以 SKILL.md 绝对路径为 id（见 parse_skill_md），目录名才是"短名"
    short_id = os.path.basename(os.path.dirname(skill_id)) if skill_id else ""
    return {
        "id": skill_id,
        "short_id": short_id,
        "name": str(skill.get("name") or ""),
        "description": str(skill.get("description") or ""),
        "enabled": skill_id in active_ids,
        "source": str(skill.get("source") or "user"),
        # 是否曾由内置技能释放过（True 表示可用 restore 恢复内置版本）
        "from_builtin": bool(short_id and short_id in released_builtin),
    }


def _skill_snapshot():
    """扫描技能并拼装 ``{skills, active_ids}`` 快照（供各 action 复用）。"""
    from core.skills import (
        get_active_skill_ids,
        get_released_builtin_names,
        scan_all_skills,
    )

    active_ids = set(get_active_skill_ids() or [])
    try:
        released_builtin = get_released_builtin_names()
    except Exception:
        # 保守降级：名单读不到只影响 from_builtin 展示字段，不能让整个列表失败
        released_builtin = set()
    skills = [_skill_view(s, active_ids, released_builtin) for s in (scan_all_skills() or [])]
    return skills, sorted(active_ids)


def skill_manage(action, skill_id="", reload_only=False):
    """技能管理：list / enable / disable / refresh / restore。

    - ``refresh`` 与 ``reload_only=True`` 都是"重新扫描"，不做任何写操作；
      ``reload_only=True`` 时 enable/disable 降级为只读预演（只回传当前状态），
      便于模型先确认目标技能再落盘。
    """
    try:
        from core.skills import (
            get_active_skill_ids,
            restore_builtin_skill,
            save_active_skill_ids,
        )

        action = str(action or "").strip().lower()

        if action == "list":
            skills, active_ids = _skill_snapshot()
            return _ok(action="list", skills=skills, active_ids=active_ids,
                       count=len(skills), enabled_count=len(active_ids))

        if action == "refresh":
            skills, active_ids = _skill_snapshot()
            return _ok(action="refresh", skills=skills, active_ids=active_ids,
                       count=len(skills), enabled_count=len(active_ids))

        if action in ("enable", "disable"):
            target_id, error = _require_str(skill_id, "skill_id")
            if target_id is None:
                return _err(error)
            skills, _ = _skill_snapshot()
            known = {s["id"]: s for s in skills}
            if target_id not in known:
                return _err(
                    f"未找到技能 {target_id}（skill_id 必须是列表返回的 SKILL.md 绝对路径，"
                    "可先执行 action=list 获取）"
                )
            want_enabled = action == "enable"
            active_ids = list(get_active_skill_ids() or [])
            if reload_only:
                # 只读预演：不写回激活名单，仅报告"将会变成什么状态"
                currently_enabled = target_id in active_ids
                return _ok(
                    action=action,
                    reload_only=True,
                    applied=False,
                    skill_id=target_id,
                    name=known[target_id]["name"],
                    was_enabled=currently_enabled,
                    would_change=currently_enabled != want_enabled,
                    active_ids=active_ids,
                )
            if want_enabled:
                if target_id not in active_ids:
                    active_ids.append(target_id)
            else:
                active_ids = [i for i in active_ids if i != target_id]
            # save_active_skill_ids 内部是"临时文件 + os.replace"原子写，失败只记日志
            save_active_skill_ids(active_ids)
            stored = list(get_active_skill_ids() or [])
            if (target_id in stored) != want_enabled:
                return _err("写入激活技能名单后校验失败，请检查 active_skills.json 是否可写")
            skills, active_ids = _skill_snapshot()
            return _ok(action=action, applied=True, skill_id=target_id,
                       name=known[target_id]["name"], skills=skills,
                       active_ids=active_ids, enabled_count=len(active_ids))

        if action == "restore":
            target_id, error = _require_str(skill_id, "skill_id")
            if target_id is None:
                return _err(error)
            # restore_builtin_skill 以"技能目录名"为唯一标识，需从 SKILL.md 路径反推
            name = os.path.basename(os.path.dirname(target_id))
            if not name:
                return _err("技能标识无效，无法定位技能目录")
            result = restore_builtin_skill(name) or {}
            if not result.get("ok"):
                return _err(str(result.get("msg") or "恢复内置版本失败"))
            skills, active_ids = _skill_snapshot()
            return _ok(action="restore", applied=True, skill_id=target_id,
                       name=name, message=str(result.get("msg") or "已恢复内置版本"),
                       skills=skills, active_ids=active_ids)

        return _err(f"未知 action: {action}（仅支持 list/enable/disable/refresh/restore）")
    except Exception as exc:  # 工具边界兜底：异常一律转成错误 JSON
        return _err(f"技能管理失败：{exc}")


# ── 插件管理 ────────────────────────────────────────────────


def _plugin_view(info):
    """插件信息精简视图（``get_plugin_infos`` 已返回 dict 副本，可直接挑字段）。"""
    return {
        "name": str(info.get("name") or ""),
        "version": str(info.get("version") or ""),
        "description": str(info.get("description") or ""),
        "path": str(info.get("path") or ""),
        "source": str(info.get("source") or ""),
        # loaded / disabled / failed / overridden / untrusted
        "status": str(info.get("status") or ""),
        "enabled": str(info.get("status") or "") in ("loaded", "failed"),
        "has_settings": bool(info.get("has_settings")),
        "tools": list(info.get("tools") or []),
        "error": str(info.get("error") or ""),
    }


def plugin_manage(action, name="", setting="", value=None):
    """插件管理：list / enable / disable / settings_get / settings_action。

    设置读写沿用既有插件钩子约定 ``plugin_settings``（见 gui_bridge 同款调用）：
    - 读取快照：``call_plugin_hook(name, "plugin_settings", {"action": "render"})``
    - 执行动作：``call_plugin_hook(name, "plugin_settings",
      {"action": <setting>, "params": <value>})``
    两种调用都只对"已加载（= 已信任 + 已启用）"的插件生效，未加载时 core 返回
    ``needs``（trust / enable）指引，本工具原样透出——但不提供代替用户信任的动作。
    """
    try:
        from core.plugin_loader import (
            call_plugin_hook,
            get_plugin_infos,
            refresh_plugins,
            toggle_plugin,
        )

        action = str(action or "").strip().lower()

        if action == "list":
            # 增量刷新：发现运行期新装/已删除的插件。未信任的插件不会被加载执行
            # （refresh_plugins 内部先做信任确认），因此这一步不会执行陌生插件代码。
            refresh_plugins()
            plugins = [_plugin_view(i) for i in (get_plugin_infos() or [])]
            return _ok(action="list", plugins=plugins, count=len(plugins),
                       loaded=[p["name"] for p in plugins if p["status"] == "loaded"])

        if action in ("enable", "disable"):
            plugin_name, error = _require_str(name, "name")
            if plugin_name is None:
                return _err(error)
            ok, message = toggle_plugin(plugin_name, action == "enable")
            plugins = [_plugin_view(i) for i in (get_plugin_infos() or [])]
            if not ok:
                message = str(message or "")
                if "未通过信任确认" in message or "未获信任" in message:
                    # 明确告知模型：信任必须由用户在交互终端完成，工具无权代办
                    return _err(
                        f"{message}。插件信任需由用户在交互终端确认，工具无法代替信任。"
                    )
                return _err(message or f"{action} 插件 {plugin_name} 失败")
            return _ok(action=action, applied=True, plugin=plugin_name,
                       message=str(message or ""), plugins=plugins)

        if action == "settings_get":
            plugin_name, error = _require_str(name, "name")
            if plugin_name is None:
                return _err(error)
            result = call_plugin_hook(plugin_name, "plugin_settings", {"action": "render"})
            if not result.get("ok"):
                return _err(str(result.get("message") or "插件设置页不可用"))
            data = result.get("data") or {}
            page = data.get("page") if isinstance(data, dict) else None
            if not isinstance(page, dict):
                return _err("插件未返回有效的设置页内容")
            return _ok(action="settings_get", plugin=plugin_name, page=page)

        if action == "settings_action":
            plugin_name, error = _require_str(name, "name")
            if plugin_name is None:
                return _err(error)
            setting_name, error = _require_str(setting, "setting")
            if setting_name is None:
                return _err(error)
            # 约定参数是 dict（插件按 params.get(...) 取值）；非 dict 的标量包一层，
            # 避免插件收到非法类型而整体报错
            if value is None:
                params = {}
            elif isinstance(value, dict):
                params = value
            else:
                params = {"value": value}
            result = call_plugin_hook(
                plugin_name, "plugin_settings", {"action": setting_name, "params": params}
            )
            if not result.get("ok"):
                return _err(str(result.get("message") or "插件动作不可用"))
            data = result.get("data") or {}
            if not isinstance(data, dict):
                return _err("插件动作返回值无效（必须是 dict）")
            if not bool(data.get("ok", True)):
                return _err(str(data.get("message") or f"插件动作 {setting_name} 执行失败"))
            return _ok(
                action="settings_action",
                plugin=plugin_name,
                setting=setting_name,
                message=str(data.get("message") or ""),
                page=data.get("page"),
                auth_url=data.get("auth_url"),
                poll=data.get("poll"),
                done=data.get("done"),
            )

        return _err(
            f"未知 action: {action}（仅支持 list/enable/disable/settings_get/settings_action；"
            "插件信任不在工具能力范围内）"
        )
    except Exception as exc:
        return _err(f"插件管理失败：{exc}")


# ── Hook 管理 ───────────────────────────────────────────────


def _hook_snapshot(workspace_root):
    """读取两级 hook 配置 + 信任状态（只读视图，不执行任何 hook）。"""
    from core.hooks.trust import list_hooks_with_status

    return list_hooks_with_status(workspace_root=workspace_root) or []


def hook_manage(action, source_path="", digest="", payload=None, disabled=False):
    """Hook 管理：list / save / delete / set_disabled。

    信任/授权（grant / deny / revoke / reset / trust）**故意不实现**：hook 信任是
    用户对本机命令执行的授权，必须由人在 CLI/GUI 里确认。
    """
    try:
        action = str(action or "").strip().lower()
        workspace_root = _workspace_root()

        if action == "list":
            hooks = _hook_snapshot(workspace_root)
            return _ok(action="list", workspace_root=workspace_root,
                       hooks=hooks, count=len(hooks))

        if action == "save":
            from core.hooks.config_mutation import save_hook_config

            if payload is None:
                return _err("缺少必填参数 payload（hook 配置对象）")
            if not isinstance(payload, dict):
                return _err("payload 必须是对象（JSON object）")
            data = dict(payload)
            # 供模型少写一个字段：外层 source_path 可直接作为编辑定位依据。
            # 不覆盖 payload 自带值，避免与显式传入的定位信息冲突。
            if source_path and not data.get("source_path"):
                data["source_path"] = str(source_path)
            ok, message = save_hook_config(data, workspace_root=workspace_root)
            if not ok:
                return _err(str(message or "保存 hook 配置失败"))
            hooks = _hook_snapshot(workspace_root)
            return _ok(action="save", applied=True, message=str(message or "已保存"),
                       workspace_root=workspace_root, hooks=hooks, count=len(hooks))

        if action == "delete":
            from core.hooks.config_mutation import delete_hook_config

            source, error = _require_str(source_path, "source_path")
            if source is None:
                return _err(error)
            digest_text, error = _require_str(digest, "digest")
            if digest_text is None:
                return _err(error)
            ok, message = delete_hook_config(
                source, digest_text, workspace_root=workspace_root
            )
            if not ok:
                return _err(str(message or "删除 hook 配置失败"))
            hooks = _hook_snapshot(workspace_root)
            return _ok(action="delete", applied=True, message=str(message or "已删除"),
                       workspace_root=workspace_root, hooks=hooks, count=len(hooks))

        if action == "set_disabled":
            from core.hooks.config_mutation import set_hook_disabled

            source, error = _require_str(source_path, "source_path")
            if source is None:
                return _err(error)
            digest_text, error = _require_str(digest, "digest")
            if digest_text is None:
                return _err(error)
            # 拒绝 "true"/1 这类非布尔值：把"想启用"误判成停用是危险方向
            if not isinstance(disabled, bool):
                return _err("disabled 必须是布尔值（true 停用 / false 启用）")
            ok, message = set_hook_disabled(
                source, digest_text, disabled, workspace_root=workspace_root
            )
            if not ok:
                return _err(str(message or "更新 hook 状态失败"))
            hooks = _hook_snapshot(workspace_root)
            return _ok(action="set_disabled", applied=True, disabled=disabled,
                       message=str(message or ""), workspace_root=workspace_root,
                       hooks=hooks, count=len(hooks))

        if action in ("grant", "deny", "revoke", "reset", "trust"):
            return _err(
                "hook 信任/授权动作不在工具能力范围内：必须由用户在 CLI/GUI 中确认后执行"
            )
        return _err(
            f"未知 action: {action}（仅支持 list/save/delete/set_disabled；"
            "信任相关动作不在工具能力范围内）"
        )
    except Exception as exc:
        return _err(f"Hook 管理失败：{exc}")


# ── 工具规格 ────────────────────────────────────────────────


def _spec(name, description, properties, execute, activity_description):
    """构造 ToolSpec：管理类工具都会落盘/改系统状态，故统一非只读 + 需授权 + system 范围。"""
    return ToolSpec(
        name=name,
        description=description,
        input_schema={
            "type": "object",
            "properties": properties,
            "required": ["action"],
            "additionalProperties": False,
        },
        execute=execute,
        read_only=False,
        requires_permission=True,
        side_effect_scope="system",
        activity_description=activity_description,
    )


def build_specs():
    """返回三个管理工具的 ToolSpec 列表（skill / plugin / hook）。"""
    skill_action = {
        "type": "string",
        "enum": ["list", "enable", "disable", "refresh", "restore"],
        "description": (
            "list=列出全部技能及启停状态；enable/disable=启用或停用指定技能（写回激活名单）；"
            "refresh=重新扫描技能目录；restore=把指定技能恢复为随包内置版本"
        ),
    }
    plugin_action = {
        "type": "string",
        "enum": ["list", "enable", "disable", "settings_get", "settings_action"],
        "description": (
            "list=列出插件及加载状态（先增量刷新扫描）；enable/disable=启用或停用插件"
            "（未信任的插件无法通过工具启用，需用户在交互终端确认信任）；"
            "settings_get=读取插件设置页快照；settings_action=执行插件设置页上的一个动作"
        ),
    }
    hook_action = {
        "type": "string",
        "enum": ["list", "save", "delete", "set_disabled"],
        "description": (
            "list=列出用户级与项目级 hook 及信任状态；save=新增或编辑一条 hook 配置"
            "（新增需 payload.scope=user|project，编辑用 source_path+digest 定位）；"
            "delete=按 source_path+digest 删除；set_disabled=按 source_path+digest 停用/启用。"
            "hook 信任/授权动作不可用（需用户自行确认）"
        ),
    }

    return [
        _spec(
            name="skill_manage",
            description=(
                "管理本机技能（SKILL.md）。action=list 列出全部技能与启停状态；"
                "enable/disable 通过改写激活技能名单启用或停用某个技能；"
                "refresh 重新扫描技能目录（技能文件被新增/修改后用）；"
                "restore 把某个内置技能覆盖回随包发布的版本（会丢弃该技能的自定义修改）。"
                "需要 skill_id（SKILL.md 的绝对路径，取自 list 返回的 id）"
            ),
            properties={
                "action": skill_action,
                "skill_id": {
                    "type": "string",
                    "description": "技能标识：SKILL.md 的绝对路径（list 返回的 id）；enable/disable/restore 必填",
                },
                "reload_only": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "true 时只重新扫描并报告状态，不做任何写入：enable/disable 降级为"
                        "只读预演（返回 was_enabled / would_change），适合先确认再落盘"
                    ),
                },
            },
            execute=skill_manage,
            activity_description="管理技能",
        ),
        _spec(
            name="plugin_manage",
            description=(
                "管理本机插件。action=list 列出插件、加载状态与是否提供设置页；"
                "enable/disable 启用或停用插件（未信任的插件会被拒绝，信任需用户本人在交互终端确认，"
                "本工具无信任能力）；settings_get 读取插件设置页快照（调用插件 plugin_settings 钩子）；"
                "settings_action 用 setting 指定插件设置页动作名、value 传参数（对象）执行该动作。"
                "设置相关动作仅对已信任且已启用的插件有效"
            ),
            properties={
                "action": plugin_action,
                "name": {"type": "string", "description": "插件名（list 返回的 name）"},
                "setting": {
                    "type": "string",
                    "description": "settings_action 的插件设置页动作名（由插件在设置页里声明，如 save / login）",
                },
                "value": {
                    "type": "object",
                    "description": "settings_action 的参数对象（作为 params 传给插件）；不传视为空对象",
                    "additionalProperties": True,
                },
            },
            execute=plugin_manage,
            activity_description="管理插件",
        ),
        _spec(
            name="hook_manage",
            description=(
                "管理 Hook（生命周期钩子）配置。action=list 列出用户级与项目级 hook 及其信任状态；"
                "save 新增或编辑一条配置（新增用 payload 里的 scope=user|project；"
                "编辑用 source_path+digest 定位，payload 含 event/command/matcher/timeout_ms/status_message）；"
                "delete 按 source_path+digest 删除；set_disabled 按 source_path+digest 停用或启用。"
                "source_path 与 digest 均取自 list 返回项。信任/授权动作不提供：hook 由模型写入后"
                "仍需用户自行信任才会执行"
            ),
            properties={
                "action": hook_action,
                "source_path": {
                    "type": "string",
                    "description": "hook 来源配置文件路径（list 返回的 source_path，仅支持用户级/项目级 hooks.json）",
                },
                "digest": {
                    "type": "string",
                    "description": "hook 内容指纹（list 返回的 digest），delete/set_disabled 必填，save 编辑时用于定位",
                },
                "payload": {
                    "type": "object",
                    "description": (
                        "save 的配置对象：新增 {scope, event, matcher, command, timeout_ms, status_message}；"
                        "编辑 {event, matcher, command, timeout_ms, status_message}"
                    ),
                    "additionalProperties": True,
                },
                "disabled": {
                    "type": "boolean",
                    "default": False,
                    "description": "set_disabled 的目标状态：true=停用，false=启用",
                },
            },
            execute=hook_manage,
            activity_description="管理 Hook",
        ),
    ]

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from core.hooks.registry import default_project_hooks_path, default_user_hooks_path
from core.hooks.trust import compute_hook_digest
from core.hooks.types import DEFAULT_TIMEOUT_MS, MAX_TIMEOUT_MS, HookEvent

_logger = logging.getLogger("ycode.hooks")

_OWNED_KEYS = frozenset({"command", "matcher", "timeout_ms", "status_message", "disabled"})

_EVENT_VALUES = frozenset(event.value for event in HookEvent)

_MISSING = object()

_NOT_FOUND_MESSAGE = "未找到对应的 hook（配置可能已变更，请刷新后重试）"


def _resolve_user_path(user_hooks_path) -> Path:
    """用户级配置路径：显式注入优先，否则走 ``~/.ycode/hooks.json`` 口径。"""
    return Path(user_hooks_path) if user_hooks_path else default_user_hooks_path()


def _resolve_project_path(workspace_root, project_hooks_path) -> Path | None:
    if project_hooks_path:
        return Path(project_hooks_path)
    if workspace_root:
        return default_project_hooks_path(workspace_root)
    return None


def _resolve_source_path(
    source_path, workspace_root, user_hooks_path, project_hooks_path
) -> tuple[Path | None, str]:
    if not isinstance(source_path, str) or not source_path.strip():
        return None, "缺少来源路径（source_path）"
    user_path = _resolve_user_path(user_hooks_path)
    if source_path == str(user_path):
        return user_path, ""
    project_path = _resolve_project_path(workspace_root, project_hooks_path)
    if project_path is not None and source_path == str(project_path):
        return project_path, ""
    return None, "该来源文件不允许通过管理页修改（仅支持用户级/项目级 hooks.json）"


def _load_config(path: Path) -> tuple[dict | None, str]:
    """读取配置文件；返回 ``(data, "")`` 或 ``(None, 拒绝原因)``。

    文件不存在是正常起点（以 ``{"hooks": {}}`` 为基底）；文件存在但结构损坏
    一律拒绝（调用方提示用户先修复），绝不覆盖重建。``data["hooks"]`` 在返回
    成功时保证是 dict。
    """
    if not path.exists():
        return {"hooks": {}}, ""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        return None, f"配置文件读取失败：{path}（{exc}）"
    try:
        data = json.loads(raw)
    except ValueError:
        return None, f"配置文件 JSON 非法，请先修复后重试：{path}"
    if not isinstance(data, dict):
        return None, f"配置文件顶层不是 JSON 对象，请先修复后重试：{path}"
    hooks = data.get("hooks")
    if hooks is None:
        data["hooks"] = {}
    elif not isinstance(hooks, dict):
        return None, f"配置文件 hooks 字段不是对象，请先修复后重试：{path}"
    return data, ""


def _save_config(path: Path, data: dict) -> tuple[bool, str]:
    """原子写配置（临时文件 + ``os.replace``），失败只返回原因不抛错。"""
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_name(path.name + ".tmp")
        tmp_path.write_text(text, encoding="utf-8")
        os.replace(tmp_path, path)
    except OSError as exc:
        return False, f"配置文件写入失败：{path}（{exc}）"
    return True, ""


def _entry_digest(entry, event_name: str, source_path: str) -> str | None:
    if not isinstance(entry, dict):
        return None
    command = entry.get("command")
    if not isinstance(command, str) or not command.strip():
        return None
    raw_matcher = entry.get("matcher", "*")
    if not isinstance(raw_matcher, str):
        return None
    matcher = raw_matcher.strip()
    if not matcher:
        return None
    raw_timeout = entry.get("timeout_ms", DEFAULT_TIMEOUT_MS)
    # bool 是 int 的子类，必须显式排除（与 _parse_entry 一致）
    if isinstance(raw_timeout, bool) or not isinstance(raw_timeout, (int, float)):
        return None
    try:
        timeout_ms = int(raw_timeout)
    except (ValueError, OverflowError):
        # 理论上只有 inf / nan（json.loads 默认允许 Infinity / NaN）才走到这
        return None
    if timeout_ms <= 0:
        return None
    if timeout_ms > MAX_TIMEOUT_MS:
        timeout_ms = MAX_TIMEOUT_MS
    return compute_hook_digest(
        {
            "event": event_name,
            "command": command,
            "matcher": matcher,
            "timeout_ms": timeout_ms,
        },
        source_path,
    )


def _locate_entry(hooks: dict, digest, source_path: str) -> tuple[str, int] | None:
    if not isinstance(digest, str) or not digest:
        return None
    for event_name, entries in hooks.items():
        if event_name not in _EVENT_VALUES or not isinstance(entries, list):
            continue
        for index, entry in enumerate(entries):
            if _entry_digest(entry, event_name, source_path) == digest:
                return event_name, index
    return None


def _build_entry(
    *,
    command: str,
    matcher: str,
    timeout_ms: int,
    status_message: str,
    disabled=_MISSING,
    extra: dict | None = None,
) -> dict:
    """按「非默认值才落盘」生成条目，保持配置文件干净。

    ``disabled`` 只接受布尔原值（编辑时从原条目继承）：True/False 均原样保留，
    缺失/非法则不写——编辑操作绝不改变条目的启停状态。``extra`` 是原条目的
    未知字段（不含本模块覆盖的字段），原样搬运以兼容用户手写扩展。
    """
    entry: dict = {"command": command}
    if matcher != "*":
        entry["matcher"] = matcher
    if timeout_ms != DEFAULT_TIMEOUT_MS:
        entry["timeout_ms"] = timeout_ms
    if status_message:
        entry["status_message"] = status_message
    if isinstance(disabled, bool):
        entry["disabled"] = disabled
    for key, value in (extra or {}).items():
        entry[key] = value
    return entry


def _parse_common_fields(payload: dict):
    raw_event = payload.get("event")
    try:
        event = HookEvent(raw_event)
    except (ValueError, TypeError):
        return None, f"未知事件：{raw_event!r}（仅支持 SessionStart/PreToolUse/PostToolUse/Stop）"

    command = payload.get("command")
    if not isinstance(command, str) or not command.strip():
        return None, "命令（command）不能为空"

    raw_timeout = payload.get("timeout_ms", DEFAULT_TIMEOUT_MS)
    # bool 是 int 的子类，必须显式排除（True 会被当成 1ms 的超时）
    if isinstance(raw_timeout, bool) or not isinstance(raw_timeout, (int, float)):
        return None, "timeout_ms 必须是数字"
    try:
        timeout_ms = int(raw_timeout)
    except (ValueError, OverflowError):
        return None, "timeout_ms 非法"
    if timeout_ms <= 0:
        return None, "timeout_ms 必须为正数"
    if timeout_ms > MAX_TIMEOUT_MS:
        # 与 registry 一致：超上限截断而不是拒绝（用户意图明确，截断是安全降级）
        timeout_ms = MAX_TIMEOUT_MS

    raw_matcher = payload.get("matcher")
    matcher = raw_matcher.strip() if isinstance(raw_matcher, str) else ""
    if not matcher:
        matcher = "*"

    raw_status = payload.get("status_message", "")
    status_message = raw_status if isinstance(raw_status, str) else ""

    return (event, command, matcher, timeout_ms, status_message), ""


def save_hook_config(
    payload, *, workspace_root=None, user_hooks_path=None, project_hooks_path=None
) -> tuple[bool, str]:
    try:
        return _save_hook_config_impl(
            payload,
            workspace_root=workspace_root,
            user_hooks_path=user_hooks_path,
            project_hooks_path=project_hooks_path,
        )
    except Exception as exc:  # GUI 调用入口：兜底绝不抛出
        _logger.warning("保存 hook 配置异常（已拒绝）: %s", exc)
        return False, f"保存 hook 配置失败：{exc}"


def delete_hook_config(
    source_path, digest, *, workspace_root=None, user_hooks_path=None, project_hooks_path=None
) -> tuple[bool, str]:
    try:
        if not isinstance(digest, str) or not digest:
            return False, "缺少 hook 指纹（digest）"
        target_path, error = _resolve_source_path(
            source_path, workspace_root, user_hooks_path, project_hooks_path
        )
        if target_path is None:
            return False, error
        data, error = _load_config(target_path)
        if data is None:
            return False, error
        hooks = data["hooks"]
        hit = _locate_entry(hooks, digest, source_path)
        if hit is None:
            return False, _NOT_FOUND_MESSAGE
        event_name, index = hit
        del hooks[event_name][index]
        if not hooks[event_name]:
            del hooks[event_name]
        ok, error = _save_config(target_path, data)
        if not ok:
            return False, error
        return True, "已删除"
    except Exception as exc:  # 兜底绝不抛出
        _logger.warning("删除 hook 配置异常（已拒绝）: %s", exc)
        return False, f"删除 hook 配置失败：{exc}"


def set_hook_disabled(
    source_path, digest, disabled, *, workspace_root=None, user_hooks_path=None, project_hooks_path=None
) -> tuple[bool, str]:
    """启用/停用一条 hook；成功文案 "已启用" / "已禁用"，绝不抛异常。

    ``disabled=True`` 写入 ``"disabled": true``；``False`` 直接删除该键而不是
    写 false（文件只表达与默认值的差异）。disabled 不参与 digest，所以停用后
    条目对 ``HookRegistry`` 不可见、但本模块仍能用同一 digest 定位并恢复。
    """
    try:
        if not isinstance(disabled, bool):
            return False, "disabled 必须是布尔值"
        if not isinstance(digest, str) or not digest:
            return False, "缺少 hook 指纹（digest）"
        target_path, error = _resolve_source_path(
            source_path, workspace_root, user_hooks_path, project_hooks_path
        )
        if target_path is None:
            return False, error
        data, error = _load_config(target_path)
        if data is None:
            return False, error
        hooks = data["hooks"]
        hit = _locate_entry(hooks, digest, source_path)
        if hit is None:
            return False, _NOT_FOUND_MESSAGE
        event_name, index = hit
        entry = hooks[event_name][index]
        if disabled:
            entry["disabled"] = True
        else:
            entry.pop("disabled", None)
        ok, error = _save_config(target_path, data)
        if not ok:
            return False, error
        return True, "已禁用" if disabled else "已启用"
    except Exception as exc:  # 兜底绝不抛出
        _logger.warning("更新 hook 状态异常（已拒绝）: %s", exc)
        return False, f"更新 hook 状态失败：{exc}"


def _save_hook_config_impl(
    payload, *, workspace_root, user_hooks_path, project_hooks_path
) -> tuple[bool, str]:
    if not isinstance(payload, dict):
        return False, "payload 必须是 JSON 对象"
    values, error = _parse_common_fields(payload)
    if values is None:
        return False, error
    event, command, matcher, timeout_ms, status_message = values

    if "source_path" in payload or "digest" in payload:
        return _edit_hook(
            payload,
            event=event,
            command=command,
            matcher=matcher,
            timeout_ms=timeout_ms,
            status_message=status_message,
            workspace_root=workspace_root,
            user_hooks_path=user_hooks_path,
            project_hooks_path=project_hooks_path,
        )
    return _create_hook(
        payload,
        event=event,
        command=command,
        matcher=matcher,
        timeout_ms=timeout_ms,
        status_message=status_message,
        workspace_root=workspace_root,
        user_hooks_path=user_hooks_path,
        project_hooks_path=project_hooks_path,
    )


def _create_hook(
    payload,
    *,
    event,
    command,
    matcher,
    timeout_ms,
    status_message,
    workspace_root,
    user_hooks_path,
    project_hooks_path,
) -> tuple[bool, str]:
    """新增分支：解析目标文件 → 追加条目 → 原子写。"""
    scope = payload.get("scope")
    if scope == "user":
        target_path = _resolve_user_path(user_hooks_path)
    elif scope == "project":
        target_path = _resolve_project_path(workspace_root, project_hooks_path)
        if target_path is None:
            return False, "项目级 hook 需要提供 workspace_root 或 project_hooks_path"
    else:
        return False, f"scope 非法：{scope!r}（仅支持 user/project）"

    data, error = _load_config(target_path)
    if data is None:
        return False, error
    hooks = data["hooks"]
    entries = hooks.get(event.value)
    if entries is None:
        entries = []
        hooks[event.value] = entries
    elif not isinstance(entries, list):
        return False, f"配置文件 {event.value} 的值不是数组，请先修复后重试：{target_path}"
    entries.append(
        _build_entry(
            command=command,
            matcher=matcher,
            timeout_ms=timeout_ms,
            status_message=status_message,
        )
    )
    ok, error = _save_config(target_path, data)
    if not ok:
        return False, error
    return True, "已保存"


def _edit_hook(
    payload,
    *,
    event,
    command,
    matcher,
    timeout_ms,
    status_message,
    workspace_root,
    user_hooks_path,
    project_hooks_path,
) -> tuple[bool, str]:
    """编辑分支：定位 digest → 覆盖字段（event 变化时迁移数组）→ 原子写。"""
    source_path = payload.get("source_path")
    digest = payload.get("digest")
    if not isinstance(digest, str) or not digest:
        return False, "缺少 hook 指纹（digest）"
    target_path, error = _resolve_source_path(
        source_path, workspace_root, user_hooks_path, project_hooks_path
    )
    if target_path is None:
        return False, error
    data, error = _load_config(target_path)
    if data is None:
        return False, error
    hooks = data["hooks"]
    hit = _locate_entry(hooks, digest, source_path)
    if hit is None:
        return False, _NOT_FOUND_MESSAGE
    old_event_name, index = hit
    old_entry = hooks[old_event_name][index]
    new_entry = _build_entry(
        command=command,
        matcher=matcher,
        timeout_ms=timeout_ms,
        status_message=status_message,
        disabled=old_entry.get("disabled", _MISSING),
        extra={k: v for k, v in old_entry.items() if k not in _OWNED_KEYS},
    )
    if event.value == old_event_name:
        hooks[old_event_name][index] = new_entry
    else:
        destination = hooks.get(event.value)
        if destination is None:
            destination = []
            hooks[event.value] = destination
        elif not isinstance(destination, list):
            return False, f"配置文件 {event.value} 的值不是数组，请先修复后重试：{target_path}"
        del hooks[old_event_name][index]
        if not hooks[old_event_name]:
            del hooks[old_event_name]
        destination.append(new_entry)
    ok, error = _save_config(target_path, data)
    if not ok:
        return False, error
    return True, "已更新"
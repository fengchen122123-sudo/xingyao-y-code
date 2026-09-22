"""企业/团队分发场景的管理员强制基线（借鉴 Codex ``requirements.toml``）。

- 受管文件：Windows ``%ProgramData%\\Y-code\\managed.json``；
  其他平台 ``/etc/ycode/managed.json``；``YCODE_MANAGED_FILE`` 环境变量覆盖（测试用）。
- V1 可强制键：
  - ``permission_mode_max``：权限模式上限，请求超限时在消费点降档 + 警告；
  - ``disabled_tools``：工具名列表，注册表构建后过滤（禁用工具不可用）。
- 文件缺失/损坏/顶层非对象时一律视为「无策略」：读取链与工具注册表零行为变化，
  且不产生任何告警（缺省 = 现行为）。
- 本模块只读受管文件，绝不回写；缓存以 (mtime_ns, size) 为键，管理端改写后自动失效。
"""

from __future__ import annotations

import json
import logging
import os
import threading
from typing import Any

from core.app_context import is_known_permission_mode, normalize_permission_mode

# 测试/隔离运行可覆盖受管文件路径；生产默认路径不受影响
MANAGED_FILE_ENV = "YCODE_MANAGED_FILE"

# 权限模式强度排序（越大越宽松）：上限校验 = 生效模式不得比 max 更宽松。
# plan=只读 / confirm=变更前确认 / accept_edits=自动编辑 / full=完全访问
_MODE_RANK = {"plan": 0, "confirm": 1, "accept_edits": 2, "full": 3}

_STATE_LOCK = threading.RLock()
# path -> ((mtime_ns, size), data | None)；data=None 表示该文件版本解析失败，
# 缓存它避免每次读取都重复解析/重复告警。
_CACHE: dict[str, tuple[tuple[int, int], dict | None]] = {}
# 一次性告警去重（如降档提醒不能在每个工具调用上都刷一条）
_WARNED: set[tuple[Any, ...]] = set()


def _log_warning(message, *args):
    """与 config.py 同通道的运行告警（py.warnings），失败绝不影响主流程。"""
    try:
        logging.getLogger("py.warnings").warning(message, *args)
    except Exception:
        pass


def managed_file_path() -> str:
    """返回受管文件路径：环境变量覆盖优先，其次平台默认位置。"""
    override = os.environ.get(MANAGED_FILE_ENV)
    if override and override.strip():
        return override.strip()
    if os.name == "nt":
        program_data = os.environ.get("ProgramData") or r"C:\ProgramData"
        return os.path.join(program_data, "Y-code", "managed.json")
    return "/etc/ycode/managed.json"


def load_managed_policy() -> dict:
    """读取受管策略；文件缺失/损坏/顶层非对象时返回 ``{}``（无策略）。

    返回值是浅拷贝，调用方的修改不会污染缓存。
    """
    path = managed_file_path()
    try:
        stat = os.stat(path)
    except OSError:
        # 文件不存在：清掉陈旧缓存，返回无策略（零行为变化）
        with _STATE_LOCK:
            _CACHE.pop(path, None)
        return {}
    key = (stat.st_mtime_ns, stat.st_size)
    with _STATE_LOCK:
        cached = _CACHE.get(path)
        if cached is not None and cached[0] == key:
            data = cached[1]
            return dict(data) if data is not None else {}
    data = None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            parsed = json.load(handle)
    except (ValueError, OSError) as error:
        _log_warning("受管策略文件 %s 读取/解析失败（%s），本次忽略该策略", path, error)
    else:
        if isinstance(parsed, dict):
            data = parsed
        else:
            _log_warning("受管策略文件 %s 顶层不是 JSON 对象，本次忽略该策略", path)
    with _STATE_LOCK:
        _CACHE[path] = (key, data)
    return dict(data) if data is not None else {}


def _warn_once(key, message, *args):
    with _STATE_LOCK:
        if key in _WARNED:
            return
        _WARNED.add(key)
    _log_warning(message, *args)


def permission_mode_ceiling(policy: dict | None = None) -> str | None:
    """返回规范化后的权限模式上限；无策略/上限值无效时返回 None（不施加约束）。"""
    if policy is None:
        policy = load_managed_policy()
    if not isinstance(policy, dict):
        return None
    raw = policy.get("permission_mode_max")
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw.strip():
        _warn_once(
            ("ceiling-invalid", repr(raw)),
            "受管策略 permission_mode_max 值无效（%r），已忽略该上限",
            raw,
        )
        return None
    if not is_known_permission_mode(raw):
        _warn_once(
            ("ceiling-invalid", raw),
            "受管策略 permission_mode_max 值无法识别（%r），已忽略该上限",
            raw,
        )
        return None
    return normalize_permission_mode(raw)


def clamp_permission_mode(requested: Any, policy: dict | None = None) -> str:
    """把请求模式钳制在受管上限内：超限则降档并留一次性警告。

    无上限或未超限时返回规范化后的请求值（与 ``normalize_permission_mode`` 一致）。
    """
    mode = normalize_permission_mode(requested)
    ceiling = permission_mode_ceiling(policy)
    if ceiling is None or _MODE_RANK[mode] <= _MODE_RANK[ceiling]:
        return mode
    _warn_once(
        ("downgrade", mode, ceiling),
        "受管策略将权限模式 %s 降档为 %s（由管理员锁定：permission_mode_max=%s）",
        mode,
        ceiling,
        ceiling,
    )
    return ceiling


def apply_managed_policy(data: dict, policy: dict) -> dict:
    """把受管策略套用到有效配置（config 读取链返回前调用）。

    仅当受管上限实际压低了 ``permission_mode`` 时返回改写副本；否则原样返回入参
    对象——文件缺失/策略为空时保证读取结果与现状一致。
    """
    if not isinstance(data, dict) or not isinstance(policy, dict) or not policy:
        return data
    if "permission_mode" not in data:
        return data
    ceiling = permission_mode_ceiling(policy)
    if ceiling is None:
        return data
    original = data["permission_mode"]
    clamped = clamp_permission_mode(original, policy)
    if clamped == normalize_permission_mode(original):
        return data
    result = dict(data)
    result["permission_mode"] = clamped
    return result


def enforce_managed_writes(
    data: dict, before: dict, policy: dict | None = None
) -> tuple[dict, tuple[str, ...]]:
    """保存流程守卫：受管锁定键（``permission_mode``）的超限变更一律拒绝。

    返回 ``(写入用 data, 被拒绝的键名元组)``；无策略/未超限时原样返回第一个入参，
    零行为变化。拒绝时把该键回退为写入前值（``before``），并提示「由管理员锁定」。
    """
    if not isinstance(data, dict):
        return data, ()
    if policy is None:
        policy = load_managed_policy()
    ceiling = permission_mode_ceiling(policy)
    if ceiling is None:
        return data, ()
    if "permission_mode" not in data or data.get("permission_mode") is None:
        return data, ()
    requested = data["permission_mode"]
    if clamp_permission_mode(requested, policy) == normalize_permission_mode(requested):
        return data, ()
    result = dict(data)
    if isinstance(before, dict) and "permission_mode" in before:
        result["permission_mode"] = before["permission_mode"]
    else:
        result.pop("permission_mode", None)
    _log_warning(
        "配置键 permission_mode=%r 超出受管策略上限 %r（由管理员锁定），本次写入已拒绝",
        requested,
        ceiling,
    )
    return result, ("permission_mode",)


def disabled_tool_names(policy: dict | None = None) -> tuple[str, ...]:
    """返回受管策略禁用的工具名（已校验；无策略/无效值返回空元组）。"""
    if policy is None:
        policy = load_managed_policy()
    if not isinstance(policy, dict):
        return ()
    raw = policy.get("disabled_tools")
    if raw is None:
        return ()
    if not isinstance(raw, (list, tuple)):
        _log_warning(
            "受管策略 disabled_tools 应为字符串列表（实际为 %r），已忽略该约束", raw
        )
        return ()
    names: list[str] = []
    for item in raw:
        if isinstance(item, str) and item.strip():
            names.append(item.strip())
        else:
            _log_warning("受管策略 disabled_tools 含无效条目 %r，已忽略", item)
    return tuple(names)


def _reset_state_for_tests() -> None:
    """清空进程内缓存与告警去重状态（仅供测试隔离使用）。"""
    with _STATE_LOCK:
        _CACHE.clear()
        _WARNED.clear()
"""Hooks 配置变更（GUI 管理页「新建 / 编辑 / 删除 / 启停」的写入侧）。

为什么需要独立的写入模块：``registry`` 只负责只读加载（且对坏条目「跳过」），
``trust`` 只负责信任状态；GUI 管理页要改动磁盘上的两级 ``hooks.json``。若各
调用方自行写文件，极易与 ``HookRegistry._parse_entry`` 的归一化口径漂移——
写出的条目 registry 读不出、或 digest 对不上（信任记录指向幽灵条目）。本模块
把「写」收敛到一处，并显式复用 registry / trust 的同一套口径：

- 字段级归一化（matcher strip、timeout_ms int 转换 + 上限截断）与
  ``HookRegistry._parse_entry`` 逐条对齐，保证「registry 加载出的 digest」
  一定能被本模块定位（测试用「往返一致性」用例锁定该约束）；
- digest 直接调用 ``trust.compute_hook_digest``，字段集合与 registry 一致
  （只有 event/command/matcher/timeout_ms 参与，disabled/status_message 不参与）。

设计约束：

- 三个公开函数一律返回 ``tuple[bool, str]`` 且**绝不抛异常**：调用方是 GUI
  事件回调，异常会让管理页崩在用户操作上；错误以可展示的中文原因返回。
- 写入用「临时文件 + ``os.replace``」原子替换（与 ``HookTrustStore._save``
  同模式），避免 registry / 执行侧读到半截 JSON——写坏配置文件等于让全部
  hook 静默失效。
- 保持文件干净：字段等于默认值时不落盘（``matcher:"*"`` / ``timeout_ms:60000``
  / 空 ``status_message``），文件只表达「与默认不同的部分」；条目内未覆盖的
  字段（含 ``disabled``）编辑时原样保留，兼容用户手写的扩展字段。
- 文件级损坏（JSON 非法 / 顶层非对象 / hooks 非对象 / 事件值非数组）一律
  拒绝并提示先修复，不做「覆盖重建」——那会静默丢弃用户文件里可能还能手修的
  内容。

调用方（GUI 管理页 / 后续 CLI）从这里导入，不要各自拼写 JSON。
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from core.hooks.registry import default_project_hooks_path, default_user_hooks_path
from core.hooks.trust import compute_hook_digest
from core.hooks.types import DEFAULT_TIMEOUT_MS, MAX_TIMEOUT_MS, HookEvent

_logger = logging.getLogger("ycode.hooks")

#: 本模块编辑时负责覆盖的字段；其余字段（含 disabled）从原条目原样搬运。
_OWNED_KEYS = frozenset({"command", "matcher", "timeout_ms", "status_message", "disabled"})

#: v1 认可的 4 个事件值；未知事件键按 registry 口径跳过（不参与定位）。
_EVENT_VALUES = frozenset(event.value for event in HookEvent)

#: 区分「原条目没有 disabled 键」与「disabled: false」的哨兵。
_MISSING = object()

#: digest 未命中的统一文案（与 trust.apply_hook_trust_action 共用，GUI 提示刷新）。
_NOT_FOUND_MESSAGE = "未找到对应的 hook（配置可能已变更，请刷新后重试）"


# ── 路径解析（与 HookRegistry 构造口径一致）────────────────────


def _resolve_user_path(user_hooks_path) -> Path:
    """用户级配置路径：显式注入优先，否则走 ``~/.ycode/hooks.json`` 口径。"""
    return Path(user_hooks_path) if user_hooks_path else default_user_hooks_path()


def _resolve_project_path(workspace_root, project_hooks_path) -> Path | None:
    """项目级配置路径：显式注入优先，其次由 workspace_root 推导。"""
    if project_hooks_path:
        return Path(project_hooks_path)
    if workspace_root:
        return default_project_hooks_path(workspace_root)
    return None


def _resolve_source_path(
    source_path, workspace_root, user_hooks_path, project_hooks_path
) -> tuple[Path | None, str]:
    """把 ``source_path`` 限定到允许写入的两个配置文件之一（str 比对）。

    为什么按 str 比对而不是 Path 相等：GUI 传来的 source_path 就是 registry
    加载时写入的 ``str(path)``；逐字符相等才是同一路径对象的同一写法，也顺带
    保证后续 digest 复算用的 source_path 与 registry 完全一致（同一 Path 的
    ``str()`` 稳定，跨写法比较可能因规范化差异误判）。
    """
    if not isinstance(source_path, str) or not source_path.strip():
        return None, "缺少来源路径（source_path）"
    user_path = _resolve_user_path(user_hooks_path)
    if source_path == str(user_path):
        return user_path, ""
    project_path = _resolve_project_path(workspace_root, project_hooks_path)
    if project_path is not None and source_path == str(project_path):
        return project_path, ""
    return None, "该来源文件不允许通过管理页修改（仅支持用户级/项目级 hooks.json）"


# ── 配置读写 ────────────────────────────────────────────────


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


# ── 条目归一化与定位（与 HookRegistry._parse_entry 对齐）────────


def _entry_digest(entry, event_name: str, source_path: str) -> str | None:
    """按 ``HookRegistry._parse_entry`` 口径归一化并计算条目 digest。

    返回 None 表示该条目按加载口径不可见（缺 command / matcher 非法 / timeout
    非法），不参与定位——registry 不为这类条目生成 digest，GUI 也不可能拿到
    指向它们的指纹（这类条目只揪出「未知字段里塞了坏值」的手写配置）。

    与 ``_parse_entry`` 的唯一有意偏离：不因 ``disabled=True`` 跳过。禁用条目
    必须仍能被 ``set_hook_disabled`` 重新启用（否则「停用」就成了不可逆操作），
    而 disabled 本就不参与指纹（``compute_hook_digest`` 字段白名单里没有它）。
    """
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
        # 空 matcher 无法确定匹配意图；registry 会跳过该条，这里同步跳过。
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
    """在全部事件数组中按 digest 定位条目，命中返回 ``(事件名, 下标)``。

    事件键取文件里的原始字符串（registry 算 digest 用的就是 ``event.value``
    这个键）；未知事件键与非数组值按 registry 口径跳过。
    """
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
    """校验并归一化 payload 的通用字段（新增/编辑共用）。

    返回 ``((event, command, matcher, timeout_ms, status_message), "")`` 或
    ``(None, 校验失败原因)``。归一化口径与 ``_parse_entry`` 一致，差别只在
    宽容度：matcher 缺失/空白归一到 ``"*"``（GUI 表单场景不因此拒绝），
    而 registry 对「手写文件里的空 matcher」是跳过该条。
    """
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


# ── 公开 API ────────────────────────────────────────────────


def save_hook_config(
    payload, *, workspace_root=None, user_hooks_path=None, project_hooks_path=None
) -> tuple[bool, str]:
    """新增或编辑一条 hook 配置；返回 ``(ok, 中文原因)``，绝不抛异常。

    payload 两种形态（GUI 管理页表单直接对应）：

    - 新增：``{"scope": "user"|"project", "event", "matcher", "command",
      "timeout_ms", "status_message"}``；
    - 编辑：``{"source_path", "digest", "event", "matcher", "command",
      "timeout_ms", "status_message"}``（无 scope）。

    编辑的定位口径与 ``HookRegistry`` 加载完全一致：``source_path`` 必须是
    解析出的用户级/项目级配置路径（str 比对），``digest`` 由 registry 加载
    （或本模块删除/启停后）取得。成功文案：新增 "已保存"、编辑 "已更新"。
    """
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
    """删除一条 hook 配置（定位逻辑与编辑完全一致）；成功文案 "已删除"。

    从事件数组移除命中条目；数组为空则删除该事件键（保持文件干净）。
    """
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
            # 拒绝 "true" 这类字符串：把「想启用」误判成停用是危险方向
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


# ── save 的内部实现 ─────────────────────────────────────────


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
        # 先确认目标数组可用，再做内存搬运（失败整体不落盘，磁盘无副作用）
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
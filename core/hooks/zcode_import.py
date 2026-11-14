"""ZCode hooks 配置导入转换器（ZCode 导入设计 §2）。

分层：

- ``convert_matcher`` / ``convert_entry`` 纯转换函数，不做任何 I/O，可穷举测试；
- ``plan_import`` 扫描来源文件（默认四路，见 ``scan_default_sources``）生成
  导入计划（含跳过原因分类，绝不静默丢条目）；
- ``apply_plan`` 把计划条目合并写入星瑶 hooks.json（按 scope 分组、去重、
  带 ``imported_from`` 溯源）；
- ``rollback_import`` 按 ``imported_from`` 前缀整批撤销。

ZCode 侧格式事实（zai-org/ZCode 仓库 diagnosing-hooks 技能文档）：
配置文件形态 ``{hooks: {enabled?, timeoutMs?, events: {<Event>: [{matcher?, hooks: [...]}]}}}``
默认 disabled；插件形态 ``{hooks: {<Event>: [...]}}`` 无总开关。matcher 是
大小写敏感正则；``command`` 型 timeout 单位是秒、``process`` 型 argv 无 shell。
"""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import subprocess
from pathlib import Path

logger = logging.getLogger("ycode.hooks")

# ── 常量与映射表 ──────────────────────────────────────────────

#: ZCode 七事件里星瑶有对应事件点的四个（事件名两边同拼写，零转换）。
SUPPORTED_EVENTS = ("SessionStart", "PreToolUse", "PostToolUse", "Stop")

#: 星瑶 v1 无对应事件点的三个：跳过并进导入报告（设计 §2.2）。
UNSUPPORTED_EVENTS = ("UserPromptSubmit", "PermissionRequest", "PostToolUseFailure")

#: SessionStart 的 matcher 匹配源是子类型（startup/resume/clear/compact）；
#: 星瑶无子类型，全子类集等价于「全匹配」。
_SESSION_START_SUBTYPES = frozenset({"startup", "resume", "clear", "compact"})

#: ZCode PascalCase 工具名 → 星瑶蛇形小写工具名。ApplyPatch 在 ZCode 里是
#: Write/Edit 的别名，映射成多值 matcher（星瑶 matcher 原生支持 ``|``）。
#: 未知名保留原名——匹配不上任何星瑶工具，无害，导入报告提示人工核对。
TOOL_NAME_MAP = {
    "Bash": "execute_command",
    "Read": "read_file",
    "Write": "write_file",
    "Edit": "edit_file",
    "Agent": "run_subagent",
    "Task": "run_subagent",
    "ApplyPatch": "write_file|edit_file",
    "Glob": "glob_files",
    "Grep": "search_code",
}

#: matcher 分支里出现这些元字符即视为正则语义，无法无损转换（设计 §2.3：
#: 不降级为 ``*``——那会让 hook 从「命中少数工具」变成「命中所有工具」）。
_META_CHARS = re.compile(r"[\[\](){}.*+?\\]")

#: 与 core/hooks/types.py 的上限保持一致（导入侧先行截断，registry 兜底）。
MAX_TIMEOUT_MS = 300000
DEFAULT_TIMEOUT_MS = 60000


# ── 纯转换层 ──────────────────────────────────────────────────


def _unwrap_outer_group(body: str) -> str:
    """剥掉「整体包裹」的外层括号：``(A|B)`` → ``A|B``。

    只在括号从第 0 位一直配平到末位时解包；``(?:A|B)`` 这类解包后残留
    ``?:`` 仍会被元字符检查拒绝，``a(b)c`` 不整体包裹、原样返回。
    """
    while body.startswith("(") and body.endswith(")"):
        depth = 0
        wrapped = True
        for index, char in enumerate(body):
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0 and index != len(body) - 1:
                    wrapped = False
                    break
        if not wrapped or depth != 0:
            break
        body = body[1:-1]
    return body


def _literal_alternation(pattern: str) -> list[str] | None:
    """把正则拆成「纯字面量分支列表」；含锚点/分组以外的正则语义返回 None。

    接受 ``A`` / ``^A$`` / ``^(A|B)$`` / ``A|B`` 四种等价写法（ZCode 文档里的
    常见形态）；其余（字符类、``.*``、前缀匹配、非捕获分组等）一律不可无损
    转换。
    """
    body = pattern.strip()
    if body.startswith("^"):
        body = body[1:]
    if body.endswith("$"):
        body = body[:-1]
    body = _unwrap_outer_group(body)
    if not body:
        return None
    branches = [part.strip() for part in body.split("|")]
    for branch in branches:
        if not branch or _META_CHARS.search(branch):
            return None
    return branches


def convert_matcher(event: str, matcher) -> tuple[str | None, str]:
    """按事件策略转换 matcher，返回 (星瑶 matcher, 跳过原因)。

    返回 ``(None, 原因)`` 表示该条 hook 应跳过（不降级为全匹配）。
    """
    if matcher is None or not str(matcher).strip():
        return "*", ""
    pattern = str(matcher).strip()
    branches = _literal_alternation(pattern)
    if branches is None:
        return None, f"matcher 正则无法无损转换: {pattern!r}"
    if event == "SessionStart":
        # 星瑶无子类型：全子类集 → 全匹配；出现工具名等其他字面量语义不明，跳过。
        if set(branches) <= _SESSION_START_SUBTYPES:
            return "*", ""
        return None, f"SessionStart matcher 含未知子类型: {pattern!r}"
    if event == "Stop":
        # Stop 的 matcher 匹配的是回复预览文本，星瑶 Stop 无 matcher 语义。
        return None, f"Stop matcher（匹配回复文本）无星瑶对应语义: {pattern!r}"
    mapped: list[str] = []
    for branch in branches:
        if branch not in TOOL_NAME_MAP:
            logger.info("ZCode 工具名 %r 无星瑶映射，保留原名（将匹配不上任何工具）", branch)
        name = TOOL_NAME_MAP.get(branch, branch)
        if name not in mapped:
            mapped.append(name)
    return "|".join(mapped), ""


def _resolve_timeout_ms(timeout_ms, timeout_seconds, config_default_ms) -> int:
    """ZCode 超时优先级：timeoutMs → timeout×1000（秒陷阱）→ 配置级 → 默认。"""
    if isinstance(timeout_ms, (int, float)) and not isinstance(timeout_ms, bool):
        return int(timeout_ms)
    if isinstance(timeout_seconds, (int, float)) and not isinstance(timeout_seconds, bool):
        return int(timeout_seconds * 1000)
    if isinstance(config_default_ms, (int, float)) and not isinstance(config_default_ms, bool):
        return int(config_default_ms)
    return DEFAULT_TIMEOUT_MS


def _join_argv(argv: list[str]) -> str:
    """process 型 argv 拼成命令行（Windows 用 list2cmdline 语义，POSIX 用 shlex）。"""
    if os.name == "nt":
        return subprocess.list2cmdline(argv)
    return shlex.join(argv)


def convert_entry(entry, *, config_default_timeout_ms=None):
    """转换单个 ZCode hook 条目，返回 (星瑶条目字段 dict 或 None, 跳过原因)。"""
    if not isinstance(entry, dict):
        return None, "hook 条目不是对象"
    hook_type = entry.get("type")
    if hook_type == "command":
        command = entry.get("command")
        if not isinstance(command, str) or not command.strip():
            return None, "command 型缺少 command 字符串"
        timeout_ms = _resolve_timeout_ms(
            entry.get("timeoutMs"), entry.get("timeout"), config_default_timeout_ms
        )
    elif hook_type == "process":
        command = entry.get("command")
        if not isinstance(command, str) or not command.strip():
            return None, "process 型缺少 command 可执行文件"
        args = entry.get("args")
        if args is not None and not isinstance(args, list):
            return None, "process 型 args 不是数组"
        argv = [command] + [str(item) for item in (args or [])]
        command = _join_argv(argv)
        timeout_ms = _resolve_timeout_ms(entry.get("timeoutMs"), None, config_default_timeout_ms)
    else:
        return None, f"未知 hook 类型: {hook_type!r}（ZCode 只支持 command/process）"

    star: dict = {
        "command": command,
        "timeout_ms": max(1, min(timeout_ms, MAX_TIMEOUT_MS)),
    }
    status_message = entry.get("statusMessage")
    if isinstance(status_message, str) and status_message:
        star["status_message"] = status_message
    return star, ""


# ── 配置解析与导入计划 ────────────────────────────────────────


def _extract_groups(data):
    """提取 (事件分组, 配置级默认超时, 不可导入原因)。

    不可导入原因非空表示整个文件跳过（JSON 坏 / 缺 hooks 键 / 配置文件形态
    未启用）。设计 §2.4：尊重 ZCode 侧 ``enabled`` 总开关语义——配置文件形态
    必须显式 ``enabled: true``；插件形态无总开关。
    """
    if not isinstance(data, dict):
        return {}, None, "配置顶层不是 JSON 对象"
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return {}, None, "缺少 hooks 键"
    default_ms = hooks.get("timeoutMs")
    if not isinstance(default_ms, (int, float)) or isinstance(default_ms, bool):
        default_ms = None
    if "events" in hooks:
        if hooks.get("enabled") is not True:
            return {}, None, "配置文件 hooks 未启用（ZCode 侧默认 disabled，需 enabled: true）"
        events = hooks.get("events")
        if not isinstance(events, dict):
            return {}, None, "hooks.events 不是对象"
        return events, default_ms, ""
    return hooks, default_ms, ""


def _preview(text, limit: int = 80) -> str:
    single = " ".join(str(text).split())
    return single if len(single) <= limit else single[: limit - 1] + "…"


def convert_file(data, *, label: str, path: str, scope: str) -> dict:
    """转换单个来源文件 → 文件级导入报告。"""
    report: dict = {
        "label": label,
        "path": path,
        "scope": scope,
        "status": "imported",
        "reason": "",
        "entries": [],
        "skipped": [],
    }
    groups, default_ms, gate_error = _extract_groups(data)
    if gate_error:
        report["status"] = "skipped"
        report["reason"] = gate_error
        return report
    for event_name, group_list in groups.items():
        if event_name in UNSUPPORTED_EVENTS:
            reason = f"星瑶暂无 {event_name} 事件点"
            hooks = []
            if isinstance(group_list, list):
                for group in group_list:
                    if isinstance(group, dict) and isinstance(group.get("hooks"), list):
                        hooks.extend(group["hooks"])
            for entry in hooks:
                report["skipped"].append(
                    {
                        "event": event_name,
                        "command": _preview(entry.get("command", "") if isinstance(entry, dict) else ""),
                        "reason": reason,
                    }
                )
            continue
        if event_name not in SUPPORTED_EVENTS:
            report["skipped"].append(
                {"event": str(event_name), "command": "", "reason": f"未知事件名: {event_name!r}"}
            )
            continue
        if not isinstance(group_list, list):
            report["skipped"].append(
                {"event": event_name, "command": "", "reason": "事件分组不是数组"}
            )
            continue
        for group in group_list:
            if not isinstance(group, dict):
                report["skipped"].append(
                    {"event": event_name, "command": "", "reason": "分组不是对象"}
                )
                continue
            inner = group.get("hooks")
            if not isinstance(inner, list):
                report["skipped"].append(
                    {"event": event_name, "command": "", "reason": "分组缺少 hooks 数组"}
                )
                continue
            star_matcher, matcher_skip = convert_matcher(event_name, group.get("matcher"))
            for entry in inner:
                star_entry, reason = convert_entry(
                    entry, config_default_timeout_ms=default_ms
                )
                if reason:
                    report["skipped"].append(
                        {
                            "event": event_name,
                            "command": _preview(entry.get("command", "") if isinstance(entry, dict) else ""),
                            "reason": reason,
                        }
                    )
                    continue
                if star_matcher is None:
                    report["skipped"].append(
                        {
                            "event": event_name,
                            "command": _preview(star_entry.get("command", "")),
                            "reason": matcher_skip,
                        }
                    )
                    continue
                report["entries"].append(
                    {
                        **star_entry,
                        "event": event_name,
                        "matcher": star_matcher,
                        "imported_from": f"zcode:{label}",
                    }
                )
    if not report["entries"] and not report["skipped"]:
        report["status"] = "skipped"
        report["reason"] = "文件里没有任何 hook 条目"
    return report


def scan_default_sources(workspace_root=None) -> list[dict]:
    """默认扫描来源（设计 §2.1）：用户级一路 + 项目级三路。插件 hooks 默认不扫。"""
    home = Path.home()
    root = Path(workspace_root or os.getcwd())
    return [
        {"label": "user", "path": home / ".zcode" / "cli" / "config.json", "scope": "user"},
        {"label": "project-config", "path": root / ".zcode" / "config.json", "scope": "project"},
        {"label": "project-zcode-json", "path": root / "zcode.json", "scope": "project"},
        {"label": "project-settings", "path": root / ".zcode" / "settings.json", "scope": "project"},
    ]


def plan_import(sources=None, workspace_root=None) -> dict:
    """扫描来源并生成导入计划（不写盘）。文件不存在是正常情况，不入报告。"""
    if sources is None:
        sources = scan_default_sources(workspace_root)
    files = []
    for source in sources:
        path = Path(source["path"])
        if not path.exists():
            continue
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            files.append(
                {
                    "label": source["label"],
                    "path": str(path),
                    "scope": source["scope"],
                    "status": "skipped",
                    "reason": f"读取失败: {exc}",
                    "entries": [],
                    "skipped": [],
                }
            )
            continue
        try:
            data = json.loads(raw)
        except ValueError as exc:
            files.append(
                {
                    "label": source["label"],
                    "path": str(path),
                    "scope": source["scope"],
                    "status": "skipped",
                    "reason": f"JSON 非法: {exc}",
                    "entries": [],
                    "skipped": [],
                }
            )
            continue
        files.append(
            convert_file(data, label=source["label"], path=str(path), scope=source["scope"])
        )
    return {"files": files}


# ── 落盘应用与回滚 ────────────────────────────────────────────


def target_hooks_path(scope: str, workspace_root=None) -> Path:
    """星瑶侧目标文件：user → ~/.ycode/hooks.json；project → <root>/.ycode/hooks.json。"""
    if scope == "project":
        return Path(workspace_root or os.getcwd()) / ".ycode" / "hooks.json"
    env_dir = os.environ.get("YCODE_DATA_DIR")
    if env_dir and str(env_dir).strip():
        return Path(env_dir) / "hooks.json"
    return Path.home() / ".ycode" / "hooks.json"


#: 写入 hooks.json 时保留的字段（registry 侧忽略未知键，imported_from 仅溯源用）。
_ENTRY_FIELDS = ("command", "matcher", "timeout_ms", "status_message", "imported_from")


def apply_plan(plan: dict, *, workspace_root=None) -> dict:
    """把计划里 status=imported 的条目按 scope 合并写入目标 hooks.json。

    - 目标文件已有同 (event, command) 条目视为重复导入，跳过并计数；
    - 目标文件存在但 JSON 坏 → 该 scope 整体跳过并报告（绝不覆盖坏文件）。
    """
    summary = {"scopes": [], "written": 0, "duplicates": 0, "errors": []}
    by_scope: dict[str, list[dict]] = {}
    for file_report in plan.get("files", []):
        if file_report.get("status") != "imported":
            continue
        by_scope.setdefault(file_report["scope"], []).extend(file_report.get("entries", []))
    for scope, entries in by_scope.items():
        path = target_hooks_path(scope, workspace_root)
        data: dict = {"hooks": {}}
        if path.exists():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(existing, dict) and isinstance(existing.get("hooks"), dict):
                    data = existing
                else:
                    raise ValueError("顶层结构不是 {hooks: {...}}")
            except (OSError, ValueError) as exc:
                summary["errors"].append(f"{path}: 已有文件无法解析（{exc}），该来源未写入")
                continue
        existing_pairs = {
            (str(event), str(item.get("command")))
            for event, items in data["hooks"].items()
            if isinstance(items, list)
            for item in items
            if isinstance(item, dict)
        }
        written = 0
        for entry in entries:
            key = (entry["event"], entry["command"])
            if key in existing_pairs:
                summary["duplicates"] += 1
                continue
            existing_pairs.add(key)
            item = {field: entry[field] for field in _ENTRY_FIELDS if field in entry}
            data["hooks"].setdefault(entry["event"], []).append(item)
            written += 1
        if written:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
        summary["written"] += written
        summary["scopes"].append({"scope": scope, "path": str(path), "written": written})
    return summary


def rollback_import(*, workspace_root=None, label: str | None = None) -> dict:
    """按 ``imported_from`` 撤销导入条目（label 为空撤销全部 zcode: 来源）。

    只删带 ``imported_from`` 标记的条目，星瑶原生条目绝不触碰。
    """
    prefix = f"zcode:{label}" if label else "zcode:"
    removed_total = 0
    touched = []
    for scope in ("user", "project"):
        path = target_hooks_path(scope, workspace_root)
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        hooks = data.get("hooks") if isinstance(data, dict) else None
        if not isinstance(hooks, dict):
            continue
        removed = 0
        for event, items in list(hooks.items()):
            if not isinstance(items, list):
                continue
            kept = []
            for item in items:
                if (
                    isinstance(item, dict)
                    and str(item.get("imported_from", "")).startswith(prefix)
                ):
                    removed += 1
                    continue
                kept.append(item)
            if removed:
                if kept:
                    hooks[event] = kept
                else:
                    del hooks[event]
        if removed:
            path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            removed_total += removed
            touched.append({"scope": scope, "path": str(path), "removed": removed})
    return {"removed": removed_total, "touched": touched}

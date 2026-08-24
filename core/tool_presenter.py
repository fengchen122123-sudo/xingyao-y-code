from __future__ import annotations

import json
import re
from typing import Any


_TOOL_COPY = {
    "read_file": ("查看文件", "已查看文件", "file_path"),
    "write_file": ("写入文件", "已写入文件", "file_path"),
    "edit_file": ("修改文件", "已修改文件", "file_path"),
    "execute_command": ("执行命令", "命令执行完成", "command"),
    "list_directory": ("查看目录", "已查看目录", "dir_path"),
    "glob_files": ("查找文件", "文件查找完成", "pattern"),
    "search_code": ("搜索代码", "代码搜索完成", "query"),
    "search_news": ("搜索资料", "资料搜索完成", "query"),
    "fetch_url": ("访问网址", "网址访问完成", "url"),
    "get_weather": ("查询天气", "天气查询完成", "city"),
    "run_subagent": ("调度子代理", "子代理任务完成", "task_description"),
    "manage_task": ("管理后台任务", "后台任务管理完成", "action"),
    "ask_user": ("等待用户回答", "已获得用户回答", "question"),
    "plan_write": ("更新任务计划", "任务计划已更新", ""),
    "plan_update": ("更新计划项", "计划项已更新", "item_id"),
}

_TOOL_CATEGORIES = {
    "read_file": "read",
    "search_code": "search",
    "glob_files": "search",
    "execute_command": "command",
    "write_file": "mutation",
    "edit_file": "mutation",
}

_TOOL_ACTIONS = {
    "read_file": "读取文件",
    "search_code": "搜索代码",
    "glob_files": "查找文件",
    "execute_command": "运行命令",
    "write_file": "写入文件",
    "edit_file": "修改文件",
    "list_directory": "查看目录",
    "search_news": "搜索资料",
    "fetch_url": "访问网址",
    "get_weather": "查询天气",
    "run_subagent": "调度子代理",
    "manage_task": "管理后台任务",
    "ask_user": "等待用户回答",
    "plan_write": "更新任务计划",
    "plan_update": "更新计划项",
}

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_COOKIE_HEADER_RE = re.compile(r"(?im)^(\s*(?:set-)?cookie\s*:\s*)[^\r\n]*")
_NAMED_SECRET_RE = re.compile(
    r'(?i)(["\']?(?:api[_-]?key|authorization|token|password|client[_-]?secret|secret|cookie)["\']?\s*[:=]\s*)'
    r'(Bearer\s+[A-Za-z0-9._~+/=-]+|"[^"]*"|\'[^\']*\'|[^\s,;]+)'
)
_BEARER_RE = re.compile(r"(?i)(\bBearer\s+)[A-Za-z0-9._~+/=-]+")
_OPENAI_KEY_RE = re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b")


def sanitize_tool_display_text(value: object, limit: int = 160) -> str:
    text = _ANSI_RE.sub("", str(value or ""))
    text = _COOKIE_HEADER_RE.sub(lambda match: f"{match.group(1)}***", text)
    text = _CONTROL_RE.sub("", text.replace("\r", " ").replace("\n", " "))
    text = _NAMED_SECRET_RE.sub(lambda match: f"{match.group(1)}***", text)
    text = _BEARER_RE.sub(lambda match: f"{match.group(1)}***", text)
    text = _OPENAI_KEY_RE.sub("***", text)
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    if limit <= 3:
        return text[:limit]
    return text[: limit - 3].rstrip() + "..."


def _skill_name_from_path(file_path: str) -> str:
    normalized = str(file_path or "").replace("\\", "/")
    parts = [part for part in normalized.split("/") if part]
    if not parts or parts[-1].lower() != "skill.md":
        return ""
    for index, part in enumerate(parts[:-1]):
        if part.lower() == "skills" and index + 1 < len(parts) - 1:
            return parts[index + 1]
    return ""


def describe_permission_request(function_name: str, arguments: dict) -> str:
    """权限询问的人类可读描述：写清楚要执行什么命令 / 查看什么文件。

    GUI 内联权限卡片与任何需要向用户解释工具意图的场景共用，
    绝不把 arguments JSON 原文直接甩给用户。
    """
    args = dict(arguments or {})
    if function_name == "sandbox_outside_access":
        path = str(args.get("path") or "")
        op = str(args.get("operation") or "")
        op_label = _TOOL_ACTIONS.get(op, op or "访问")
        return (
            "访问沙盒外路径（当前项目目录之外）\n"
            f"路径：{path}\n"
            f"操作：{op_label}"
        )
    if function_name == "execute_command":
        cmd = sanitize_tool_display_text(args.get("command") or "", limit=500)
        shell_id = sanitize_tool_display_text(args.get("shell_id") or "", limit=40)
        shell_line = f"\n解释器：{shell_id}（显式覆盖）" if shell_id else ""
        return f"执行命令{shell_line}：\n{cmd}" if cmd else f"执行命令{shell_line}"
    action = _TOOL_ACTIONS.get(function_name, f"使用工具 {function_name}")
    target = tool_target(function_name, args)
    if target:
        return f"{action}：{sanitize_tool_display_text(target, limit=300)}"
    return action


def tool_target(name: str, arguments: dict) -> str:
    key = _TOOL_COPY.get(name, ("使用工具", "工具执行完成", ""))[2]
    target = str(arguments.get(key, "")).strip() if key else ""
    if name == "read_file":
        skill_name = _skill_name_from_path(target)
        if skill_name:
            return skill_name
    if name == "run_subagent":
        target = " ".join(target.split())
        if len(target) > 48:
            target = target[:45] + "..."
    return target


def build_tool_display(
    name: str,
    arguments: dict,
    result: dict | None = None,
    ok: bool | None = None,
) -> dict[str, object]:
    """Build presentation-safe metadata for tool activity consumers."""
    if result is not None and not isinstance(result, dict):
        raise TypeError("result must be a dict or None")

    file_path = str(arguments.get("file_path", ""))
    skill_name = _skill_name_from_path(file_path) if name == "read_file" else ""
    category = "skill" if skill_name else _TOOL_CATEGORIES.get(name, "other")
    target = skill_name or tool_target(name, arguments)
    command = str(arguments.get("command", "")) if name == "execute_command" else ""
    result_data: dict[str, Any] = result or {}
    metrics: dict[str, int] = {}
    if name == "read_file" and ok is True:
        content = result_data.get("content")
        if isinstance(content, str):
            metrics["line_count"] = len(content.splitlines()) if content else 0

    error_summary = ""
    if ok is False:
        error_content = str(result_data.get("content", "") or "").lstrip("🚨").strip()
        error_summary = sanitize_tool_display_text(error_content, 160)

    return {
        "category": category,
        "action": sanitize_tool_display_text(
            _TOOL_ACTIONS.get(name, f"使用工具 {name}"), 160
        ),
        "target": sanitize_tool_display_text(target, 160),
        "command": sanitize_tool_display_text(command, 240),
        "metrics": metrics,
        "error_summary": error_summary,
        "is_skill": bool(skill_name),
    }


def summarize_tool_started(name: str, arguments: dict) -> str:
    if name == "read_file":
        skill_name = _skill_name_from_path(str(arguments.get("file_path", "")))
        if skill_name:
            return f"星瑶正在使用skill：{skill_name}"
    action = _TOOL_COPY.get(name, (f"使用工具 {name}", "工具执行完成", ""))[0]
    target = tool_target(name, arguments)
    # 执行命令的预览需要截断，避免长命令（尤其 python -c / PowerShell）刷屏
    if name == "execute_command" and target:
        target = target.strip()
        if len(target) > 30:
            target = target[:27] + "..."
    return f"星瑶正在{action}：{target}" if target else f"星瑶正在{action}"


def summarize_tool_finished(
    name: str,
    arguments: dict,
    result: dict,
    ok: bool,
) -> str:
    target = tool_target(name, arguments)
    if name == "run_subagent":
        content = str(result.get("content", "") or "")
        status = "completed" if ok else "failed"
        try:
            payload = json.loads(content)
        except (TypeError, ValueError):
            payload = None
        if isinstance(payload, dict):
            status = str(payload.get("status") or status)
        copy = {
            "running": "子代理已在后台启动",
            "completed": "子代理已完成",
            "timed_out": "子代理执行超时",
            "cancelled": "子代理已取消",
            "blocked": "子代理等待计划就绪",
            "failed": "子代理执行失败",
        }.get(status, "子代理暂未完成")
        return f"{copy}：{target}" if target else copy

    if not ok:
        # 显示真实的错误信息，而非通用的"此步骤暂未完成"，
        # 让用户/LLM 知道真正的原因以便修正
        content = str(result.get("content", "") or "").strip()
        # 剥离错误哨兵前缀（🚨）：它只用于程序化失败判定，
        # 拼进展示文案只会在不支持 emoji 的终端变成乱码噪音
        error_hint = content.lstrip("🚨").strip() if content else ""
        # 多行错误压成一行展示；上限 160 字符，避免像原来 80 字符那样
        # 把最关键的指导语（如"请使用 read_file 工具"）截掉
        error_hint = " ".join(error_hint.split())[:160] if error_hint else ""
        if error_hint:
            # 错误文本里已包含目标（路径/命令）时不重复拼接，
            # 避免 "✗ 路径 — 失败：'路径'" 这种路径出现两次的噪音
            if target and target in error_hint:
                return error_hint
            if target:
                return f"{target} — {error_hint}"
            return error_hint
        return f"此步骤暂未完成：{target}" if target else "此步骤暂未完成"

    if name == "read_file":
        skill_name = _skill_name_from_path(str(arguments.get("file_path", "")))
        if skill_name:
            return f"星瑶已使用skill：{skill_name}"
        content = str(result.get("content", "") or "")
        line_count = len(content.splitlines()) if content else 0
        return f"星瑶已查看文件：{target}（{line_count} 行）"

    success = _TOOL_COPY.get(name, ("使用工具", "工具执行完成", ""))[1]
    if name == "execute_command" or not target:
        return success
    return f"星瑶{success}：{target}"

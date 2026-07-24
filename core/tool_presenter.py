from __future__ import annotations

import json


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


def _skill_name_from_path(file_path: str) -> str:
    normalized = str(file_path or "").replace("\\", "/")
    parts = [part for part in normalized.split("/") if part]
    if not parts or parts[-1].lower() != "skill.md":
        return ""
    for index, part in enumerate(parts[:-1]):
        if part.lower() == "skills" and index + 1 < len(parts) - 1:
            return parts[index + 1]
    return ""


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
        # 截取关键错误信息，避免过长
        error_hint = content[:80] if content else ""
        if name == "execute_command":
            return f"命令执行失败：{error_hint}" if error_hint else f"命令暂未完成：{target}"
        if target and error_hint:
            return f"{target} — {error_hint}"
        if error_hint:
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

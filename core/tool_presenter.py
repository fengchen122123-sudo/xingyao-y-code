from __future__ import annotations


_TOOL_COPY = {
    "read_file": ("查看文件", "已查看文件", "file_path"),
    "write_file": ("写入文件", "已写入文件", "file_path"),
    "edit_file": ("修改文件", "已修改文件", "file_path"),
    "execute_command": ("执行命令", "命令执行完成", "command"),
    "list_directory": ("查看目录", "已查看目录", "dir_path"),
    "glob_files": ("查找文件", "文件查找完成", "pattern"),
    "search_code": ("搜索代码", "代码搜索完成", "query"),
    "search_news": ("搜索资料", "资料搜索完成", "query"),
    "run_subagent": ("调度子代理", "子代理任务完成", "task_description"),
}


def tool_target(name: str, arguments: dict) -> str:
    key = _TOOL_COPY.get(name, ("使用工具", "工具执行完成", ""))[2]
    return str(arguments.get(key, "")).strip() if key else ""


def summarize_tool_started(name: str, arguments: dict) -> str:
    action = _TOOL_COPY.get(name, (f"使用工具 {name}", "工具执行完成", ""))[0]
    target = tool_target(name, arguments)
    return f"星瑶正在{action}：{target}" if target else f"星瑶正在{action}"


def summarize_tool_finished(
    name: str,
    arguments: dict,
    result: dict,
    ok: bool,
) -> str:
    target = tool_target(name, arguments)
    if not ok:
        if name == "execute_command":
            return f"命令执行失败：{target}"
        return f"星瑶执行失败：{target}" if target else "星瑶执行工具失败"

    if name == "read_file":
        content = str(result.get("content", "") or "")
        line_count = len(content.splitlines()) if content else 0
        return f"星瑶已查看文件：{target}（{line_count} 行）"

    success = _TOOL_COPY.get(name, ("使用工具", "工具执行完成", ""))[1]
    if name == "execute_command" or not target:
        return success
    return f"星瑶{success}：{target}"

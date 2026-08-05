import pytest
from rich.text import Text

from core.tool_progress import ToolDisplayEvent, ToolProgressAggregator


def plain(lines):
    return Text.from_ansi("".join(lines)).plain


def tool_payload(
    tool_id,
    name,
    category,
    action,
    target,
    *,
    ok=True,
    duration_ms=100,
    command="",
    error_summary="",
    metrics=None,
):
    payload = {
        "id": tool_id,
        "name": name,
        "ok": ok,
        "display": {
            "category": category,
            "action": action,
            "target": target,
            "command": command,
            "metrics": metrics or {},
            "error_summary": error_summary,
            "is_skill": False,
        },
    }
    if duration_ms is not None:
        payload["duration_ms"] = duration_ms
    return payload


def read_payload(tool_id, path, duration_ms=100, line_count=10):
    return tool_payload(
        tool_id,
        "read_file",
        "read",
        "读取文件",
        path,
        duration_ms=duration_ms,
        metrics={"line_count": line_count},
    )


def search_payload(tool_id, query, duration_ms=100):
    return tool_payload(
        tool_id,
        "search_code",
        "search",
        "搜索代码",
        query,
        duration_ms=duration_ms,
    )


def command_payload(tool_id, command, ok, duration_ms=100):
    return tool_payload(
        tool_id,
        "execute_command",
        "command",
        "运行命令",
        command,
        ok=ok,
        duration_ms=duration_ms,
        command=command,
        error_summary="" if ok else "ModuleNotFoundError: pytest",
    )


def mutation_payload(tool_id, path):
    return tool_payload(tool_id, "edit_file", "mutation", "修改文件", path)


def test_three_reads_flush_as_one_group():
    progress = ToolProgressAggregator(clock=lambda: 10.0)
    for index in range(3):
        payload = read_payload(str(index), f"file-{index}.py", duration_ms=100)
        progress.start(payload)
        assert progress.finish(payload) == []
    output = plain(progress.flush())
    assert output.count("已读取") == 1
    assert "已读取 3 个文件" in output
    assert "30 行" in output
    assert "0.3s" in output


def test_category_change_flushes_previous_group():
    progress = ToolProgressAggregator(clock=lambda: 10.0)
    read = read_payload("read", "a.py", duration_ms=100)
    search = search_payload("search", "target", duration_ms=250)
    progress.start(read)
    progress.finish(read)
    progress.start(search)
    assert "已读取 1 个文件" in plain(progress.finish(search))
    assert "已完成 1 次搜索" in plain(progress.flush())


def test_command_failure_is_visible_and_next_command_starts():
    progress = ToolProgressAggregator(clock=lambda: 5.0)
    failed = command_payload("bad", "pytest -q", ok=False, duration_ms=3200)
    corrected = command_payload("good", "python -m pytest -q", ok=True, duration_ms=100)
    progress.start(failed)
    failed_output = plain(progress.finish(failed))
    assert "运行命令失败" in failed_output
    assert "pytest -q" in failed_output
    assert "ModuleNotFoundError: pytest" in failed_output
    progress.start(corrected)
    assert progress.active_status().detail == "python -m pytest -q"
    assert "命令执行完成" in plain(progress.finish(corrected))


def test_missing_duration_uses_injected_clock():
    ticks = iter([10.0, 10.251])
    progress = ToolProgressAggregator(clock=lambda: next(ticks))
    payload = read_payload("read", "a.py", duration_ms=None)
    progress.start(payload)
    progress.finish(payload)
    assert "0.3s" in plain(progress.flush())


def test_diff_suppresses_duplicate_mutation_line():
    progress = ToolProgressAggregator(clock=lambda: 5.0)
    mutation = mutation_payload("edit-1", "core/turn_renderer.py")
    progress.start(mutation)
    diff_line = plain(progress.record_diff("edit-1", "core/turn_renderer.py", 28, 14))
    assert "已修改 core/turn_renderer.py" in diff_line
    assert "+28 / -14 行" in diff_line
    assert progress.finish(mutation) == []


def test_finish_without_start_is_visible_as_an_independent_result():
    progress = ToolProgressAggregator(clock=lambda: 5.0)

    output = plain(progress.finish(command_payload("late", "pytest -q", ok=True)))

    assert "命令执行完成" in output
    assert "pytest -q" in output


def test_duplicate_start_replaces_prior_active_entry():
    ticks = iter([1.0, 2.0])
    progress = ToolProgressAggregator(clock=lambda: next(ticks))
    progress.start(read_payload("same", "old.py"))
    progress.start(read_payload("same", "new.py"))

    status = progress.active_status()

    assert status.count == 1
    assert status.detail == "new.py"
    assert status.started_at == 2.0


def test_mixed_active_status_uses_the_latest_activity_start_time():
    ticks = iter([1.0, 2.0])
    progress = ToolProgressAggregator(clock=lambda: next(ticks))
    progress.start(read_payload("read", "a.py"))
    progress.start(command_payload("command", "pytest -q", ok=True))

    status = progress.active_status()

    assert status.label == "正在运行命令"
    assert status.detail == "pytest -q"
    assert status.started_at == 2.0


def test_duplicate_finish_is_ignored_after_the_first_terminal_event():
    progress = ToolProgressAggregator(clock=lambda: 5.0)
    payload = command_payload("same", "pytest -q", ok=True)
    progress.start(payload)

    first = plain(progress.finish(payload))

    assert "命令执行完成" in first
    assert progress.finish(payload) == []


def test_new_start_with_a_finished_id_opens_a_new_activity():
    progress = ToolProgressAggregator(clock=lambda: 5.0)
    first = command_payload("reused", "pytest -q", ok=True)
    second = command_payload("reused", "python -m pytest -q", ok=True)

    progress.start(first)
    progress.finish(first)
    progress.start(second)

    output = plain(progress.finish(second))

    assert "命令执行完成" in output
    assert "python -m pytest -q" in output


def test_new_start_with_a_finished_id_clears_stale_diff_ownership():
    progress = ToolProgressAggregator(clock=lambda: 5.0)
    first = mutation_payload("reused", "old.py")
    second = mutation_payload("reused", "new.py")
    progress.start(first)
    progress.finish(first)
    progress.record_diff("reused", "old.py", 1, 0)

    progress.start(second)
    output = plain(progress.finish(second))

    assert "已修改文件 new.py" in output


def test_missing_id_is_a_visible_protocol_error_and_never_groups():
    progress = ToolProgressAggregator(clock=lambda: 5.0)
    payload = read_payload("", "a.py")

    progress.start(payload)
    output = plain(progress.finish(payload))

    assert "未知工具事件" in output
    assert progress.flush() == []


def test_unknown_payload_stays_other_without_reading_result_text():
    payload = {
        "id": "unknown",
        "name": "custom_tool",
        "ok": True,
        "display": {"target": "custom target"},
        "result": {"content": "读取 99 个文件，42 行"},
    }

    event = ToolDisplayEvent.from_payload(payload)

    assert event.category == "other"
    assert event.metrics == {}


def test_missing_display_target_does_not_read_top_level_preview():
    payload = command_payload("legacy", "pytest -q", ok=True)
    payload["display"].pop("target")
    payload["preview"] = "raw-derived preview must stay hidden"

    event = ToolDisplayEvent.from_payload(payload)

    assert event.target == ""


def test_subagent_category_is_preserved_as_an_independent_activity():
    progress = ToolProgressAggregator(clock=lambda: 5.0)
    read = read_payload("read", "a.py")
    subagent = tool_payload(
        "subagent",
        "run_subagent",
        "subagent",
        "调度子代理",
        "检查实现",
    )
    progress.start(read)
    progress.finish(read)

    progress.start(subagent)
    status = progress.active_status()
    output = plain(progress.finish(subagent))

    assert ToolDisplayEvent.from_payload(subagent).category == "subagent"
    assert status.label == "正在调度子代理"
    assert status.detail == "检查实现"
    assert "已读取 1 个文件" in output
    assert "已调度子代理 检查实现" in output


def test_normalized_metrics_are_immutable_and_detached_from_payload():
    source_metrics = {"line_count": 3}
    payload = read_payload("read", "a.py", line_count=3)
    payload["display"]["metrics"] = source_metrics
    event = ToolDisplayEvent.from_payload(payload)

    source_metrics["line_count"] = 99

    assert event.metrics["line_count"] == 3
    with pytest.raises(TypeError):
        event.metrics["line_count"] = 4

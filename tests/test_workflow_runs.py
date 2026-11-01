"""星流工作流运行台账测试（create/finish/list/recover/clear + 进程级句柄）。"""

from __future__ import annotations

import pytest

from core.memory import MemoryManager
from core.workflows import runs as runs_module
from core.workflows.runs import (
    WorkflowRunStorage,
    get_run_storage,
    init_run_storage,
)


@pytest.fixture()
def manager(tmp_path):
    return MemoryManager(str(tmp_path / "ycode.db"))


@pytest.fixture()
def storage(manager):
    return WorkflowRunStorage(manager)


def test_create_run_registers_running_with_args(manager, storage):
    run = storage.create_run(
        "pr-review",
        scope="project",
        session_name="主会话",
        args={"base": "origin/main"},
        artifact_dir="E:/p/.ycode/workflow-artifacts/wfr-1",
    )
    assert run["id"].startswith("wfr-")
    assert run["status"] == "running"
    assert run["started_at"] and not run["finished_at"]

    stored = storage.get_run(run["id"])
    assert stored["workflow_name"] == "pr-review"
    assert stored["args"] == {"base": "origin/main"}
    assert stored["artifact_dir"].endswith("wfr-1")


def test_finish_run_only_from_running_and_idempotent(storage):
    run = storage.create_run("x")
    assert storage.finish_run(run["id"], "completed", "2 处发现") is True
    stored = storage.get_run(run["id"])
    assert stored["status"] == "completed"
    assert stored["summary"] == "2 处发现"
    assert stored["finished_at"]
    # 已终态：再收尾不改写（幂等）
    assert storage.finish_run(run["id"], "failed", "迟到") is False
    assert storage.get_run(run["id"])["status"] == "completed"


def test_finish_failed_records_error(storage):
    run = storage.create_run("x")
    storage.finish_run(run["id"], "failed", "npm test 超时")
    stored = storage.get_run(run["id"])
    assert stored["status"] == "failed"
    assert stored["error"] == "npm test 超时"


def test_finish_rejects_invalid_status(storage):
    run = storage.create_run("x")
    with pytest.raises(ValueError):
        storage.finish_run(run["id"], "running")


def test_list_runs_filters_and_orders(storage):
    first = storage.create_run("a")
    second = storage.create_run("a")
    storage.create_run("b")
    storage.finish_run(first["id"], "completed", "ok")

    names = [r["workflow_name"] for r in storage.list_runs("a")]
    assert names == ["a", "a"]  # 过滤 + 倒序（最新在前）
    assert len(storage.list_runs()) == 3  # 空 name = 全部


def test_recover_stale_runs_marks_interrupted_only(storage):
    running = storage.create_run("keep")
    done = storage.create_run("done")
    storage.finish_run(done["id"], "completed", "")

    recovered = storage.recover_stale_runs()
    assert recovered == 1
    assert storage.get_run(running["id"])["status"] == "interrupted"
    assert storage.get_run(running["id"])["finished_at"]
    assert storage.get_run(done["id"])["status"] == "completed"  # 终态不动

    # 无遗留时幂等
    assert storage.recover_stale_runs() == 0


def test_clear_runs_scoped_to_workflow(storage):
    storage.create_run("a")
    storage.create_run("b")
    assert storage.clear_runs("a") == 1
    assert [r["workflow_name"] for r in storage.list_runs()] == ["b"]


def test_process_level_handle_injection(manager):
    storage = init_run_storage(manager)
    assert get_run_storage() is storage
    run = storage.create_run("via-handle")
    assert get_run_storage().get_run(run["id"])["workflow_name"] == "via-handle"
    runs_module._storage = None  # 测试收尾：不泄漏到其他用例
    assert get_run_storage() is None

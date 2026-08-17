import threading
from pathlib import Path

from core.workspace_context import get_workdir, set_workdir, clear_workdir, workspace_scope


def test_get_workdir_falls_back_to_cwd():
    # 未设置时兜底返回进程 cwd
    assert get_workdir() == Path.cwd().resolve()


def test_set_and_get_workdir(tmp_path):
    set_workdir(tmp_path)
    try:
        assert get_workdir() == tmp_path.resolve()
    finally:
        clear_workdir()


def test_workspace_scope_restores_previous(tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    with workspace_scope(tmp_path):
        assert get_workdir() == tmp_path.resolve()
        with workspace_scope(other):  # 嵌套
            assert get_workdir() == other.resolve()
        assert get_workdir() == tmp_path.resolve()  # 恢复外层
    assert get_workdir() == Path.cwd().resolve()  # 退出后回 cwd


def test_workdir_is_thread_local(tmp_path):
    # 线程 A 设置 workdir，不影响主线程
    result = {}

    def worker():
        set_workdir(tmp_path)
        result["inner"] = get_workdir()

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    assert result["inner"] == tmp_path.resolve()
    assert get_workdir() == Path.cwd().resolve()  # 主线程不受影响

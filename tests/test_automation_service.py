"""星轨自动化任务：core 层服务句柄（AutomationService）测试。

覆盖设计规格「服务句柄」：GUI 启动时注入回调，Agent 工具经 get() 访问；
CLI/headless 未注册时 get() 返回 None。用 fake 回调记录调用，不依赖真实数据库。
"""

import pytest

from core.automation import service


@pytest.fixture(autouse=True)
def _reset_singleton():
    """每个用例前后重置模块级单例，避免用例互相污染。"""
    service.unregister()
    yield
    service.unregister()


class _Recorder:
    """记录 7 个回调调用的 fake，返回值可自定义以便断言原样转发。"""

    def __init__(self):
        self.calls = []
        self.returns = {}

    def _make(self, name):
        def callback(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            return self.returns.get(name)

        return callback

    def callbacks(self) -> dict:
        names = (
            "list_tasks",
            "get_task",
            "save_task",
            "delete_task",
            "set_enabled",
            "run_now",
            "get_runs",
        )
        return {name: self._make(name) for name in names}


def _service(recorder: _Recorder) -> service.AutomationService:
    return service.AutomationService(**recorder.callbacks())


# ---------------------------------------------------------------------------
# get / register / unregister
# ---------------------------------------------------------------------------

def test_get_returns_none_when_unregistered():
    assert service.get() is None


def test_register_then_get_returns_same_instance():
    recorder = _Recorder()
    svc = _service(recorder)
    assert service.register(svc) is None
    assert service.get() is svc


def test_register_overwrites_previous_instance():
    first = _service(_Recorder())
    second = _service(_Recorder())
    service.register(first)
    service.register(second)
    assert service.get() is second


def test_unregister_clears_and_is_idempotent():
    svc = _service(_Recorder())
    service.register(svc)
    assert service.get() is svc
    assert service.unregister() is None
    assert service.get() is None
    # 重复 unregister 不抛异常
    assert service.unregister() is None
    assert service.get() is None


# ---------------------------------------------------------------------------
# 7 个方法原样转发
# ---------------------------------------------------------------------------

def test_list_tasks_forwards():
    recorder = _Recorder()
    recorder.returns["list_tasks"] = [{"id": "task-1"}]
    svc = _service(recorder)
    assert svc.list_tasks() == [{"id": "task-1"}]
    assert recorder.calls == [("list_tasks", (), {})]


def test_get_task_forwards():
    recorder = _Recorder()
    recorder.returns["get_task"] = {"id": "task-1"}
    svc = _service(recorder)
    assert svc.get_task("task-1") == {"id": "task-1"}
    assert recorder.calls == [("get_task", ("task-1",), {})]


def test_save_task_forwards():
    recorder = _Recorder()
    recorder.returns["save_task"] = "task-9"
    svc = _service(recorder)
    payload = {"name": "每日站会摘要", "trigger_type": "daily"}
    assert svc.save_task(payload) == "task-9"
    assert recorder.calls == [("save_task", (payload,), {})]


def test_delete_task_forwards():
    recorder = _Recorder()
    recorder.returns["delete_task"] = True
    svc = _service(recorder)
    assert svc.delete_task("task-1") is True
    assert recorder.calls == [("delete_task", ("task-1",), {})]


def test_set_enabled_forwards():
    recorder = _Recorder()
    recorder.returns["set_enabled"] = True
    svc = _service(recorder)
    assert svc.set_enabled("task-1", True) is True
    assert recorder.calls == [("set_enabled", ("task-1", True), {})]


def test_run_now_forwards():
    recorder = _Recorder()
    recorder.returns["run_now"] = {"id": "run-1"}
    svc = _service(recorder)
    assert svc.run_now("task-1") == {"id": "run-1"}
    assert recorder.calls == [("run_now", ("task-1",), {})]


def test_get_runs_forwards():
    recorder = _Recorder()
    recorder.returns["get_runs"] = [{"id": "run-1"}]
    svc = _service(recorder)
    assert svc.get_runs("task-1", 20) == [{"id": "run-1"}]
    assert recorder.calls == [("get_runs", ("task-1", 20), {})]

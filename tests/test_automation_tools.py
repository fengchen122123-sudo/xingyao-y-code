"""星轨自动化 Agent 工具（automation_*）测试。

把 fake AutomationService 注册进 core.automation.service 单例，完全不依赖真实
GUI / 数据库：工具层只允许经服务句柄访问自动化能力，fake 记录调用并返回与桥侧
同构的 dict（ok/errors/msg/deleted/run_id/runs）。
"""

import pytest

from core import app_context
from core.automation import service as automation_service
from core.tool_protocol import ToolResult
from core.tool_registry import build_default_registry

_TOOL_NAMES = (
    "automation_list",
    "automation_create",
    "automation_update",
    "automation_delete",
    "automation_run_now",
)


class FakeAutomationService:
    """记录调用的 fake 服务：方法签名与 AutomationService 完全一致。"""

    def __init__(self):
        self.tasks: dict[str, dict] = {}
        self.runs: dict[str, list] = {}
        self.calls: list[tuple] = []
        self.save_errors: list[str] | None = None
        self.save_msg: str | None = None
        self.runs_ok = True

    # -- 断言辅助 --
    def call_names(self) -> list[str]:
        return [call[0] for call in self.calls]

    def last_saved(self) -> dict:
        saved = [call[1] for call in self.calls if call[0] == "save_task"]
        assert saved, "工具没有调用 save_task"
        return saved[-1]

    def _dto(self, task: dict) -> dict:
        return {
            "id": task["id"],
            "name": task["name"],
            "enabled": task["enabled"],
            "trigger_type": task["trigger_type"],
            "trigger_config": task["trigger_config"],
            "target_session": task["target_session"],
            "permission_mode": task["permission_mode"],
            "trigger_summary": "触发器摘要",
            "next_run_at": task.get("next_run_at"),
            "last_run_at": task.get("last_run_at"),
        }

    # -- 7 个服务方法 --
    def list_tasks(self):
        self.calls.append(("list_tasks",))
        return {"ok": True, "tasks": [self._dto(task) for task in self.tasks.values()]}

    def get_task(self, task_id):
        self.calls.append(("get_task", task_id))
        return self.tasks.get(task_id)

    def save_task(self, task):
        self.calls.append(("save_task", dict(task)))
        if self.save_errors:
            return {"ok": False, "errors": list(self.save_errors)}
        if self.save_msg:
            return {"ok": False, "msg": self.save_msg}
        task_id = str(task.get("id") or f"task-{len(self.tasks) + 1}")
        stored = dict(task)
        stored["id"] = task_id
        stored.setdefault("enabled", True)
        stored.setdefault("next_run_at", 1700000000.0)
        stored.setdefault("last_run_at", None)
        self.tasks[task_id] = stored
        return {"ok": True, "id": task_id}

    def delete_task(self, task_id):
        self.calls.append(("delete_task", task_id))
        return {"ok": True, "deleted": self.tasks.pop(task_id, None) is not None}

    def set_enabled(self, task_id, enabled):
        self.calls.append(("set_enabled", task_id, enabled))
        task = self.tasks.get(task_id)
        if task is None:
            return {"ok": False, "msg": "任务不存在"}
        task["enabled"] = bool(enabled)
        return {"ok": True, "msg": ""}

    def run_now(self, task_id):
        self.calls.append(("run_now", task_id))
        if task_id not in self.tasks:
            return {"ok": False, "msg": "任务不存在"}
        return {"ok": True, "run_id": f"run-{task_id}"}

    def get_runs(self, task_id, limit):
        self.calls.append(("get_runs", task_id, limit))
        if not self.runs_ok:
            return {"ok": False, "msg": "读取运行历史失败"}
        return {"ok": True, "runs": list(self.runs.get(task_id, []))}


def _existing_task(**overrides) -> dict:
    task = {
        "id": "task-1",
        "name": "每日站会摘要",
        "enabled": True,
        "trigger_type": "daily",
        "trigger_config": {"time": "09:30"},
        "target_session": "会话A",
        "prompt": "总结今天的站会",
        "permission_mode": "confirm",
        "missed_run_policy": "skip",
        "notify_on_success": False,
        "created_at": "2026-01-01T00:00:00",
        "updated_at": "2026-01-01T00:00:00",
        "last_run_at": None,
        "next_run_at": 1700000000.0,
        "workdir": "",
        "model_id": "",
    }
    task.update(overrides)
    return task


def _min_args(name: str) -> dict:
    return {
        "automation_list": {},
        "automation_create": {
            "name": "任务",
            "prompt": "提示",
            "trigger_type": "startup",
            "target_session": "会话A",
        },
        "automation_update": {"task_id": "task-1", "name": "新名"},
        "automation_delete": {"task_id": "task-1"},
        "automation_run_now": {"task_id": "task-1"},
    }[name]


@pytest.fixture
def fake_service():
    automation_service.unregister()
    svc = FakeAutomationService()
    automation_service.register(svc)
    old_session = app_context.get_thread_session()
    try:
        yield svc
    finally:
        automation_service.unregister()
        app_context.set_thread_session(old_session)


@pytest.fixture
def registry():
    return build_default_registry()


# ---------------------------------------------------------------------------
# 服务未注册（CLI / headless）
# ---------------------------------------------------------------------------

def test_all_automation_tools_fail_without_gui_service(registry):
    automation_service.unregister()

    for name in _TOOL_NAMES:
        result = registry.execute(name, **_min_args(name))
        assert isinstance(result, ToolResult)
        assert result.is_error is True, name
        assert "仅在 GUI 运行期间可用" in result.content, name


# ---------------------------------------------------------------------------
# automation_list
# ---------------------------------------------------------------------------

def test_list_reports_tasks(fake_service, registry):
    fake_service.tasks["task-1"] = _existing_task()

    result = registry.execute("automation_list")

    assert result.is_error is False
    assert "task-1" in result.content
    assert "每日站会摘要" in result.content
    assert fake_service.call_names() == ["list_tasks"]


def test_list_without_tasks_says_empty(fake_service, registry):
    result = registry.execute("automation_list")

    assert result.is_error is False
    assert "没有" in result.content


def test_list_with_task_id_returns_detail_and_recent_runs(fake_service, registry):
    fake_service.tasks["task-1"] = _existing_task()
    fake_service.runs["task-1"] = [
        {"id": "run-1", "status": "completed", "trigger_kind": "scheduled"}
    ]

    result = registry.execute("automation_list", task_id="task-1")

    assert result.is_error is False
    assert ("get_task", "task-1") in fake_service.calls
    assert ("get_runs", "task-1", 10) in fake_service.calls
    assert "总结今天的站会" in result.content
    assert "run-1" in result.content


def test_list_unknown_task_id_is_error(fake_service, registry):
    result = registry.execute("automation_list", task_id="task-404")

    assert result.is_error is True
    assert "task-404" in result.content


# ---------------------------------------------------------------------------
# automation_create
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "trigger_kwargs, expected_type, expected_config",
    [
        (
            {"trigger_type": "interval", "interval_minutes": 30},
            "interval",
            {"interval_minutes": 30},
        ),
        (
            {"trigger_type": "daily", "time": "09:05"},
            "daily",
            {"time": "09:05"},
        ),
        (
            {"trigger_type": "weekly", "time": "20:00", "weekdays": [1, 5]},
            "weekly",
            {"time": "20:00", "weekdays": [1, 5]},
        ),
    ],
)
def test_create_builds_trigger_config_for_each_type(
    fake_service, registry, trigger_kwargs, expected_type, expected_config
):
    result = registry.execute(
        "automation_create",
        name="定时任务",
        prompt="执行某任务",
        target_session="会话A",
        **trigger_kwargs,
    )

    assert result.is_error is False
    assert len(fake_service.tasks) == 1
    (task,) = fake_service.tasks.values()
    assert task["trigger_type"] == expected_type
    assert task["trigger_config"] == expected_config
    assert task["id"] in result.content
    assert task["target_session"] == "会话A"
    assert task["permission_mode"] == "confirm"
    assert task["missed_run_policy"] == "skip"
    assert task["enabled"] is True


def test_create_startup_needs_no_trigger_config(fake_service, registry):
    result = registry.execute(
        "automation_create",
        name="启动自检",
        prompt="检查环境",
        trigger_type="startup",
        target_session="会话A",
    )

    assert result.is_error is False
    (task,) = fake_service.tasks.values()
    assert task["trigger_config"] == {}
    assert task["trigger_type"] == "startup"


def test_create_passes_optional_fields(fake_service, registry):
    result = registry.execute(
        "automation_create",
        name="周报",
        prompt="写周报",
        trigger_type="weekly",
        time="18:00",
        weekdays=[5],
        target_session="会话B",
        permission_mode="accept_edits",
        missed_run_policy="run_once",
        notify_on_success=True,
        workdir="D:\\work",
        model_id="gpt-x",
    )

    assert result.is_error is False
    task = fake_service.last_saved()
    assert task["permission_mode"] == "accept_edits"
    assert task["missed_run_policy"] == "run_once"
    assert task["notify_on_success"] is True
    assert task["workdir"] == "D:\\work"
    assert task["model_id"] == "gpt-x"


def test_create_defaults_target_session_to_bound_thread_session(fake_service, registry):
    app_context.set_thread_session("绑定的会话")

    result = registry.execute(
        "automation_create",
        name="定时",
        prompt="做事",
        trigger_type="daily",
        time="08:00",
    )

    assert result.is_error is False
    (task,) = fake_service.tasks.values()
    assert task["target_session"] == "绑定的会话"


def test_create_without_any_session_is_rejected(fake_service, registry):
    app_context.set_thread_session(None)

    result = registry.execute(
        "automation_create",
        name="定时",
        prompt="做事",
        trigger_type="daily",
        time="08:00",
    )

    assert result.is_error is True
    assert "target_session" in result.content
    assert fake_service.call_names() == []


@pytest.mark.parametrize("mode", ["plan", "full"])
def test_create_rejects_unattended_high_permission_modes(fake_service, registry, mode):
    result = registry.execute(
        "automation_create",
        name="定时",
        prompt="做事",
        trigger_type="startup",
        target_session="会话A",
        permission_mode=mode,
    )

    assert result.is_error is True
    assert "confirm 或 accept_edits" in result.content
    assert "GUI 星轨自动化面板" in result.content
    assert fake_service.call_names() == []


# ---------------------------------------------------------------------------
# automation_update
# ---------------------------------------------------------------------------

def test_update_merges_partial_fields_without_wiping_others(fake_service, registry):
    fake_service.tasks["task-1"] = _existing_task()

    result = registry.execute("automation_update", task_id="task-1", name="改名后的任务")

    assert result.is_error is False
    saved = fake_service.last_saved()
    assert saved["id"] == "task-1"
    assert saved["name"] == "改名后的任务"
    # 未传字段必须原样保留（桥侧 save_task 是整体覆盖）
    assert saved["prompt"] == "总结今天的站会"
    assert saved["trigger_type"] == "daily"
    assert saved["trigger_config"] == {"time": "09:30"}
    assert saved["target_session"] == "会话A"
    assert saved["permission_mode"] == "confirm"
    assert saved["missed_run_policy"] == "skip"
    assert saved["workdir"] == ""


def test_update_rebuilds_trigger_config_only_when_trigger_changes(fake_service, registry):
    fake_service.tasks["task-1"] = _existing_task()

    result = registry.execute(
        "automation_update",
        task_id="task-1",
        trigger_type="weekly",
        time="07:00",
        weekdays=[2, 7],
        permission_mode="accept_edits",
    )

    assert result.is_error is False
    saved = fake_service.last_saved()
    assert saved["trigger_type"] == "weekly"
    assert saved["trigger_config"] == {"time": "07:00", "weekdays": [2, 7]}
    assert saved["permission_mode"] == "accept_edits"
    assert saved["prompt"] == "总结今天的站会"


def test_update_partial_trigger_params_reuse_existing_config(fake_service, registry):
    fake_service.tasks["task-1"] = _existing_task(
        trigger_type="weekly",
        trigger_config={"time": "20:00", "weekdays": [1]},
    )

    result = registry.execute("automation_update", task_id="task-1", time="21:30")

    assert result.is_error is False
    saved = fake_service.last_saved()
    assert saved["trigger_config"] == {"time": "21:30", "weekdays": [1]}


def test_update_missing_task_is_error(fake_service, registry):
    result = registry.execute("automation_update", task_id="task-404", name="新名")

    assert result.is_error is True
    assert "task-404" in result.content
    assert "save_task" not in fake_service.call_names()


@pytest.mark.parametrize("mode", ["plan", "full"])
def test_update_rejects_unattended_high_permission_modes(fake_service, registry, mode):
    fake_service.tasks["task-1"] = _existing_task()

    result = registry.execute("automation_update", task_id="task-1", permission_mode=mode)

    assert result.is_error is True
    assert "confirm 或 accept_edits" in result.content
    assert "GUI 星轨自动化面板" in result.content
    assert "save_task" not in fake_service.call_names()


def test_update_can_disable_task_via_set_enabled(fake_service, registry):
    fake_service.tasks["task-1"] = _existing_task(enabled=True)

    result = registry.execute("automation_update", task_id="task-1", enabled=False)

    assert result.is_error is False
    assert ("set_enabled", "task-1", False) in fake_service.calls
    assert fake_service.tasks["task-1"]["enabled"] is False


# ---------------------------------------------------------------------------
# automation_delete / automation_run_now
# ---------------------------------------------------------------------------

def test_delete_removes_task(fake_service, registry):
    fake_service.tasks["task-1"] = _existing_task()

    result = registry.execute("automation_delete", task_id="task-1")

    assert result.is_error is False
    assert ("delete_task", "task-1") in fake_service.calls
    assert "task-1" not in fake_service.tasks


def test_delete_missing_task_is_error(fake_service, registry):
    result = registry.execute("automation_delete", task_id="task-404")

    assert result.is_error is True
    assert "task-404" in result.content


def test_run_now_triggers_manual_run(fake_service, registry):
    fake_service.tasks["task-1"] = _existing_task()

    result = registry.execute("automation_run_now", task_id="task-1")

    assert result.is_error is False
    assert ("run_now", "task-1") in fake_service.calls
    assert "run-task-1" in result.content


def test_run_now_missing_task_is_error(fake_service, registry):
    result = registry.execute("automation_run_now", task_id="task-404")

    assert result.is_error is True
    assert "任务不存在" in result.content


# ---------------------------------------------------------------------------
# 桥侧错误原样透出
# ---------------------------------------------------------------------------

def test_create_surfaces_bridge_errors(fake_service, registry):
    fake_service.save_errors = ["任务名称不能为空", "提示词不能为空"]

    result = registry.execute(
        "automation_create",
        name="",
        prompt="",
        trigger_type="startup",
        target_session="会话A",
    )

    assert result.is_error is True
    assert "任务名称不能为空" in result.content
    assert "提示词不能为空" in result.content


def test_create_surfaces_bridge_msg(fake_service, registry):
    fake_service.save_msg = "应用正在退出"

    result = registry.execute(
        "automation_create",
        name="定时",
        prompt="做事",
        trigger_type="startup",
        target_session="会话A",
    )

    assert result.is_error is True
    assert "应用正在退出" in result.content


def test_update_surfaces_bridge_errors(fake_service, registry):
    fake_service.tasks["task-1"] = _existing_task()
    fake_service.save_errors = ["触发器：触发间隔非法"]

    result = registry.execute("automation_update", task_id="task-1", name="新名")

    assert result.is_error is True
    assert "触发器：触发间隔非法" in result.content


# ---------------------------------------------------------------------------
# 注册与 schema 断言
# ---------------------------------------------------------------------------

def test_registry_registers_automation_tools_with_expected_capabilities(registry):
    names = {schema["function"]["name"] for schema in registry.api_schemas()}
    assert set(_TOOL_NAMES) <= names

    list_spec = registry.get("automation_list")
    assert list_spec.read_only is True
    assert list_spec.requires_permission is False
    assert list_spec.parallel_safe is True

    for name in (
        "automation_create",
        "automation_update",
        "automation_delete",
        "automation_run_now",
    ):
        spec = registry.get(name)
        assert spec.read_only is False
        assert spec.requires_permission is True
        assert spec.parallel_safe is False


def test_create_description_documents_triggers_and_permission_limit(registry):
    description = registry.get("automation_create").description

    assert "interval_minutes" in description
    assert "HH:MM" in description
    assert "1=周一" in description
    assert "confirm" in description and "accept_edits" in description
    assert "每天" in description


def test_create_schema_exposes_trigger_parameters(registry):
    properties = (
        registry.get("automation_create")
        .api_schema()["function"]["parameters"]["properties"]
    )

    assert {"name", "prompt", "trigger_type"} <= set(properties)
    assert {"interval_minutes", "time", "weekdays"} <= set(properties)
    assert properties["permission_mode"]["default"] == "confirm"
    assert properties["missed_run_policy"]["default"] == "skip"
    assert properties["notify_on_success"]["default"] is False

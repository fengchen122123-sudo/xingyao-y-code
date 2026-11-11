"""星轨自动化回合工具策略（G3：防任务自我繁殖）测试。

覆盖四层同源策略：线程本地标记与 scope 语义、core.llm 工具 schema 过滤、
工具 wrapper 拦截、Harness 管线门控兜底。FakeAutomationService 与
tests/test_automation_tools.py 同风格：只经 core.automation.service 单例注入，
完全不依赖 GUI / SQLite。
"""

import json

import pytest

import core.app_context as app_context
import core.llm as llm
from core.automation import service as automation_service
from core.automation import tool_policy
from core.harness.tool_pipeline import ToolPipeline
from core.tool_protocol import ToolResult
from core.tool_registry import build_default_registry

_WRITE_TOOLS = (
    "automation_create",
    "automation_update",
    "automation_delete",
    "automation_run_now",
)


class FakeAutomationService:
    """记录调用的 fake 服务：方法签名与 AutomationService 一致。"""

    def __init__(self):
        self.tasks: dict[str, dict] = {}
        self.calls: list[tuple] = []

    def call_names(self) -> list[str]:
        return [call[0] for call in self.calls]

    def list_tasks(self):
        self.calls.append(("list_tasks",))
        return {
            "ok": True,
            "tasks": [
                {
                    "id": task["id"],
                    "name": task["name"],
                    "enabled": task.get("enabled", True),
                    "trigger_type": task.get("trigger_type", "startup"),
                    "trigger_config": task.get("trigger_config", {}),
                    "target_session": task.get("target_session", ""),
                    "permission_mode": task.get("permission_mode", "confirm"),
                    "trigger_summary": "触发器摘要",
                    "next_run_at": None,
                    "last_run_at": None,
                }
                for task in self.tasks.values()
            ],
        }

    def get_task(self, task_id):
        self.calls.append(("get_task", task_id))
        return self.tasks.get(task_id)

    def save_task(self, task):
        self.calls.append(("save_task", dict(task)))
        task_id = str(task.get("id") or "task-1")
        stored = dict(task)
        stored["id"] = task_id
        stored.setdefault("enabled", True)
        self.tasks[task_id] = stored
        return {"ok": True, "id": task_id}

    def delete_task(self, task_id):
        self.calls.append(("delete_task", task_id))
        return {"ok": True, "deleted": self.tasks.pop(task_id, None) is not None}

    def set_enabled(self, task_id, enabled):
        self.calls.append(("set_enabled", task_id, enabled))
        return {"ok": True, "msg": ""}

    def run_now(self, task_id):
        self.calls.append(("run_now", task_id))
        return {"ok": True, "run_id": f"run-{task_id}"}

    def get_runs(self, task_id, limit):
        self.calls.append(("get_runs", task_id, limit))
        return {"ok": True, "runs": []}


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
        "workdir": "",
        "model_id": "",
        "max_runs": None,
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


class _EmptyMcp:
    def get_schemas(self):
        return []

    def get_schema(self, _name):
        return None


class _Decoded:
    """管线门控入参替身：只需要 name / call_id。"""

    def __init__(self, name, call_id="call-1"):
        self.name = name
        self.call_id = call_id
        self.arguments = {}


@pytest.fixture
def fake_service():
    automation_service.unregister()
    svc = FakeAutomationService()
    automation_service.register(svc)
    try:
        yield svc
    finally:
        automation_service.unregister()


@pytest.fixture
def registry():
    return build_default_registry()


@pytest.fixture
def tool_schema_env(monkeypatch):
    """工具 schema 构建环境：MCP 走空实现（不连接、不引入外部依赖）。"""
    monkeypatch.setattr(llm, "_mcp_first_wait_done", True)
    monkeypatch.setattr(llm, "_get_mcp_manager", lambda: _EmptyMcp())


@pytest.fixture
def clean_automation_turn():
    """前后都清空线程标记，避免测试之间互相污染。"""
    app_context.set_thread_automation_turn(None)
    try:
        yield
    finally:
        app_context.set_thread_automation_turn(None)


def _schema_names():
    return {
        str(schema.get("function", {}).get("name", ""))
        for schema in llm.get_tools_schema()
    }


# ---------------------------------------------------------------------------
# 1. 回归：无标记时策略完全惰性，普通会话不受任何影响
# ---------------------------------------------------------------------------

def test_plain_turn_hides_nothing_and_blocks_nothing(clean_automation_turn):
    assert app_context.get_thread_automation_turn() is None
    assert tool_policy.is_automation_turn() is False
    assert tool_policy.hidden_tool_names_for_current_turn() == frozenset()
    assert tool_policy.automation_write_denied() is False
    for name in _WRITE_TOOLS + ("automation_list", "read_file"):
        assert tool_policy.is_automation_turn_tool_blocked(name) is False, name
    # 空名字不阻断（工具名解析失败时不得误拦）
    assert tool_policy.is_automation_turn_tool_blocked("") is False


# ---------------------------------------------------------------------------
# 2. 自动化回合：只隐藏/拒绝 4 个写工具，automation_list 保留
# ---------------------------------------------------------------------------

def test_scope_hides_only_write_tools(clean_automation_turn):
    with app_context.automation_turn_scope("run-1"):
        assert app_context.get_thread_automation_turn() == "run-1"
        assert tool_policy.is_automation_turn() is True
        assert tool_policy.automation_write_denied() is True
        assert (
            tool_policy.hidden_tool_names_for_current_turn()
            == tool_policy.AUTOMATION_TURN_HIDDEN_TOOLS
        )
        for name in _WRITE_TOOLS:
            assert tool_policy.is_automation_turn_tool_blocked(name) is True, name
        # 只读查询必须保留：模型要靠它了解现状并向用户提建议
        assert tool_policy.is_automation_turn_tool_blocked("automation_list") is False
        assert tool_policy.is_automation_turn_tool_blocked("read_file") is False


# ---------------------------------------------------------------------------
# 3. 退出恢复（含嵌套 / 异常 / 空 run_id）
# ---------------------------------------------------------------------------

def test_scope_restores_after_exit_and_supports_nesting(clean_automation_turn):
    with app_context.automation_turn_scope("run-outer"):
        with app_context.automation_turn_scope("run-inner"):
            assert app_context.get_thread_automation_turn() == "run-inner"
        assert app_context.get_thread_automation_turn() == "run-outer"
        assert tool_policy.is_automation_turn_tool_blocked("automation_delete") is True
    assert app_context.get_thread_automation_turn() is None
    assert tool_policy.is_automation_turn_tool_blocked("automation_delete") is False
    assert tool_policy.hidden_tool_names_for_current_turn() == frozenset()


def test_scope_restores_on_exception(clean_automation_turn):
    with pytest.raises(RuntimeError):
        with app_context.automation_turn_scope("run-boom"):
            assert tool_policy.is_automation_turn() is True
            raise RuntimeError("boom")
    assert app_context.get_thread_automation_turn() is None
    assert tool_policy.is_automation_turn() is False


def test_empty_run_id_means_plain_turn(clean_automation_turn):
    # 回合入口 automation_run_id 为 "" 时传 None：不得把普通回合误判成自动化回合
    with app_context.automation_turn_scope(None):
        assert app_context.get_thread_automation_turn() is None
        assert tool_policy.is_automation_turn() is False
    with app_context.automation_turn_scope(""):
        assert tool_policy.is_automation_turn() is False


# ---------------------------------------------------------------------------
# 4. 工具 schema：自动化回合不含 4 个写工具，普通回合含
# ---------------------------------------------------------------------------

def test_tool_schema_hides_write_tools_only_in_automation_turn(
    tool_schema_env, clean_automation_turn
):
    plain = _schema_names()
    for name in _WRITE_TOOLS:
        assert name in plain, name
    assert "automation_list" in plain

    with app_context.automation_turn_scope("run-schema"):
        automation = _schema_names()

    assert automation == plain - set(_WRITE_TOOLS)
    assert "automation_list" in automation
    # 退出后必须逐元素回到普通会话工具集（前缀缓存前提）
    assert _schema_names() == plain


def test_secretary_branch_unaffected_by_automation_turn(
    tool_schema_env, clean_automation_turn, monkeypatch
):
    monkeypatch.setattr(llm.secretary, "is_secretary_session", lambda *_a, **_k: True)
    plain = _schema_names()
    for name in _WRITE_TOOLS:
        assert name not in plain, name
    with app_context.automation_turn_scope("run-secretary"):
        automation = _schema_names()
    assert automation == plain


# ---------------------------------------------------------------------------
# 5. 工具 wrapper：自动化回合拒绝且不落库，普通回合正常执行
# ---------------------------------------------------------------------------

def test_write_wrappers_deny_in_automation_turn(fake_service, registry, clean_automation_turn):
    fake_service.tasks["task-1"] = _existing_task()
    with app_context.automation_turn_scope("run-1"):
        results = {
            name: registry.execute(name, **_min_args(name)) for name in _WRITE_TOOLS
        }
    for name, result in results.items():
        assert isinstance(result, ToolResult), name
        assert result.is_error is True, name
        assert result.content == tool_policy.AUTOMATION_TURN_DENIED_MESSAGE, name
    # 一道都没打到服务层：绝不产生写副作用
    assert fake_service.calls == []


def test_write_wrappers_execute_in_plain_turn(fake_service, registry, clean_automation_turn):
    fake_service.tasks["task-1"] = _existing_task()

    created = registry.execute("automation_create", **_min_args("automation_create"))
    assert created.is_error is False
    updated = registry.execute("automation_update", **_min_args("automation_update"))
    assert updated.is_error is False
    ran = registry.execute("automation_run_now", **_min_args("automation_run_now"))
    assert ran.is_error is False
    deleted = registry.execute("automation_delete", **_min_args("automation_delete"))
    assert deleted.is_error is False

    assert fake_service.call_names() == [
        "save_task",
        "get_task",
        "save_task",
        "run_now",
        "delete_task",
    ]


def test_automation_list_still_works_in_automation_turn(
    fake_service, registry, clean_automation_turn
):
    fake_service.tasks["task-1"] = _existing_task()
    with app_context.automation_turn_scope("run-1"):
        result = registry.execute("automation_list")
    assert result.is_error is False
    assert "task-1" in result.content
    assert fake_service.call_names() == ["list_tasks"]


# ---------------------------------------------------------------------------
# 6. Harness 管线门控兜底（MCP / 插件 / 同名调用旁路）
# ---------------------------------------------------------------------------

def test_pipeline_gate_blocks_write_tools(clean_automation_turn):
    with app_context.automation_turn_scope("run-1"):
        message = ToolPipeline._automation_gate_blocked(
            None, _Decoded("automation_delete"), None
        )
    assert message == {
        "role": "tool",
        "tool_call_id": "call-1",
        "name": "automation_delete",
        "content": tool_policy.AUTOMATION_TURN_DENIED_MESSAGE,
    }


def test_pipeline_gate_allows_plain_turn_and_readonly_tool(clean_automation_turn):
    assert (
        ToolPipeline._automation_gate_blocked(None, _Decoded("automation_delete"), None)
        is None
    )
    with app_context.automation_turn_scope("run-1"):
        assert (
            ToolPipeline._automation_gate_blocked(None, _Decoded("automation_list"), None)
            is None
        )


def test_pipeline_end_to_end_blocks_tool_call(
    fake_service, clean_automation_turn
):
    # 门控在权限判定之前，因此无需权限环境即可断言调用被拦下
    with app_context.automation_turn_scope("run-e2e"):
        result = llm._execute_single_tool_call({
            "id": "call-1",
            "function": {
                "name": "automation_delete",
                "arguments": json.dumps({"task_id": "task-1"}),
            },
        })
    assert result["content"] == tool_policy.AUTOMATION_TURN_DENIED_MESSAGE
    assert fake_service.calls == []


def test_pipeline_end_to_end_allows_tool_call_in_plain_turn(
    fake_service, clean_automation_turn
):
    fake_service.tasks["task-1"] = _existing_task()
    result = llm._execute_single_tool_call({
        "id": "call-1",
        "function": {
            "name": "automation_list",
            "arguments": json.dumps({}),
        },
    })
    assert "task-1" in result["content"]
    assert fake_service.call_names() == ["list_tasks"]

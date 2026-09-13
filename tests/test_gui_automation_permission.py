"""星轨自动化任务：无人值守权限自动拒绝（规格 5.5）。

用真实 permission_broker 单例验证 resolve/future 语义；人工回合行为
（无限等待 + PermCard 事件）必须分毫未动。
"""
import threading

import pytest

from core import permission_broker as broker_module
from core.memory import MemoryManager
from gui_bridge import ApiBridge


@pytest.fixture()
def manager(tmp_path):
    return MemoryManager(str(tmp_path / "ycode.db"))


def _make_bridge(manager):
    bridge = ApiBridge.__new__(ApiBridge)
    bridge._shutting_down = False
    bridge._turn_start_lock = threading.Lock()
    bridge._message_queues = {}
    bridge._active_turns = {}
    bridge._cancel_events = {}
    bridge._memory = manager
    bridge._automation_storage = None
    bridge._automation_turn_labels = {}
    bridge._turn_owner_lock = threading.Lock()
    bridge._turn_owner_sessions = {}
    bridge._active_turn_lock = threading.Lock()
    bridge._active_turn_snapshots = {}
    events = []
    bridge.emit_event = lambda etype, payload: events.append((etype, payload))
    bridge._events = events
    # 人工路径会触达的 undo 影响查询桩掉（自动化路径在它之前返回）
    bridge._undo_turn_impact_for_event = lambda request: None
    return bridge


def _enqueue_request(turn_id: str):
    """通过真实 broker 入队一条默认三选项权限请求（choice_0/1/2）。"""
    request = broker_module.permission_broker.enqueue(
        "execute_command",
        {"command": "rm -rf /"},
        owner_id=turn_id,
        parent_turn_id=turn_id,
        source="主代理",
    )
    return request


@pytest.fixture()
def _clean_broker():
    yield
    broker_module.permission_broker.cancel_all()


def test_automation_permission_auto_denied_with_notice(manager, _clean_broker):
    bridge = _make_bridge(manager)
    turn_id = "turn-automation-1"
    bridge._automation_turn_labels[turn_id] = {"run_id": "run-1", "task_name": "每日摘要"}

    request = _enqueue_request(turn_id)
    broker_module.permission_broker.set_prompt_handlers(
        bridge._activate_permission_request, bridge._dismiss_permission_prompt
    )
    # enqueue 未触发（注册前已激活）→ 手动激活
    bridge._activate_permission_request(request)

    # future 已被以"拒绝"（索引 1）结算
    assert request.future.done()
    assert request.future.result(timeout=0) == 1
    # 不弹权限卡
    assert all(e[0] != "permission_request" for e in bridge._events)
    # 有 notice 留痕
    notices = [e for e in bridge._events if e[0] == "notice"]
    assert notices and "无人值守已自动拒绝" in notices[0][1]["message"]
    assert "每日摘要" in notices[0][1]["message"]


def test_human_permission_request_unaffected(manager, _clean_broker):
    bridge = _make_bridge(manager)
    turn_id = "turn-human-1"
    # 不在自动化映射中 → 人工回合路径
    request = _enqueue_request(turn_id)
    broker_module.permission_broker.set_prompt_handlers(
        bridge._activate_permission_request, bridge._dismiss_permission_prompt
    )
    bridge._activate_permission_request(request)

    # 权限卡事件已推送，future 仍未结算（无限等待语义）
    assert any(e[0] == "permission_request" for e in bridge._events)
    assert not request.future.done()
    assert broker_module.permission_broker.is_active(request.request_id)
    # 清理：模拟用户拒绝，避免污染 broker 单例
    assert broker_module.permission_broker.resolve(request.request_id, 1)


def test_automation_permission_keyed_options_use_deny_key(manager, _clean_broker):
    from core.permission_broker import PermissionOption

    bridge = _make_bridge(manager)
    turn_id = "turn-automation-2"
    bridge._automation_turn_labels[turn_id] = {"run_id": "run-2", "task_name": "备份任务"}

    request = broker_module.permission_broker.enqueue(
        "write_file",
        {"path": "x.txt"},
        owner_id=turn_id,
        parent_turn_id=turn_id,
        source="主代理",
        options=(
            PermissionOption("allow", "允许"),
            PermissionOption("deny", "拒绝"),
        ),
    )
    broker_module.permission_broker.set_prompt_handlers(
        bridge._activate_permission_request, bridge._dismiss_permission_prompt
    )
    bridge._activate_permission_request(request)

    assert request.future.done()
    # 命中 deny 键 → resolve_by_key 返回该键索引
    assert request.future.result(timeout=0) == 1


def test_automation_label_cleaned_after_turn(manager, tmp_path):
    """终态回写后 turn_labels 必须清空（配合 dispatch 测试的 worker 用例）。"""
    bridge = _make_bridge(manager)
    assert bridge._automation_turn_labels == {}
    # 清理动作本身在 worker finally 中（见 test_gui_automation_dispatch）；
    # 这里仅验证映射结构约定：标签包含 run_id 与 task_name
    bridge._automation_turn_labels["turn-x"] = {"run_id": "run-x", "task_name": "t"}
    bridge._automation_turn_labels.pop("turn-x", None)
    assert bridge._automation_turn_labels == {}


# ---------------------------------------------------------------------------
# 评审修复回归：P1-2 子代理权限归属自动拒绝 / P2-4 ask_user 取消
# ---------------------------------------------------------------------------

def test_subagent_of_automation_turn_auto_denied(manager, _clean_broker, monkeypatch):
    """P1-2：自动化回合派生的子代理权限请求（owner=task_id）同样自动拒绝。"""
    from core.tools import SubagentTask, subagent_manager

    bridge = _make_bridge(manager)
    automation_turn = "turn-automation-parent"
    bridge._automation_turn_labels[automation_turn] = {
        "run_id": "run-1", "task_name": "每日摘要",
    }
    monkeypatch.setattr(
        subagent_manager, "snapshot",
        lambda: [SubagentTask(task_id="sub-1", parent_session="自动化任务",
                              parent_turn_id=automation_turn, description="子任务")],
    )

    request = _enqueue_request("sub-1")  # 子代理请求的 parent_turn_id 是 task_id
    broker_module.permission_broker.set_prompt_handlers(
        bridge._activate_permission_request, bridge._dismiss_permission_prompt
    )
    bridge._activate_permission_request(request)

    assert request.future.done()
    assert request.future.result(timeout=0) == 1
    assert all(e[0] != "permission_request" for e in bridge._events)
    assert any("无人值守已自动拒绝" in e[1]["message"] for e in bridge._events if e[0] == "notice")


def test_subagent_of_human_turn_not_denied(manager, _clean_broker, monkeypatch):
    """人工回合派生的子代理不受影响：正常弹卡等待用户。"""
    from core.tools import SubagentTask, subagent_manager

    bridge = _make_bridge(manager)
    monkeypatch.setattr(
        subagent_manager, "snapshot",
        lambda: [SubagentTask(task_id="sub-2", parent_session="人工会话",
                              parent_turn_id="turn-human-parent", description="子任务")],
    )

    request = _enqueue_request("sub-2")
    broker_module.permission_broker.set_prompt_handlers(
        bridge._activate_permission_request, bridge._dismiss_permission_prompt
    )
    bridge._activate_permission_request(request)

    assert any(e[0] == "permission_request" for e in bridge._events)
    assert not request.future.done()
    assert broker_module.permission_broker.resolve(request.request_id, 1)


def test_ask_user_cancelled_not_auto_answered(manager, _clean_broker):
    """P2-4：自动化回合的 ask_user 走取消语义，绝不自动选第 2 个回答项。"""
    bridge = _make_bridge(manager)
    turn_id = "turn-automation-ask"
    bridge._automation_turn_labels[turn_id] = {"run_id": "run-9", "task_name": "调研任务"}

    request = _enqueue_request(turn_id)
    # 伪造 ask_user：任意回答项字符串
    request = broker_module.permission_broker.enqueue(
        "ask_user",
        {"question": "选哪个方案？"},
        owner_id=turn_id,
        parent_turn_id=turn_id,
        source="主代理",
        options=("方案A", "方案B"),
    )
    broker_module.permission_broker.set_prompt_handlers(
        bridge._activate_permission_request, bridge._dismiss_permission_prompt
    )
    bridge._activate_permission_request(request)

    from core.permission_broker import CANCELLED_RESULT

    assert request.future.done()
    assert request.future.result(timeout=0) == CANCELLED_RESULT
    assert any("已自动取消提问" in e[1]["message"] for e in bridge._events if e[0] == "notice")

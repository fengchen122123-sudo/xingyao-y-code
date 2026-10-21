"""用户停止电脑操控不应终止对话回合：抑制状态、通知投递与硬拦截的回归测试。

覆盖点（对应修复「点停止 → 整个对话回合被终止、模型毫不知情」）：
1) stop_for_user 后 cancel_event 未被置位（stop 默认行为保持不变）；
2) 抑制激活后 computer_turn_context_error / start_run 均返回 computer_user_stopped；
3) 待投递通知的 queue/drain 语义；
4) user_message_resumes_control 的正负例（含"通知文案自身不得命中"）。

所有 backend 均为 fake，绝不启动真实浏览器/键鼠。
"""

import threading

import pytest

from core.computer import stop_notice
from core.computer.controller import ComputerControlService
from core.computer.models import (
    ComputerControlError,
    ComputerOwner,
    ComputerRunStatus,
)
from core.computer.settings import ComputerSettingsSnapshot
from core.computer.tool_adapter import ComputerTurnLease, computer_turn_context_error
from tests.computer_fakes import GENERIC_CAPABILITY, FakeComputerBackend


@pytest.fixture(autouse=True)
def _clean_stop_state():
    """抑制态是进程级模块变量：每个用例前后都必须清干净，避免相互污染。"""
    stop_notice.deactivate_user_stop("test_setup")
    yield
    stop_notice.deactivate_user_stop("test_teardown")


@pytest.fixture
def stop_ctx():
    """最小 controller：settings 全开、backend 走 fake，并记录 abort 调用。"""
    abort_calls = []

    def factory(target, identity):
        return FakeComputerBackend(target=target)

    def loader():
        return ComputerSettingsSnapshot(
            browser_control_enabled=True,
            computer_control_enabled=True,
            computer_browser="edge",
            browser_options=(),
            profile_display_path="",
            platform_supported=True,
            generation=1,
        )

    service = ComputerControlService(
        backend_factory=factory,
        settings_loader=loader,
        generation_provider=lambda: 1,
        abort_streams=abort_calls.append,
    )
    owner = ComputerOwner(
        session_id="session-stop",
        turn_id="turn-stop",
        session_label="停止测试",
        cancel_event=threading.Event(),
    )
    return service, owner, abort_calls


def test_stop_for_user_does_not_set_turn_cancel_event(stop_ctx):
    """stop_for_user 复用 stop 清理链，但绝不置位回合 cancel_event / abort 在飞流。"""
    service, owner, abort_calls = stop_ctx
    run_id = service.start_run(owner, GENERIC_CAPABILITY).run_id

    snapshot = service.stop_for_user(run_id, "user_stop")

    assert snapshot.status == ComputerRunStatus.STOPPED.value
    assert owner.cancel_event.is_set() is False
    assert abort_calls == []
    assert stop_notice.is_user_stop_active() is True
    # 抑制态同时排了一条模型可见通知，供回合下个 Step 边界投递。
    assert stop_notice.drain_stop_notices() == [stop_notice.STOP_NOTICE_TEXT]


def test_stop_default_still_sets_cancel_event(stop_ctx):
    """回归保护：application_exit/emergency 等仍走 cancel_owner=True 的默认行为。"""
    service, owner, abort_calls = stop_ctx
    run_id = service.start_run(owner, GENERIC_CAPABILITY).run_id

    service.stop(run_id, "application_exit")

    assert owner.cancel_event.is_set() is True
    assert abort_calls == [owner.cancel_event]


def test_turn_context_error_blocks_after_user_stop(stop_ctx):
    """抑制激活后 computer_action 的上下文校验直接返回 computer_user_stopped。"""
    service, owner, _ = stop_ctx
    run_id = service.start_run(owner, GENERIC_CAPABILITY).run_id
    service.stop_for_user(run_id, "user_stop")
    lease = ComputerTurnLease(GENERIC_CAPABILITY, owner, service, run_id)

    error = computer_turn_context_error(lease)

    assert isinstance(error, ComputerControlError)
    assert error.code == "computer_user_stopped"
    assert "用户已停止电脑操控" in error.message


def test_start_run_rejected_after_user_stop(stop_ctx):
    """start_run 也要挡住：否则模型换个 target 就能绕开上下文校验。"""
    service, owner, _ = stop_ctx
    run_id = service.start_run(owner, GENERIC_CAPABILITY).run_id
    service.stop_for_user(run_id, "user_stop")
    new_owner = ComputerOwner(
        session_id="session-stop",
        turn_id="turn-stop-2",
        session_label="停止测试2",
        cancel_event=threading.Event(),
    )

    with pytest.raises(ComputerControlError) as excinfo:
        service.start_run(new_owner, GENERIC_CAPABILITY)

    assert excinfo.value.code == "computer_user_stopped"


def test_queue_and_drain_stop_notices():
    """待投递通知：drain 取空、同 reason 去重、文案固定。"""
    assert stop_notice.drain_stop_notices() == []

    stop_notice.queue_stop_notice("user_stop")
    stop_notice.queue_stop_notice("user_stop")
    notices = stop_notice.drain_stop_notices()

    assert notices == [stop_notice.STOP_NOTICE_TEXT]
    assert stop_notice.drain_stop_notices() == []


def test_deactivate_clears_pending_notices():
    """用户要求恢复后，未投递的"别再操作"通知必须一起丢弃，否则自相矛盾。"""
    stop_notice.activate_user_stop("user_stop")
    stop_notice.queue_stop_notice("user_stop")

    stop_notice.deactivate_user_stop("settings_reenabled")

    assert stop_notice.is_user_stop_active() is False
    assert stop_notice.drain_stop_notices() == []


@pytest.mark.parametrize(
    "text",
    [
        "好的，恢复操控吧",
        "继续操作电脑",
        "再帮我操作一下那个网页",
        "帮我点一下提交按钮",
        "用鼠标点一下右上角",
    ],
)
def test_user_message_resumes_control_positive(text):
    assert stop_notice.user_message_resumes_control(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "",
        "今天天气怎么样",
        "把刚才的测试报告总结一下",
        # 通知文案自身绝不能命中恢复关键词：它可能是最后一条 user 消息，
        # 否则抑制态会被自己投递的通知解除。
        stop_notice.STOP_NOTICE_TEXT,
    ],
)
def test_user_message_resumes_control_negative(text):
    assert stop_notice.user_message_resumes_control(text) is False

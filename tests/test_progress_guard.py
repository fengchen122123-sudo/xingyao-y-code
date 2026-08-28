# -*- coding: utf-8 -*-
"""TurnProgressGuard 纯单元测试：确定性进展观察与停滞熔断。"""
import pytest

from core.harness.progress_guard import ProgressObservation, TurnProgressGuard


def _obs(task="t0", plan="p0", result="r0", mutation=False):
    return ProgressObservation(
        task_snapshots_sha256=task,
        plan_snapshot_sha256=plan,
        tool_results_sha256=result,
        mutation_committed=mutation,
    )


def test_first_observation_without_mutation_counts_as_first_stall():
    # 首次观察无基线可比、无 mutation：无法证明进展，保守计入停滞序列，
    # 保证"连续 6 个无进展 Step 后第 7 次 Provider 请求不发送"（spec 验收 4）
    guard = TurnProgressGuard()
    assert guard.observe(_obs()) is False
    assert guard.stalled_steps == 1
    assert guard.should_abort() is False


def test_first_observation_with_mutation_is_progress():
    guard = TurnProgressGuard()
    assert guard.observe(_obs(mutation=True)) is True
    assert guard.stalled_steps == 0


def test_same_observation_counts_stall():
    guard = TurnProgressGuard()
    guard.observe(_obs())
    assert guard.observe(_obs()) is False
    assert guard.stalled_steps == 2
    assert guard.should_abort() is False


def test_task_hash_change_clears_stall():
    guard = TurnProgressGuard()
    guard.observe(_obs())
    guard.observe(_obs())
    assert guard.stalled_steps == 2
    assert guard.observe(_obs(task="t1")) is True
    assert guard.stalled_steps == 0


def test_plan_hash_change_clears_stall():
    guard = TurnProgressGuard()
    guard.observe(_obs())
    guard.observe(_obs())
    assert guard.observe(_obs(plan="p1")) is True
    assert guard.stalled_steps == 0


def test_result_hash_change_clears_stall():
    guard = TurnProgressGuard()
    guard.observe(_obs())
    guard.observe(_obs())
    assert guard.observe(_obs(result="r1")) is True
    assert guard.stalled_steps == 0


def test_mutation_clears_stall():
    guard = TurnProgressGuard()
    guard.observe(_obs())
    guard.observe(_obs())
    assert guard.stalled_steps == 2
    assert guard.observe(_obs(mutation=True)) is True
    assert guard.stalled_steps == 0


def test_abort_at_max_stalled_steps():
    # 连续 6 个 Step 状态完全不变 → 熔断（spec 测试矩阵）
    guard = TurnProgressGuard()
    for expected in range(1, TurnProgressGuard.MAX_STALLED_STEPS):
        assert guard.observe(_obs()) is False
        assert guard.stalled_steps == expected
        assert guard.should_abort() is False
    assert guard.observe(_obs()) is False
    assert guard.stalled_steps == TurnProgressGuard.MAX_STALLED_STEPS
    assert guard.should_abort() is True


def test_progress_after_stalls_resets_abort_countdown():
    guard = TurnProgressGuard()
    for _ in range(TurnProgressGuard.MAX_STALLED_STEPS - 1):
        guard.observe(_obs())
    assert guard.should_abort() is False
    # 第 6 步前出现真实变化 → 计数清零，熔断倒计时重新开始
    assert guard.observe(_obs(result="r-progress")) is True
    assert guard.stalled_steps == 0
    for _ in range(TurnProgressGuard.MAX_STALLED_STEPS - 1):
        guard.observe(_obs(result="r-progress"))
    assert guard.should_abort() is False
    guard.observe(_obs(result="r-progress"))
    assert guard.should_abort() is True


def test_max_stalled_steps_is_six():
    assert TurnProgressGuard.MAX_STALLED_STEPS == 6

# -*- coding: utf-8 -*-
"""TurnProgressGuard 纯单元测试：确定性进展观察与停滞熔断。"""
import pytest

from core.harness.progress_guard import ProgressObservation, TurnProgressGuard


def _obs(task="t0", plan="p0", result="r0", mutation=False, read_signatures=()):
    return ProgressObservation(
        task_snapshots_sha256=task,
        plan_snapshot_sha256=plan,
        tool_results_sha256=result,
        mutation_committed=mutation,
        read_signatures=tuple(read_signatures),
    )


def test_first_observation_without_mutation_counts_as_first_stall():
    # 首次观察无基线可比、无 mutation：无法证明进展，保守计入停滞序列，
    # 保证连续达到阈值的无进展 Step 后不再发送下一次 Provider 请求。
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
    # 连续达到阈值的 Step 状态完全不变 → 熔断。
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
    # 达到阈值前出现真实变化 → 计数清零，熔断倒计时重新开始。
    assert guard.observe(_obs(result="r-progress")) is True
    assert guard.stalled_steps == 0
    for _ in range(TurnProgressGuard.MAX_STALLED_STEPS - 1):
        guard.observe(_obs(result="r-progress"))
    assert guard.should_abort() is False
    guard.observe(_obs(result="r-progress"))
    assert guard.should_abort() is True


def test_general_no_progress_aborts_on_twelfth_step():
    guard = TurnProgressGuard()
    for _ in range(11):
        assert guard.observe(_obs()) is False
        assert guard.should_abort() is False
    assert guard.observe(_obs()) is False
    assert guard.should_abort() is True
# ── 全回合通用有界读取进展预算（接管核查期与普通会话共用同一机制） ──


def test_plain_session_read_only_chain_no_longer_aborts():
    """无计划普通会话的只读调查链：每步新读取目标算进展，不再被熔断。"""
    from hashlib import sha256

    guard = TurnProgressGuard()
    for index in range(10):
        obs = _obs(
            read_signatures=(
                sha256(f"read_file:file{index}.py".encode()).hexdigest(),
            ),
        )
        assert guard.observe(obs) is True
    assert guard.should_abort() is False


def test_repeated_reads_do_not_count():
    """重复读取同一目标不算进展（防复制粘贴式空转绕过熔断）。"""
    from hashlib import sha256

    guard = TurnProgressGuard()
    signature = sha256(b"read_file:same.py").hexdigest()
    assert guard.observe(_obs(read_signatures=(signature,))) is True
    for _ in range(TurnProgressGuard.MAX_STALLED_STEPS + 1):
        assert guard.observe(_obs(read_signatures=(signature,))) is False
    assert guard.should_abort() is True


def test_read_budget_bounded_and_aborts_after_exhaustion():
    """读取预算有界：耗尽后新读取目标不再算进展，状态无变化仍会熔断。"""
    from hashlib import sha256

    guard = TurnProgressGuard()
    for index in range(TurnProgressGuard.MAX_UNIQUE_READS):
        obs = _obs(
            read_signatures=(
                sha256(f"read_file:f{index}.py".encode()).hexdigest(),
            ),
        )
        assert guard.observe(obs) is True
    beyond = _obs(
        read_signatures=(
            sha256("read_file:beyond.py".encode()).hexdigest(),
        ),
    )
    assert guard.observe(beyond) is False
    # 预算耗尽且状态无变化继续堆停滞 → 仍会熔断（防无限换路径绕过）
    for _ in range(TurnProgressGuard.MAX_STALLED_STEPS - 1):
        guard.observe(_obs())
    assert guard.should_abort() is True


def test_read_budget_resets_on_real_progress():
    """真实进展（修改提交/状态哈希变化）后，读取预算重置为满额。"""
    from hashlib import sha256

    guard = TurnProgressGuard()
    for index in range(TurnProgressGuard.MAX_UNIQUE_READS):
        guard.observe(_obs(
            read_signatures=(
                sha256(f"read_file:f{index}.py".encode()).hexdigest(),
            ),
        ))
    assert guard.observe(_obs(mutation=True)) is True
    assert guard.observe(_obs(
        read_signatures=(
            sha256("read_file:fresh.py".encode()).hexdigest(),
        ),
    )) is True


def test_read_budget_resets_on_state_hash_change():
    """状态哈希变化（如后台任务推进/计划变更）同样重置读取预算。

    回归背景：此前只在 mutation 提交时清空预算，长回合里后台任务持续推进
    （每步哈希变化）但读取预算耗尽后，新读取目标仍被判停滞，最终误熔断。
    """
    from hashlib import sha256

    guard = TurnProgressGuard()
    for index in range(TurnProgressGuard.MAX_UNIQUE_READS):
        guard.observe(_obs(
            read_signatures=(
                sha256(f"read_file:f{index}.py".encode()).hexdigest(),
            ),
        ))
    # 预算耗尽后一次状态哈希变化（无 mutation）→ 新读取目标重新算进展
    assert guard.observe(_obs(task="t0-changed")) is True
    assert guard.observe(_obs(
        read_signatures=(
            sha256("read_file:fresh-after-hash.py".encode()).hexdigest(),
        ),
    )) is True


def test_mutation_takes_precedence_over_exhausted_read_budget():
    """预算耗尽后修改提交仍然算进展（预算只限制读取这条通道）。"""
    from hashlib import sha256

    guard = TurnProgressGuard()
    for index in range(TurnProgressGuard.MAX_UNIQUE_READS):
        guard.observe(_obs(
            read_signatures=(
                sha256(f"read_file:f{index}.py".encode()).hexdigest(),
            ),
        ))
    assert guard.observe(_obs(mutation=True)) is True
    assert guard.stalled_steps == 0


def test_max_unique_reads_budget_is_calibrated():
    # 真实调查回合（读文件+搜索）单回合可达 20-40 个不同目标，预算必须留足
    # 余量，否则合法只读调查会在首次写入前被误熔断。
    assert TurnProgressGuard.MAX_UNIQUE_READS == 48

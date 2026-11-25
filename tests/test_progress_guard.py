# -*- coding: utf-8 -*-

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

def test_plain_session_read_only_chain_no_longer_aborts():
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
    from hashlib import sha256

    guard = TurnProgressGuard()
    signature = sha256(b"read_file:same.py").hexdigest()
    assert guard.observe(_obs(read_signatures=(signature,))) is True
    for _ in range(TurnProgressGuard.MAX_STALLED_STEPS + 1):
        assert guard.observe(_obs(read_signatures=(signature,))) is False
    assert guard.should_abort() is True


def test_read_budget_bounded_and_aborts_after_exhaustion():
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
    for _ in range(TurnProgressGuard.MAX_STALLED_STEPS - 1):
        guard.observe(_obs())
    assert guard.should_abort() is True


def test_read_budget_resets_on_real_progress():
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
    from hashlib import sha256

    guard = TurnProgressGuard()
    for index in range(TurnProgressGuard.MAX_UNIQUE_READS):
        guard.observe(_obs(
            read_signatures=(
                sha256(f"read_file:f{index}.py".encode()).hexdigest(),
            ),
        ))
    assert guard.observe(_obs(task="t0-changed")) is True
    assert guard.observe(_obs(
        read_signatures=(
            sha256("read_file:fresh-after-hash.py".encode()).hexdigest(),
        ),
    )) is True


def test_mutation_takes_precedence_over_exhausted_read_budget():
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


def test_subagent_read_only_review_survives_past_default_budget():
    """只读审查子任务：超过默认 48 个目标后仍必须继续计进展。

    回归场景：consistency-checker（base_type=review，只有只读工具）审
    第 001-115 章正文，必然超过 48 个唯一读取目标；预算不放宽就会在收敛
    出结论前熔断成 NoProgressError。
    """
    from hashlib import sha256

    guard = TurnProgressGuard(max_unique_reads=512)
    for index in range(300):
        obs = _obs(
            read_signatures=(
                sha256(f"read_file:正文/第{index:03d}章.md".encode()).hexdigest(),
            ),
        )
        assert guard.observe(obs) is True
    assert guard.stalled_steps == 0
    assert guard.should_abort() is False


def test_explicit_read_budget_still_bounded():
    """放宽预算不等于取消熔断：预算耗尽后仍按停滞计数中止。"""
    from hashlib import sha256

    guard = TurnProgressGuard(max_unique_reads=10)
    for index in range(10):
        assert guard.observe(_obs(
            read_signatures=(
                sha256(f"read_file:f{index}.py".encode()).hexdigest(),
            ),
        )) is True
    assert guard.observe(_obs(
        read_signatures=(
            sha256("read_file:beyond.py".encode()).hexdigest(),
        ),
    )) is False
    for _ in range(TurnProgressGuard.MAX_STALLED_STEPS - 1):
        guard.observe(_obs())
    assert guard.should_abort() is True


def test_invalid_read_budget_falls_back_to_default():
    assert TurnProgressGuard()._max_unique_reads == TurnProgressGuard.MAX_UNIQUE_READS
    assert TurnProgressGuard(max_unique_reads=None)._max_unique_reads == (
        TurnProgressGuard.MAX_UNIQUE_READS
    )
    for bad in (0, -5, "bad"):
        assert TurnProgressGuard(max_unique_reads=bad)._max_unique_reads == (
            TurnProgressGuard.MAX_UNIQUE_READS
        )
    assert TurnProgressGuard(max_unique_reads=200)._max_unique_reads == 200

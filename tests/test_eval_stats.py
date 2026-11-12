"""配对评测统计层（core/eval_stats.py）单元测试。

只测纯计算：配对、去噪（blocked/saturated/flaky）、bootstrap 区间、门禁退出码。
"""

from core.eval_stats import (
    ArmRun,
    CasePair,
    bootstrap_ci,
    gate_exit_code,
    group_runs,
    summarize,
)


def _runs(case, profile, passed_list):
    return [ArmRun(case, profile, index, passed) for index, passed in enumerate(passed_list)]


def test_group_runs_pairs_by_case_and_ignores_other_profiles():
    runs = (
        _runs("c1", "A", [True, True, True])
        + _runs("c1", "B", [False, False, False])
        + _runs("c2", "A", [True])
        + _runs("c2", "C", [True])  # 第三个 profile 忽略
    )
    pairs = group_runs(runs, "A", "B")
    assert [pair.case_id for pair in pairs] == ["c1", "c2"]
    assert pairs[0].a_rate == 1.0
    assert pairs[0].b_rate == 0.0
    assert pairs[1].blocked is True


def test_saturated_when_both_sides_all_pass():
    pair = CasePair("c1", [True, True], [True, True])
    assert pair.saturated is True
    assert pair.informative is False


def test_flaky_when_one_side_is_mixed():
    pair = CasePair("c1", [True, False], [True, True])
    assert pair.flaky is True
    assert pair.saturated is False
    assert pair.informative is False


def test_single_run_is_never_flaky():
    pair = CasePair("c1", [True], [False])
    assert pair.flaky is False
    assert pair.informative is True
    assert pair.delta == 1.0


def test_summarize_computes_mean_delta_over_informative_cases():
    pairs = [
        CasePair("c1", [True], [False]),           # delta +1
        CasePair("c2", [False], [True]),           # delta -1
        CasePair("c3", [True, True], [True, True]),  # saturated
        CasePair("c4", [True], []),                # blocked
        CasePair("c5", [True], [False]),           # delta +1
    ]
    summary = summarize(pairs)
    assert summary["paired"] == 5
    assert summary["saturated"] == 1
    assert summary["blocked"] == 1
    assert summary["informative"] == 3
    # blocked = 1/5 = 20%，未超过阈值 → 结论成立
    assert summary["verdict"] == "ok"
    assert round(summary["delta"], 3) == 0.333
    assert summary["cases"]["c3"]["status"] == "saturated"
    assert summary["cases"]["c4"]["status"] == "blocked"


def test_summarize_blocks_when_blocked_ratio_exceeds_limit():
    pairs = [CasePair("c1", [True], [False]), CasePair("c2", [True], [])]
    summary = summarize(pairs)
    assert summary["verdict"] == "inconclusive"
    assert "blocked" in summary["reason"]
    assert gate_exit_code(summary) == 2


def test_summarize_inconclusive_without_informative_cases():
    pairs = [CasePair("c1", [True], [True]), CasePair("c2", [False], [False])]
    summary = summarize(pairs)
    assert summary["verdict"] == "inconclusive"
    assert summary["delta"] == 0.0


def test_summarize_empty_is_inconclusive():
    summary = summarize([])
    assert summary["verdict"] == "inconclusive"
    assert gate_exit_code(summary) == 2


def test_gate_exit_code_zero_when_ok():
    assert gate_exit_code({"verdict": "ok"}) == 0


def test_bootstrap_ci_is_deterministic_and_brackets_mean():
    values = [1.0, 0.0, 1.0, 0.0, 1.0]
    first = bootstrap_ci(values, iterations=500)
    second = bootstrap_ci(values, iterations=500)
    assert first == second
    low, high = first
    mean = sum(values) / len(values)
    assert low <= mean <= high


def test_bootstrap_ci_empty_returns_none():
    assert bootstrap_ci([]) is None
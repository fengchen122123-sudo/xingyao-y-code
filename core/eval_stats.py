"""配对评测的统计层（S2）。

设计见 docs/2026-11-12-pi-inspired-optimization-design.md 第 5 节。

本模块只做**纯计算**，不跑模型、不碰文件系统，因此可以完全离线单测：

- 配对：同一个 case 在 A/B 两个 profile 上都跑完，才算一对。
- 只统计「有信息量」的 case：两侧全通过或全失败（saturated）、同侧多次结果
  不一致（flaky）、或一侧缺失（blocked）都不进 headline。
- delta = mean(A 通过率 − B 通过率)，置信区间用 bootstrap（标准库实现）。
- 结论不成立时由调用方按 gate 退出码处理：宁可不出结论，也不出被污染的结论。
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Sequence

# 单侧 N 次结果里只要出现混合，就认为该 case 不稳定
FLAKY_MIN_MIXED_RUNS = 2
# blocked 比例超过该阈值 → 整份报告结论不成立
BLOCKED_RATIO_LIMIT = 0.2
# bootstrap 重采样次数与默认置信水平
DEFAULT_BOOTSTRAP_ITERATIONS = 2000
DEFAULT_CONFIDENCE = 0.95


@dataclass(frozen=True)
class ArmRun:
    """一次 arm 执行结果：哪个 case、哪个 profile、第几次、是否通过。"""

    case_id: str
    profile: str
    run: int
    passed: bool


@dataclass
class CasePair:
    """一个 case 的配对结果。"""

    case_id: str
    a_runs: list[bool] = field(default_factory=list)
    b_runs: list[bool] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return not self.a_runs or not self.b_runs

    @property
    def a_rate(self) -> float:
        return sum(self.a_runs) / len(self.a_runs) if self.a_runs else 0.0

    @property
    def b_rate(self) -> float:
        return sum(self.b_runs) / len(self.b_runs) if self.b_runs else 0.0

    @property
    def saturated(self) -> bool:
        """两侧都在同一极端（全过或全挂）→ 没有区分度。"""
        if self.blocked:
            return False
        return (self.a_rate in (0.0, 1.0)) and (self.a_rate == self.b_rate)

    @property
    def flaky(self) -> bool:
        """任一侧多次结果不一致 → 该 case 不可靠。"""
        return _is_mixed(self.a_runs) or _is_mixed(self.b_runs)

    @property
    def informative(self) -> bool:
        return not (self.blocked or self.saturated or self.flaky)

    @property
    def delta(self) -> float:
        return self.a_rate - self.b_rate


def _is_mixed(runs: Sequence[bool]) -> bool:
    if len(runs) < FLAKY_MIN_MIXED_RUNS:
        return False
    return len(set(runs)) > 1


def group_runs(runs: Iterable[ArmRun], profile_a: str, profile_b: str) -> list[CasePair]:
    """按 case 归并两个 profile 的执行结果（其它 profile 忽略）。"""
    pairs: dict[str, CasePair] = {}
    for run in runs:
        if run.profile not in (profile_a, profile_b):
            continue
        pair = pairs.setdefault(run.case_id, CasePair(run.case_id))
        (pair.a_runs if run.profile == profile_a else pair.b_runs).append(bool(run.passed))
    for pair in pairs.values():
        pair.a_runs.sort()
        pair.b_runs.sort()
        # sort 只为让输出确定；布尔列表本身就稳定
    return [pairs[k] for k in sorted(pairs)]


def bootstrap_ci(
    values: Sequence[float],
    *,
    iterations: int = DEFAULT_BOOTSTRAP_ITERATIONS,
    confidence: float = DEFAULT_CONFIDENCE,
    seed: int = 20261112,
) -> tuple[float, float] | None:
    """对样本均值做 percentile bootstrap，返回 (低, 高)；样本为空返回 None。

    固定随机种子：同一批数据必须给出同一区间，否则报告不可复现。
    """
    if not values:
        return None
    rng = random.Random(seed)
    n = len(values)
    means = []
    for _ in range(max(1, int(iterations))):
        total = 0.0
        for _ in range(n):
            total += values[rng.randrange(n)]
        means.append(total / n)
    means.sort()
    alpha = max(0.0, min(1.0, (1.0 - confidence) / 2.0))
    low_index = int(alpha * (len(means) - 1))
    high_index = int((1.0 - alpha) * (len(means) - 1))
    return means[low_index], means[high_index]


def summarize(pairs: Sequence[CasePair]) -> dict:
    """汇总配对结果。

    返回字段：
    - paired：参与配对的 case 数（含 blocked / saturated / flaky）
    - blocked / saturated / flaky：各自计数
    - informative：真正进入结论的 case 数
    - delta：informative case 的平均 delta（正数表示 A 更好）
    - ci：bootstrap 置信区间（None 表示样本不足）
    - verdict：ok / inconclusive（blocked 超阈值或没有 informative case）
    """
    total = len(pairs)
    blocked = [p for p in pairs if p.blocked]
    saturated = [p for p in pairs if not p.blocked and p.saturated]
    flaky = [p for p in pairs if not p.blocked and not p.saturated and p.flaky]
    informative = [p for p in pairs if p.informative]

    deltas = [p.delta for p in informative]
    blocked_ratio = (len(blocked) / total) if total else 0.0

    verdict = "ok"
    reason = ""
    if total == 0:
        verdict, reason = "inconclusive", "没有可配对的结果"
    elif blocked_ratio > BLOCKED_RATIO_LIMIT:
        verdict, reason = "inconclusive", f"blocked 比例 {blocked_ratio:.0%} 超过阈值"
    elif not informative:
        verdict, reason = "inconclusive", "没有有信息量的 case"

    return {
        "paired": total,
        "blocked": len(blocked),
        "saturated": len(saturated),
        "flaky": len(flaky),
        "informative": len(informative),
        "delta": (sum(deltas) / len(deltas)) if deltas else 0.0,
        "ci": bootstrap_ci(deltas),
        "verdict": verdict,
        "reason": reason,
        "cases": {
            p.case_id: {
                "a_rate": round(p.a_rate, 3),
                "b_rate": round(p.b_rate, 3),
                "delta": round(p.delta, 3),
                "status": (
                    "blocked" if p.blocked
                    else "saturated" if p.saturated
                    else "flaky" if p.flaky
                    else "informative"
                ),
            }
            for p in pairs
        },
    }


def gate_exit_code(summary: Mapping) -> int:
    """门禁退出码：0 = 结论成立；2 = 结论不成立（不要拿它当"没提升"）。"""
    return 0 if summary.get("verdict") == "ok" else 2
"""确定性 Turn 进展观察：只比较状态哈希与修改类提交标记，不读模型自然语言。"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProgressObservation:
    task_snapshots_sha256: str
    plan_snapshot_sha256: str
    tool_results_sha256: str
    mutation_committed: bool


class TurnProgressGuard:
    MAX_STALLED_STEPS = 6

    def __init__(self) -> None:
        self._baseline: ProgressObservation | None = None
        self._stalled_steps = 0

    def observe(self, observation: ProgressObservation) -> bool:
        """记录一步观察；返回本步是否产生真实进展。

        进展 = 修改类提交，或任一状态哈希相对上一步发生变化。首次观察没有
        可比较的前态，无法证明进展，保守计入停滞序列——保证连续
        MAX_STALLED_STEPS 个无进展 Step 后不再发送下一次 Provider 请求。
        """
        progressed = observation.mutation_committed or (
            self._baseline is not None
            and (
                observation.task_snapshots_sha256
                != self._baseline.task_snapshots_sha256
                or observation.plan_snapshot_sha256
                != self._baseline.plan_snapshot_sha256
                or observation.tool_results_sha256
                != self._baseline.tool_results_sha256
            )
        )
        if progressed:
            self._stalled_steps = 0
        else:
            self._stalled_steps += 1
        self._baseline = observation
        return progressed

    @property
    def stalled_steps(self) -> int:
        return self._stalled_steps

    def should_abort(self) -> bool:
        return self._stalled_steps >= self.MAX_STALLED_STEPS

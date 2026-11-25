from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProgressObservation:
    task_snapshots_sha256: str
    plan_snapshot_sha256: str
    tool_results_sha256: str
    mutation_committed: bool
    # 本步成功只读工具的读取签名（tool name + canonical arguments 哈希）。
    # 由观察方计算，guard 负责有界计数；签名仅存当前 Turn 内存，不写 Journal。
    read_signatures: tuple[str, ...] = ()


class TurnProgressGuard:
    MAX_STALLED_STEPS = 12
    # 每回合允许计为进展的新读取目标上限：超过后不再自动重置停滞计数，
    # 防止模型通过无限更换读取路径绕过熔断。真实进展（修改提交或状态哈希
    # 变化）会重置预算为满额。取值按真实调查回合校准：大型代码库的纯只读
    # 调查（读文件+搜索）单回合可涉及 20-40 个不同目标，预算必须留足余量，
    # 否则合法调查会在首次写入前被误杀。
    MAX_UNIQUE_READS = 48

    def __init__(self, max_unique_reads: int | None = None) -> None:
        self._max_unique_reads = self._resolve_read_budget(max_unique_reads)
        self._baseline: ProgressObservation | None = None
        self._stalled_steps = 0
        self._seen_reads: set[str] = set()

    @classmethod
    def _resolve_read_budget(cls, value: int | None) -> int:
        """把调用方传入的只读预算归一为合法正整数，非法值回落默认。

        交互回合（主 Agent）沿用 MAX_UNIQUE_READS；一次性子代理任务——尤其
        只读审查（逐章读正文 + 设定 + 追踪文件，且没有写工具可产生 mutation）
        ——读取目标数天然远超交互回合，必须由调用方显式放宽，否则合法审查会在
        收敛出结论前被误判为停滞并熔断（NoProgressError）。
        """
        if value is None:
            return cls.MAX_UNIQUE_READS
        try:
            budget = int(value)
        except (TypeError, ValueError):
            return cls.MAX_UNIQUE_READS
        return budget if budget > 0 else cls.MAX_UNIQUE_READS

    def observe(self, observation: ProgressObservation) -> bool:
        hash_changed = (
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
        progressed = observation.mutation_committed or hash_changed
        if progressed:
            self._seen_reads.clear()
        if observation.read_signatures:
            for signature in observation.read_signatures:
                if len(self._seen_reads) >= self._max_unique_reads:
                    break
                if signature not in self._seen_reads:
                    self._seen_reads.add(signature)
                    progressed = True
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

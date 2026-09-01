"""确定性 Turn 进展观察：只比较状态哈希与修改类提交标记，不读模型自然语言。"""
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

    def __init__(self) -> None:
        self._baseline: ProgressObservation | None = None
        self._stalled_steps = 0
        self._seen_reads: set[str] = set()

    def observe(self, observation: ProgressObservation) -> bool:
        """记录一步观察；返回本步是否产生真实进展。

        进展 = 修改类提交，或任一状态哈希相对上一步发生变化，或本步出现
        预算内的新读取签名。首次观察没有可比较的前态，无法证明进展，保守
        计入停滞序列——保证连续 MAX_STALLED_STEPS 个无进展 Step 后不再
        发送下一次 Provider 请求。

        读取签名只算"有界进展"：每回合最多 MAX_UNIQUE_READS 个新目标，
        重复签名与超限后的新签名一律不算；修改提交或状态哈希变化这类
        真实进展会把预算重置为满额。这样普通只读调查链（读新文件、搜新
        代码）不再被误判为停滞，而复制粘贴式空转与无限换路径仍会熔断。
        """
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
            # 真实进展（修改提交或状态哈希变化）重置读取预算：新一轮调查从头计数
            self._seen_reads.clear()
        if observation.read_signatures:
            for signature in observation.read_signatures:
                if len(self._seen_reads) >= self.MAX_UNIQUE_READS:
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

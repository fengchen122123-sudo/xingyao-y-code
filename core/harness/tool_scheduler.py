"""拓扑感知的工具批次规划（「面向 ZCode 能力借鉴」工作包 A5，规格 03 §2.2）。

为什么单独成模块（而不是继续留在 tool_pipeline.py）：
1. 调度算法（依赖分层 + 并行分组）与执行管线职责不同；独立后可以对拍与
   单元测试（tests/test_tool_scheduler.py），也便于二期"自动拓扑重排"
   只改这一个文件；
2. 执行层接口保持 ``tuple[ToolBatch, ...]`` 不变（规格 2.6），调用方零改动。

异常类型选择：规格原文写"抛 CoreError"，但全仓搜索确认项目内不存在该类型
（core/harness/models.py 的 InvalidHarnessTransition 只用于 harness 状态机
转移，语义不符）。依赖声明非法属于"调用方给出的 ToolSpec 契约 / 调用序列
非法"，与项目内工具参数校验、注册表参数校验（均用 ValueError）最贴近，
故统一使用 ValueError，不引入新的公共异常类型。
"""

from __future__ import annotations

from core.harness.tool_pipeline import ToolBatch
from core.tool_registry import ToolRegistry


def plan_tool_batches(
    tool_calls: list[dict],
    registry: ToolRegistry,
    *,
    max_concurrency: int = 4,
) -> tuple[ToolBatch, ...]:
    """按依赖拓扑分层 + 并行安全元数据分组。

    ===== 兼容性契约（最高优先级）=====
    对「所有 depends_on 均为 None」的输入，本函数在 max_concurrency 大到
    不触发截断时，输出与旧实现 _legacy_group_tool_batches() 逐批、逐项、
    逐顺序完全一致（tests/test_tool_scheduler.py 有 1000 用例级对拍守护）。

    ===== 算法 =====
    1. 计算每个调用的层号：depends_on 为 None/空 → 层号 0；否则 →
       max(依赖调用层号) + 1（依赖序号必须小于自身序号，不支持前向依赖）。
    2. 按层号升序处理，每层内保持原始相对顺序：
       parallel_safe=True 的累积到当前并行批；parallel_safe=False 的先
       flush 当前并行批、再单独成批；并行批达到 max_concurrency 时 flush。
    3. 跨层不合并：层 N 的批次必须全部先于层 N+1。

    为什么不自动重排（规格 1.4 非目标）：模型输出的工具调用顺序隐含了
    意图顺序（例如"先写后读"验证），v1 只做不改变相对顺序的优化。

    max_concurrency 语义：单批调用数上限（与执行侧 BoundedSemaphore 额度
    对齐，避免批内排队 future 长期占位）。旧实现没有该上限，因此
    group_tool_batches() 兼容包装会传入「不截断」的值以保持零回归，
    真实并发上限仍由 ToolBatchExecutor 的 max_parallel=4 执行期兜底。
    """
    if isinstance(max_concurrency, bool) or not isinstance(max_concurrency, int):
        raise ValueError("max_concurrency 必须是整数")
    if max_concurrency < 1:
        raise ValueError("max_concurrency 必须为正整数")

    calls = list(tool_calls)
    count = len(calls)

    parallel_flags: list[bool] = []
    dependencies: list[tuple[int, ...] | None] = []
    for tool_call in calls:
        name = tool_call["function"]["name"]
        try:
            spec = registry.get(name)
        except KeyError:
            # 与旧实现一致：未注册的工具按「非并行安全」保守处理。
            parallel_flags.append(False)
            dependencies.append(None)
            continue
        parallel_flags.append(bool(getattr(spec, "parallel_safe", False)))
        declared = getattr(spec, "depends_on", None)
        if declared is None:
            dependencies.append(None)
            continue
        try:
            normalized = tuple(declared)
        except TypeError as error:
            raise ValueError(
                f"工具 {name} 的 depends_on 必须是调用序号元组"
            ) from error
        # 空元组等于显式声明「无依赖」，与 None 同效（规格 2.1）。
        dependencies.append(normalized or None)

    # 依赖成环检查放在序号校验之前：自我依赖与相互依赖（0→1、1→0）报"成环"
    # 比报"前向依赖"更贴近真实问题；越界/非整数引用交给 _resolve_layers 报错。
    _assert_acyclic(dependencies, count)
    layers = _resolve_layers(dependencies, count)

    per_layer: dict[int, list[int]] = {}
    for index, layer in enumerate(layers):
        per_layer.setdefault(layer, []).append(index)

    planned: list[tuple[bool, list[int]]] = []
    pending_parallel: list[int] = []

    def flush_parallel() -> None:
        if pending_parallel:
            planned.append((True, list(pending_parallel)))
            pending_parallel.clear()

    for layer in sorted(per_layer):
        for index in per_layer[layer]:
            if parallel_flags[index]:
                pending_parallel.append(index)
                if len(pending_parallel) >= max_concurrency:
                    flush_parallel()
            else:
                # 非并行安全工具必须独占一批：先 flush 已累积的并行批，
                # 保证它既不与其它调用并发，也不越过后续调用。
                flush_parallel()
                planned.append((False, [index]))
        # 跨层不合并：层 N 的所有批次都必须先于层 N+1。
        flush_parallel()

    _assert_batches_respect_dependencies(planned, dependencies)
    return tuple(
        ToolBatch(parallel, tuple(calls[index] for index in indices))
        for parallel, indices in planned
    )


def _resolve_layers(
    dependencies: list[tuple[int, ...] | None],
    count: int,
) -> list[int]:
    """把依赖序号折算成层号，并校验依赖引用的合法性。

    为什么拒绝前向依赖（dep >= index）：v1 不重排调用顺序（规格 1.4），
    前向依赖意味着必须重排才能满足，属于不支持但必须显式爆响的输入；
    该约束同时保证依赖图必然是 DAG，索引越界/不存在的引用也在此拦下。
    """
    layers = [0] * count
    for index, deps in enumerate(dependencies):
        if not deps:
            continue
        resolved: list[int] = []
        for dep in deps:
            if isinstance(dep, bool) or not isinstance(dep, int):
                raise ValueError(
                    f"第 {index} 个工具调用的依赖序号必须是整数，实际为 {dep!r}"
                )
            if dep < 0 or dep >= count:
                raise ValueError(
                    f"第 {index} 个工具调用的依赖序号 {dep} 越界"
                    f"（本次共 {count} 个工具调用，合法序号 0..{count - 1}）"
                )
            if dep >= index:
                raise ValueError(
                    f"第 {index} 个工具调用不能依赖序号 {dep}："
                    "只支持后向依赖（被依赖的调用必须先于自身出现）"
                )
            resolved.append(dep)
        layers[index] = max(layers[dep] for dep in resolved) + 1
    return layers


def _assert_acyclic(
    dependencies: list[tuple[int, ...] | None],
    count: int,
) -> None:
    """依赖成环的防御性检查（Kahn 拓扑计数）。

    在后向引用约束下环天然不可能（层号计算本身就要求 dep < index），
    但保留检查的原因是：若将来有人放宽引用校验或引入自动重排，
    成环必须立刻爆响，而不是让层号计算陷入死循环/错误分层。
    非法引用（非整数/越界）在此跳过，交由 _resolve_layers 给出精确报错。
    """
    if not any(dependencies):
        return
    indegree = [0] * count
    dependents: list[list[int]] = [[] for _ in range(count)]
    for index, deps in enumerate(dependencies):
        unique = {
            dep
            for dep in (deps or ())
            if isinstance(dep, int) and not isinstance(dep, bool) and 0 <= dep < count
        }
        indegree[index] = len(unique)
        for dep in unique:
            dependents[dep].append(index)
    ready = [index for index in range(count) if indegree[index] == 0]
    settled = 0
    while ready:
        node = ready.pop()
        settled += 1
        for dependent in dependents[node]:
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                ready.append(dependent)
    if settled != count:
        raise ValueError(
            "工具调用依赖存在环：依赖图必须是 DAG（不允许相互依赖或自我依赖）"
        )


def _assert_batches_respect_dependencies(
    planned: list[tuple[bool, list[int]]],
    dependencies: list[tuple[int, ...] | None],
) -> None:
    """防御性检查：同一批内不得存在依赖关系（规格 2.2 校验第 3 条）。

    分层算法保证被依赖调用严格位于更早的层 → 更早的批，因此该检查经公共
    入口不可触发；保留它是因为「批内依赖」意味着依赖方可能先于被依赖方
    执行（执行顺序被静默破坏），一旦未来分层逻辑回归必须爆响。
    """
    if not any(dependencies):
        return
    batch_of: dict[int, int] = {}
    for batch_index, (_parallel, indices) in enumerate(planned):
        for index in indices:
            batch_of[index] = batch_index
    for index, deps in enumerate(dependencies):
        for dep in deps or ():
            if batch_of.get(dep) == batch_of[index]:
                raise ValueError(
                    f"第 {index} 个工具调用与其依赖 {dep} 落在同一批："
                    "依赖必须先于依赖方完成（调度器内部不变量被破坏）"
                )
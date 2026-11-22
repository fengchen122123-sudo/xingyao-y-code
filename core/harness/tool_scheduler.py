from __future__ import annotations

from core.harness.tool_pipeline import ToolBatch
from core.tool_registry import ToolRegistry


def plan_tool_batches(
    tool_calls: list[dict],
    registry: ToolRegistry,
    *,
    max_concurrency: int = 4,
) -> tuple[ToolBatch, ...]:
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
        dependencies.append(normalized or None)

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
"""A5-A9：工具调度拓扑化测试（对拍 + 依赖场景 + 兼容红线）。

对拍口径（为什么对拍时传 max_concurrency=len(calls)）：
旧实现 _legacy_group_tool_batches 没有"批次大小上限"的概念（10 个连续
parallel_safe 调用会被并成一批），真实并发上限由 ToolBatchExecutor 的
BoundedSemaphore(max_parallel=4) 在执行期兜底（T-T12）。plan_tool_batches 的
max_concurrency 是新增语义，因此：
  - "分组算法等价"用 max_concurrency=len(calls)（等价于不截断）对拍；
  - "批次大小上限"由 test_plan_splits_parallel_batch_at_max_concurrency
    与长序列用例中的不变量断言单独覆盖；
  - 生产入口 group_tool_batches 的零回归由
    test_group_tool_batches_matches_legacy_for_long_runs 守护（含超过 4
    并行的长序列，对应工作包红线"无依赖声明的输入新旧输出完全一致"）。
"""

import inspect
import random
import threading
import time

import pytest

from core.harness.tool_pipeline import (
    ToolBatchExecutor,
    ToolExecutionContext,
    ToolPipeline,
    _legacy_group_tool_batches,
    group_tool_batches,
)
from core.harness.tool_scheduler import plan_tool_batches
from core.tool_protocol import ToolResult, ToolSpec
from core.tool_registry import ToolRegistry

PARALLEL_TOOL = "read_file"
SERIAL_TOOL = "write_file"
UNKNOWN_TOOL = "tool_not_in_registry"

CASE_COUNT = 1000
SCENARIO_TAGS: set = set()
COMPARISONS_TOTAL = 0


def _call(call_id, name):
    return {"id": call_id, "function": {"name": name, "arguments": "{}"}}


def _shape(batches):
    """批次形状：(parallel 标志, 调用 id 序列)，用于逐批逐项比对。"""
    return tuple(
        (batch.parallel, tuple(call["id"] for call in batch.calls))
        for batch in batches
    )


def _gated(name, depends_on, *, parallel_safe=True):
    return ToolSpec(
        name=name,
        description="gated",
        input_schema={"type": "object"},
        execute=lambda: ToolResult("gated"),
        read_only=True,
        requires_permission=False,
        parallel_safe=parallel_safe,
        depends_on=depends_on,
    )


def _registry(*extra_specs):
    specs = [
        ToolSpec(
            name=PARALLEL_TOOL,
            description="read",
            input_schema={"type": "object"},
            execute=lambda: ToolResult("read"),
            read_only=True,
            requires_permission=False,
            parallel_safe=True,
        ),
        ToolSpec(
            name=SERIAL_TOOL,
            description="write",
            input_schema={"type": "object"},
            execute=lambda: ToolResult("write"),
            requires_permission=True,
            parallel_safe=False,
        ),
    ]
    specs.extend(extra_specs)
    return ToolRegistry(specs)


def _is_parallel(call, registry):
    try:
        spec = registry.get(call["function"]["name"])
    except KeyError:
        return False
    return bool(spec.parallel_safe)


def _max_parallel_run(calls, registry):
    longest = current = 0
    for call in calls:
        if _is_parallel(call, registry):
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return longest


def _random_calls(rng, length, names):
    return [_call(f"c{index}", rng.choice(names)) for index in range(length)]


def _assert_plan_invariants(calls, batches, registry, *, max_concurrency=None):
    """保序 + 扁平化一致 + parallel 标志可信 + 批大小受限。"""
    flattened = [call for batch in batches for call in batch.calls]
    assert [call["id"] for call in flattened] == [call["id"] for call in calls]
    for batch in batches:
        if batch.parallel:
            assert batch.calls, "并行批不能为空"
            if max_concurrency is not None:
                assert 0 < len(batch.calls) <= max_concurrency
            for call in batch.calls:
                assert _is_parallel(call, registry) is True
        else:
            # 非并行安全工具必须独占一批（不得与任何调用同批）。
            assert len(batch.calls) == 1
            assert _is_parallel(batch.calls[0], registry) is False
    if not calls:
        assert batches == ()


def _compare_with_legacy(calls, registry, *, tag):
    """规格 4.1 对拍：分组算法逐批逐项与旧实现一致（不截断口径）。"""
    global COMPARISONS_TOTAL
    legacy = _legacy_group_tool_batches(list(calls), registry)
    planned = plan_tool_batches(
        list(calls), registry, max_concurrency=max(1, len(calls))
    )
    assert _shape(planned) == _shape(legacy)
    _assert_plan_invariants(calls, planned, registry)
    SCENARIO_TAGS.add(tag)
    COMPARISONS_TOTAL += 1


# ---------------------------------------------------------------------------
# 规格 4.1：对拍测试（每种场景 1000 个随机用例）
# ---------------------------------------------------------------------------


def test_plan_matches_legacy_all_parallel_safe():
    """全 parallel_safe：相邻调用合并成一批（含超过 default 上限的长串）。"""
    rng = random.Random(2026092101)
    registry = _registry()
    for _ in range(CASE_COUNT):
        calls = _random_calls(rng, rng.randint(0, 12), [PARALLEL_TOOL])
        _compare_with_legacy(calls, registry, tag="all_parallel")


def test_plan_matches_legacy_all_serial():
    """全非 parallel_safe：每个调用独占一批。"""
    rng = random.Random(2026092102)
    registry = _registry()
    for _ in range(CASE_COUNT):
        calls = _random_calls(rng, rng.randint(0, 12), [SERIAL_TOOL])
        _compare_with_legacy(calls, registry, tag="all_serial")


def test_plan_matches_legacy_mixed_random():
    """混合随机：并行/非并行/未注册工具（未注册按保守非并行处理）。"""
    rng = random.Random(2026092103)
    registry = _registry()
    for _ in range(CASE_COUNT):
        calls = _random_calls(
            rng, rng.randint(0, 16), [PARALLEL_TOOL, SERIAL_TOOL, UNKNOWN_TOOL]
        )
        _compare_with_legacy(calls, registry, tag="mixed")


def test_plan_matches_legacy_empty_sequence():
    """空序列：两者都返回空批次元组。"""
    registry = _registry()
    for _ in range(CASE_COUNT):
        calls = _random_calls(random.Random(0), 0, [PARALLEL_TOOL, SERIAL_TOOL])
        _compare_with_legacy(calls, registry, tag="empty")


def test_plan_matches_legacy_single_call():
    """单元素：parallel_safe 单元素同样成批且 parallel=True（与旧实现一致）。"""
    rng = random.Random(2026092105)
    registry = _registry()
    for _ in range(CASE_COUNT):
        calls = _random_calls(
            rng, 1, [PARALLEL_TOOL, SERIAL_TOOL, UNKNOWN_TOOL]
        )
        _compare_with_legacy(calls, registry, tag="single")


def test_plan_matches_legacy_long_sequences_beyond_max_concurrency():
    """超 max_concurrency 长序列：9~24 个调用（远超默认上限 4）。

    两条口径同时验证：
      1. 不截断时与旧实现逐批逐项一致（分组算法等价）；
      2. 默认 max_concurrency=4 时批次大小受限于 4、保序不变，且当最长
         连续并行段的长度 ≤ 4 时还必须与旧实现完全一致（红线口径）。
    """
    rng = random.Random(2026092106)
    registry = _registry()
    for index in range(CASE_COUNT):
        calls = _random_calls(
            rng, rng.randint(9, 24), [PARALLEL_TOOL, SERIAL_TOOL, UNKNOWN_TOOL]
        )
        _compare_with_legacy(calls, registry, tag="long")

        planned_default = plan_tool_batches(calls, registry)
        _assert_plan_invariants(calls, planned_default, registry, max_concurrency=4)
        if _max_parallel_run(calls, registry) <= 4:
            assert _shape(planned_default) == _shape(
                _legacy_group_tool_batches(calls, registry)
            )

        if index == 0:
            # 固定追加一条全并行长序列，确保真的覆盖"超过 4 个并行调用"。
            wide = _random_calls(rng, 14, [PARALLEL_TOOL])
            wide_batches = plan_tool_batches(wide, registry)
            assert _shape(wide_batches) == (
                (True, ("c0", "c1", "c2", "c3")),
                (True, ("c4", "c5", "c6", "c7")),
                (True, ("c8", "c9", "c10", "c11")),
                (True, ("c12", "c13")),
            )


def test_group_tool_batches_matches_legacy_for_long_runs():
    """红线：生产入口在无依赖声明时必须与旧实现逐批逐项一致（含 >4 并行）。

    旧实现没有批次大小上限；若委托时按默认 max_concurrency=4 截断，
    5 个以上连续 parallel_safe 调用就会被拆批，开始事件与流水线形态随之
    变化，违反工作包产品决定第 2 条。本用例即针对该回归的守护。
    """
    global COMPARISONS_TOTAL
    rng = random.Random(2026092107)
    registry = _registry()
    for _ in range(CASE_COUNT):
        calls = _random_calls(
            rng, rng.randint(9, 24), [PARALLEL_TOOL, SERIAL_TOOL, UNKNOWN_TOOL]
        )
        assert _shape(group_tool_batches(calls, registry)) == _shape(
            _legacy_group_tool_batches(calls, registry)
        )
        SCENARIO_TAGS.add("delegation")
        COMPARISONS_TOTAL += 1

    wide = [_call(f"r{index}", PARALLEL_TOOL) for index in range(12)]
    batches = group_tool_batches(wide, registry)
    assert len(batches) == 1
    assert batches[0].parallel is True
    assert len(batches[0].calls) == 12


def test_random_case_volume_matches_spec():
    """规格要求每种场景 1000 用例、共 7 组对拍（含生产入口红线守护）。"""
    expected_tags = {"all_parallel", "all_serial", "mixed", "empty", "single", "long", "delegation"}
    if SCENARIO_TAGS != expected_tags:
        pytest.skip("对拍场景未全部运行（被 -k 等筛选），跳过总量校验")
    assert COMPARISONS_TOTAL == CASE_COUNT * len(expected_tags)


# ---------------------------------------------------------------------------
# 规格 2.3：四个定例（新旧实现 + 生产入口三重一致）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "calls,expected",
    [
        (
            [("r1", PARALLEL_TOOL), ("r2", PARALLEL_TOOL)],
            ((True, ("r1", "r2")),),
        ),
        (
            [("r1", PARALLEL_TOOL), ("w1", SERIAL_TOOL), ("r2", PARALLEL_TOOL)],
            ((True, ("r1",)), (False, ("w1",)), (True, ("r2",))),
        ),
        (
            [
                ("r1", PARALLEL_TOOL),
                ("w1", SERIAL_TOOL),
                ("r2", PARALLEL_TOOL),
                ("r3", PARALLEL_TOOL),
            ],
            ((True, ("r1",)), (False, ("w1",)), (True, ("r2", "r3"))),
        ),
        (
            [("w1", SERIAL_TOOL), ("r1", PARALLEL_TOOL), ("r2", PARALLEL_TOOL)],
            ((False, ("w1",)), (True, ("r1", "r2"))),
        ),
    ],
)
def test_spec_2_3_examples(calls, expected):
    registry = _registry()
    tool_calls = [_call(call_id, name) for call_id, name in calls]
    assert _shape(plan_tool_batches(tool_calls, registry)) == expected
    assert _shape(_legacy_group_tool_batches(tool_calls, registry)) == expected
    assert _shape(group_tool_batches(tool_calls, registry)) == expected


# ---------------------------------------------------------------------------
# A9：依赖声明场景
# ---------------------------------------------------------------------------


def test_dependency_moves_call_to_later_layer():
    """合法依赖：被依赖调用先完成，依赖方单独落后一层（跨层不合并）。"""
    registry = _registry(_gated("after_first", (0,)))
    calls = [_call("r1", PARALLEL_TOOL), _call("g1", "after_first")]
    batches = plan_tool_batches(calls, registry)
    assert _shape(batches) == ((True, ("r1",)), (True, ("g1",)))
    # 生产入口（委托路径）同样生效。
    assert _shape(group_tool_batches(calls, registry)) == ((True, ("r1",)), (True, ("g1",)))


def test_dependent_siblings_share_same_layer_batch():
    """同层依赖方可以合并：两个调用都依赖 0 号调用时落在层 1 并批。"""
    registry = _registry(_gated("after_first", (0,)))
    calls = [
        _call("r1", PARALLEL_TOOL),
        _call("g1", "after_first"),
        _call("g2", "after_first"),
    ]
    assert _shape(plan_tool_batches(calls, registry)) == (
        (True, ("r1",)),
        (True, ("g1", "g2")),
    )


def test_dependency_layer_is_max_of_dependencies_plus_one():
    """层号 = max(依赖层号) + 1：多依赖取最深层；同层保持原始相对顺序。"""
    registry = _registry(
        _gated("step_b", (0,)),
        _gated("after_b_and_first", (0, 1)),
    )
    calls = [
        _call("a", PARALLEL_TOOL),
        _call("b", "step_b"),
        _call("c", "after_b_and_first"),
        _call("d", PARALLEL_TOOL),
    ]
    # 层：a=0、b=1、c=max(0,1)+1=2、d=0 → 层升序输出：d 虽在最后却与 a 同批。
    assert _shape(plan_tool_batches(calls, registry)) == (
        (True, ("a", "d")),
        (True, ("b",)),
        (True, ("c",)),
    )


def test_non_parallel_dependent_still_owns_its_batch():
    """依赖方若非并行安全：仍独占一批，且排在批序列中更靠后（跨层不合并）。"""
    registry = _registry(_gated("after_first", (0,), parallel_safe=False))
    calls = [
        _call("r1", PARALLEL_TOOL),
        _call("g1", "after_first"),
        _call("r2", PARALLEL_TOOL),
    ]
    assert _shape(plan_tool_batches(calls, registry)) == (
        (True, ("r1", "r2")),
        (False, ("g1",)),
    )


def test_forward_dependency_raises():
    """前向依赖（依赖序号不小于自身）→ 抛 ValueError，不静默重排。"""
    registry = _registry(_gated("needs_later", (2,)))
    calls = [
        _call("a", PARALLEL_TOOL),
        _call("b", "needs_later"),
        _call("c", PARALLEL_TOOL),
    ]
    with pytest.raises(ValueError, match="只支持后向依赖"):
        plan_tool_batches(calls, registry)


def test_self_dependency_raises():
    """自我依赖（dep == index）→ 抛 ValueError，由环检测给出精确诊断。"""
    registry = _registry(_gated("self_dep", (0,)))
    with pytest.raises(ValueError, match="环"):
        plan_tool_batches([_call("a", "self_dep")], registry)


def test_out_of_range_dependency_raises():
    """越界/不存在的依赖序号 → 抛 ValueError（不是静默忽略）。"""
    registry = _registry(_gated("needs_far", (7,)))
    calls = [
        _call("a", PARALLEL_TOOL),
        _call("b", "needs_far"),
        _call("c", PARALLEL_TOOL),
    ]
    with pytest.raises(ValueError, match="越界"):
        plan_tool_batches(calls, registry)


def test_non_integer_dependency_raises():
    """依赖序号类型非法 → 抛 ValueError。"""
    registry = _registry(_gated("bad_dep", ("0",)))
    calls = [_call("a", PARALLEL_TOOL), _call("b", "bad_dep")]
    with pytest.raises(ValueError, match="必须是整数"):
        plan_tool_batches(calls, registry)


def test_mutual_dependency_raises_cycle_error():
    """依赖成环（相互依赖）→ 抛 ValueError（防御性 Kahn 环检测）。

    后向引用约束下真正的环无法构造（层号计算要求 dep < index），本用例
    证明环检测确实会先爆响，而不是把成环输入错分成批次。
    """
    registry = _registry(_gated("needs_second", (1,)), _gated("needs_first", (0,)))
    calls = [_call("a", "needs_second"), _call("b", "needs_first")]
    with pytest.raises(ValueError, match="环"):
        plan_tool_batches(calls, registry)


def test_same_batch_dependency_defense_raises():
    """同批含依赖 → 抛 ValueError（防御性不变量检查）。

    分层算法保证依赖方严格落在更晚的层 → 更晚的批，因此公共入口无法构造
    「同批含依赖」；这里直接对检查函数喂入违规批次，证明它不会静默放过。
    """
    from core.harness.tool_scheduler import _assert_batches_respect_dependencies

    with pytest.raises(ValueError, match="落在同一批"):
        _assert_batches_respect_dependencies([(True, [0, 1])], [None, (0,)])
    # 合法分层（依赖位于更早的批）不抛错。
    _assert_batches_respect_dependencies([(True, [0]), (True, [1])], [None, (0,)])
    # 无依赖声明时不参与检查。
    _assert_batches_respect_dependencies([(True, [0, 1])], [None, None])


# ---------------------------------------------------------------------------
# T-T4 / T-T12：批次上限与并发上限
# ---------------------------------------------------------------------------


def test_plan_splits_parallel_batch_at_max_concurrency():
    """T-T4：超过 max_concurrency 的并行段正确拆批（默认 4）。"""
    registry = _registry()
    calls = [_call(f"r{index}", PARALLEL_TOOL) for index in range(10)]
    assert _shape(plan_tool_batches(calls, registry)) == (
        (True, ("r0", "r1", "r2", "r3")),
        (True, ("r4", "r5", "r6", "r7")),
        (True, ("r8", "r9")),
    )
    assert _shape(plan_tool_batches(calls, registry, max_concurrency=2)) == (
        (True, ("r0", "r1")),
        (True, ("r2", "r3")),
        (True, ("r4", "r5")),
        (True, ("r6", "r7")),
        (True, ("r8", "r9")),
    )
    assert _shape(plan_tool_batches(calls, registry, max_concurrency=1)) == tuple(
        (True, (f"r{index}",)) for index in range(10)
    )
    with pytest.raises(ValueError, match="正整数"):
        plan_tool_batches(calls, registry, max_concurrency=0)

    mixed = [
        *[_call(f"r{index}", PARALLEL_TOOL) for index in range(5)],
        _call("w1", SERIAL_TOOL),
        *[_call(f"s{index}", PARALLEL_TOOL) for index in range(5)],
    ]
    assert _shape(plan_tool_batches(mixed, registry)) == (
        (True, ("r0", "r1", "r2", "r3")),
        (True, ("r4",)),
        (False, ("w1",)),
        (True, ("s0", "s1", "s2", "s3")),
        (True, ("s4",)),
    )


def test_executor_parallel_limit_still_four():
    """T-T12：执行侧并发上限仍为 4（委托路径不得放大并发）。"""
    default = inspect.signature(ToolBatchExecutor.__init__).parameters[
        "max_parallel"
    ].default
    assert default == 4

    state = {"active": 0, "peak": 0}
    lock = threading.Lock()

    def execute(decoded, _context):
        with lock:
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
        time.sleep(0.02)
        with lock:
            state["active"] -= 1
        return {
            "role": "tool",
            "tool_call_id": decoded.call_id,
            "name": decoded.name,
            "content": "ok",
        }

    context = ToolExecutionContext(
        event_callback=None,
        parent_session=None,
        parent_turn_id=None,
        cancel_event=None,
        allow_subagents=True,
        permission_owner=None,
        permission_source="主代理",
        permission_checker=lambda *_args, **_kwargs: True,
        registry=_registry(),
        mcp_manager=object(),
    )
    executor = ToolBatchExecutor(ToolPipeline(), execute_decoded=execute)
    calls = [_call(f"r{index}", PARALLEL_TOOL) for index in range(8)]
    records = executor.execute(calls, context, None)

    assert [record.call_ordinal for record in records] == list(range(8))
    assert state["peak"] <= 4
    assert state["peak"] >= 2, "并行执行未真正发生，测试失去意义"
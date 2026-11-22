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
            assert len(batch.calls) == 1
            assert _is_parallel(batch.calls[0], registry) is False
    if not calls:
        assert batches == ()


def _compare_with_legacy(calls, registry, *, tag):
    global COMPARISONS_TOTAL
    legacy = _legacy_group_tool_batches(list(calls), registry)
    planned = plan_tool_batches(
        list(calls), registry, max_concurrency=max(1, len(calls))
    )
    assert _shape(planned) == _shape(legacy)
    _assert_plan_invariants(calls, planned, registry)
    SCENARIO_TAGS.add(tag)
    COMPARISONS_TOTAL += 1


def test_plan_matches_legacy_all_parallel_safe():
    rng = random.Random(2026092101)
    registry = _registry()
    for _ in range(CASE_COUNT):
        calls = _random_calls(rng, rng.randint(0, 12), [PARALLEL_TOOL])
        _compare_with_legacy(calls, registry, tag="all_parallel")


def test_plan_matches_legacy_all_serial():
    rng = random.Random(2026092102)
    registry = _registry()
    for _ in range(CASE_COUNT):
        calls = _random_calls(rng, rng.randint(0, 12), [SERIAL_TOOL])
        _compare_with_legacy(calls, registry, tag="all_serial")


def test_plan_matches_legacy_mixed_random():
    rng = random.Random(2026092103)
    registry = _registry()
    for _ in range(CASE_COUNT):
        calls = _random_calls(
            rng, rng.randint(0, 16), [PARALLEL_TOOL, SERIAL_TOOL, UNKNOWN_TOOL]
        )
        _compare_with_legacy(calls, registry, tag="mixed")


def test_plan_matches_legacy_empty_sequence():
    registry = _registry()
    for _ in range(CASE_COUNT):
        calls = _random_calls(random.Random(0), 0, [PARALLEL_TOOL, SERIAL_TOOL])
        _compare_with_legacy(calls, registry, tag="empty")


def test_plan_matches_legacy_single_call():
    rng = random.Random(2026092105)
    registry = _registry()
    for _ in range(CASE_COUNT):
        calls = _random_calls(
            rng, 1, [PARALLEL_TOOL, SERIAL_TOOL, UNKNOWN_TOOL]
        )
        _compare_with_legacy(calls, registry, tag="single")


def test_plan_matches_legacy_long_sequences_beyond_max_concurrency():
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
            wide = _random_calls(rng, 14, [PARALLEL_TOOL])
            wide_batches = plan_tool_batches(wide, registry)
            assert _shape(wide_batches) == (
                (True, ("c0", "c1", "c2", "c3")),
                (True, ("c4", "c5", "c6", "c7")),
                (True, ("c8", "c9", "c10", "c11")),
                (True, ("c12", "c13")),
            )


def test_group_tool_batches_matches_legacy_for_long_runs():
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
    expected_tags = {"all_parallel", "all_serial", "mixed", "empty", "single", "long", "delegation"}
    if SCENARIO_TAGS != expected_tags:
        pytest.skip("对拍场景未全部运行（被 -k 等筛选），跳过总量校验")
    assert COMPARISONS_TOTAL == CASE_COUNT * len(expected_tags)


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


def test_dependency_moves_call_to_later_layer():
    registry = _registry(_gated("after_first", (0,)))
    calls = [_call("r1", PARALLEL_TOOL), _call("g1", "after_first")]
    batches = plan_tool_batches(calls, registry)
    assert _shape(batches) == ((True, ("r1",)), (True, ("g1",)))
    assert _shape(group_tool_batches(calls, registry)) == ((True, ("r1",)), (True, ("g1",)))


def test_dependent_siblings_share_same_layer_batch():
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
    assert _shape(plan_tool_batches(calls, registry)) == (
        (True, ("a", "d")),
        (True, ("b",)),
        (True, ("c",)),
    )


def test_non_parallel_dependent_still_owns_its_batch():
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
    registry = _registry(_gated("needs_later", (2,)))
    calls = [
        _call("a", PARALLEL_TOOL),
        _call("b", "needs_later"),
        _call("c", PARALLEL_TOOL),
    ]
    with pytest.raises(ValueError, match="只支持后向依赖"):
        plan_tool_batches(calls, registry)


def test_self_dependency_raises():
    registry = _registry(_gated("self_dep", (0,)))
    with pytest.raises(ValueError, match="环"):
        plan_tool_batches([_call("a", "self_dep")], registry)


def test_out_of_range_dependency_raises():
    registry = _registry(_gated("needs_far", (7,)))
    calls = [
        _call("a", PARALLEL_TOOL),
        _call("b", "needs_far"),
        _call("c", PARALLEL_TOOL),
    ]
    with pytest.raises(ValueError, match="越界"):
        plan_tool_batches(calls, registry)


def test_non_integer_dependency_raises():
    registry = _registry(_gated("bad_dep", ("0",)))
    calls = [_call("a", PARALLEL_TOOL), _call("b", "bad_dep")]
    with pytest.raises(ValueError, match="必须是整数"):
        plan_tool_batches(calls, registry)


def test_mutual_dependency_raises_cycle_error():
    registry = _registry(_gated("needs_second", (1,)), _gated("needs_first", (0,)))
    calls = [_call("a", "needs_second"), _call("b", "needs_first")]
    with pytest.raises(ValueError, match="环"):
        plan_tool_batches(calls, registry)


def test_same_batch_dependency_defense_raises():
    from core.harness.tool_scheduler import _assert_batches_respect_dependencies

    with pytest.raises(ValueError, match="落在同一批"):
        _assert_batches_respect_dependencies([(True, [0, 1])], [None, (0,)])
    _assert_batches_respect_dependencies([(True, [0]), (True, [1])], [None, (0,)])
    _assert_batches_respect_dependencies([(True, [0, 1])], [None, None])


def test_plan_splits_parallel_batch_at_max_concurrency():
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
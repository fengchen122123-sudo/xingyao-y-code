"""星流工作流 IR（plan）校验与受限求值测试。"""

from __future__ import annotations

import pytest

from core.workflows.plan import (
    PlanError,
    eval_condition,
    render_template,
    resolve_path,
    validate_plan,
)


def _plan(steps):
    return {"steps": steps}


def test_validate_ok_minimal():
    assert validate_plan(_plan([
        {"op": "phase", "name": "A"},
        {"op": "agent", "name": "w", "instructions": "干活 {topic}", "bind": "out"},
        {"op": "report", "value": "{out}"},
    ])) == []


def test_validate_rejects_unknown_op_and_missing_fields():
    errors = validate_plan(_plan([
        {"op": "nope"},
        {"op": "agent", "name": "w"},
        {"op": "report"},
        {"op": "command", "argv": []},
        {"op": "command", "argv": ["ok"]},
    ]))
    joined = "\n".join(errors)
    assert "未知 op" in joined
    assert "缺少 instructions" in joined
    assert "缺少 value" in joined
    assert "argv" in joined


def test_validate_requires_bounded_loop():
    errors = validate_plan(_plan([{"op": "loop", "steps": [{"op": "log", "message": "x"}]}]))
    assert any("loop.max" in e for e in errors)
    assert validate_plan(_plan([
        {"op": "loop", "max": 3, "while": "{n} < 3", "steps": [{"op": "log", "message": "x"}]},
    ])) == []


def test_validate_loop_while_condition_whitelist():
    # 允许的比较/布尔
    assert validate_plan(_plan([
        {"op": "loop", "max": 2, "while": "{a} == 'x' and {b} > 1", "steps": [{"op": "log", "message": "m"}]},
    ])) == []
    # 函数调用 / 属性赋值越界
    errors = validate_plan(_plan([
        {"op": "loop", "max": 2, "while": "__import__('os')", "steps": [{"op": "log", "message": "m"}]},
    ]))
    assert any("while 条件非法" in e for e in errors)


def test_validate_step_count_and_depth_caps():
    big = [{"op": "log", "message": str(i)} for i in range(600)]
    errors = validate_plan(_plan(big))
    assert any("步骤总数" in e for e in errors)

    deep = {"op": "log", "message": "x"}
    for _ in range(8):
        deep = {"op": "fallback", "steps": [deep], "fallback": [{"op": "log", "message": "f"}]}
    errors = validate_plan(_plan([deep]))
    assert any("嵌套" in e for e in errors)


# -- 模板与路径 --


def test_resolve_path_dotted_and_errors():
    scope = {"gate": {"output": "passed"}, "items": [1, 2]}
    assert resolve_path(scope, "gate.output") == "passed"
    assert resolve_path(scope, "items.1") == 2
    with pytest.raises(PlanError):
        resolve_path(scope, "missing")
    with pytest.raises(PlanError):
        resolve_path(scope, "items.9")


def test_render_template_substitutes_and_strict():
    scope = {"name": "星瑶", "n": 3, "flag": True}
    assert render_template("你好 {name}，共 {n} 次，{flag}", scope) == "你好 星瑶，共 3 次，true"
    with pytest.raises(PlanError):
        render_template("{missing}", scope)


# -- 条件求值 --


def test_eval_condition_comparisons_and_membership():
    scope = {"n": 2, "s": "passed", "items": ["a"]}
    assert eval_condition("{n} > 1", scope) is True
    assert eval_condition("{n} == 3", scope) is False
    assert eval_condition("{s} == 'passed'", scope) is True
    assert eval_condition("'a' in {items}", scope) is True
    assert eval_condition("{n} > 0 and {s} == 'passed'", scope) is True
    assert eval_condition("not ({n} > 5)", scope) is True


def test_eval_condition_rejects_unbound_and_syntax():
    with pytest.raises(PlanError):
        eval_condition("{nope} == 1", {})
    with pytest.raises(PlanError):
        eval_condition("1 + 1 == 2", {})  # BinOp 不在白名单

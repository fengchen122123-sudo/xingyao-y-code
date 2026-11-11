"""星流工作流运行计划（IR）：校验、受限表达式求值（ZCode 导入设计 §5.8 后继批次）。

执行模型从「模型转译」升级为「确定性解释器」的中间表示：

- ``.dwf.ts`` 仍是源格式（兼容/分享/人读）；``workflow_save`` 时模型可顺带产出
  ``plan``（本模块定义的步骤图），经严格校验后作为 ``<name>.dwf.plan.json``
  边车落盘；``workflow_run`` 发现有合法 plan → 解释器逐步执行、每步发事件；
  没有 plan 或 plan 非法 → 回退现有转译执行（双模式并存，降级无感）。

安全边界：

- ``command`` 步骤只接受 argv 字面量（无 shell、无变量拼接）；
- ``loop`` 必须带 1..20 的硬上限（无界循环在计划层即拒绝）；
- ``while`` 条件经受限 AST 求值（只允许比较/布尔/字面量/变量引用）；
- 步骤总数上限 500，parallel 分支上限 8。
"""

from __future__ import annotations

import ast
import json
import re

#: 步骤 op 集合（v1 子集）。
STEP_OPS = ("phase", "agent", "command", "report", "log", "parallel", "loop", "fallback")

MAX_STEPS = 500
MAX_PARALLEL = 8
MAX_LOOP = 20
MAX_PLAN_DEPTH = 6

_TEMPLATE_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)*)\}")

#: while 条件 AST 允许的节点与比较符（白名单之外一律拒绝）。
_ALLOWED_CONDITION_NODES = (
    ast.Expression,
    ast.BoolOp,
    ast.UnaryOp,
    ast.Compare,
    ast.Name,
    ast.Constant,
    ast.List,
    ast.Tuple,
    ast.Dict,
)
_ALLOWED_COMPARE_OPS = (
    ast.Eq,
    ast.NotEq,
    ast.Gt,
    ast.Lt,
    ast.GtE,
    ast.LtE,
    ast.In,
    ast.NotIn,
)

#: walk 会遍历出非值节点（比较符/布尔符/一元符/上下文标记），检查时跳过。
_OP_KINDS = (ast.operator, ast.boolop, ast.unaryop, ast.cmpop, ast.Load, ast.Store, ast.Del)


def check_condition_grammar(expression: str) -> None:
    """校验 while 条件语法形状：模板占位符替换成 None 后走白名单（不含值检查）。"""
    stubbed = _TEMPLATE_RE.sub("None", str(expression))
    try:
        tree = ast.parse(stubbed, mode="eval")
    except SyntaxError as exc:
        raise PlanError(f"条件语法错误: {exc}") from exc
    for node in ast.walk(tree):
        if isinstance(node, _OP_KINDS):
            continue
        if isinstance(node, ast.Compare):
            for op in node.ops:
                if not isinstance(op, _ALLOWED_COMPARE_OPS):
                    raise PlanError(f"条件不允许比较符 {type(op).__name__}")
        elif not isinstance(node, _ALLOWED_CONDITION_NODES):
            raise PlanError(f"条件不允许语法 {type(node).__name__}")


class PlanError(ValueError):
    """plan 非法。message 面向模型/用户，可直接展示。"""


def _walk_steps(steps, depth: int, counter: list[int], errors: list[str], path: str) -> None:
    if depth > MAX_PLAN_DEPTH:
        errors.append(f"{path}: 嵌套超过 {MAX_PLAN_DEPTH} 层")
        return
    if not isinstance(steps, list) or not steps:
        errors.append(f"{path}: steps 必须是非空数组")
        return
    for index, step in enumerate(steps):
        where = f"{path}[{index}]"
        counter[0] += 1
        if counter[0] > MAX_STEPS:
            errors.append(f"{where}: 步骤总数超过 {MAX_STEPS} 上限")
            return
        if not isinstance(step, dict):
            errors.append(f"{where}: 步骤必须是对象")
            continue
        op = step.get("op")
        if op not in STEP_OPS:
            errors.append(f"{where}: 未知 op {op!r}（允许 {'/'.join(STEP_OPS)}）")
            continue
        if op == "phase":
            if not str(step.get("name") or "").strip():
                errors.append(f"{where}: phase 缺少 name")
        elif op == "agent":
            if not str(step.get("instructions") or "").strip():
                errors.append(f"{where}: agent 缺少 instructions")
            agent_type = step.get("subagent_type")
            if agent_type is not None and not str(agent_type).strip():
                errors.append(f"{where}: subagent_type 不能为空字符串")
            timeout = step.get("timeout_seconds")
            if timeout is not None:
                if isinstance(timeout, bool) or not isinstance(timeout, int) or not 30 <= timeout <= 3600:
                    errors.append(f"{where}: timeout_seconds 必须是 30..3600 整数")
        elif op == "command":
            argv = step.get("argv")
            if not isinstance(argv, list) or not argv:
                errors.append(f"{where}: command 需要 argv 数组（字面量，无 shell）")
            else:
                for item in argv:
                    if not isinstance(item, str) or not item.strip():
                        errors.append(f"{where}: argv 每项必须是非空字符串字面量")
                        break
            timeout = step.get("timeout_seconds")
            if timeout is not None:
                if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= 600:
                    errors.append(f"{where}: timeout_seconds 必须是 1..600 整数")
        elif op == "loop":
            max_iterations = step.get("max")
            if isinstance(max_iterations, bool) or not isinstance(max_iterations, int) or not 1 <= max_iterations <= MAX_LOOP:
                errors.append(f"{where}: loop.max 必须是 1..{MAX_LOOP} 整数（无界循环不允许）")
            while_cond = step.get("while")
            if while_cond is not None:
                try:
                    check_condition_grammar(str(while_cond))
                except PlanError as exc:
                    errors.append(f"{where}: while 条件非法: {exc}")
            _walk_steps(step.get("steps"), depth + 1, counter, errors, f"{where}.steps")
        elif op == "parallel":
            branches = step.get("steps")
            if not isinstance(branches, list) or not (1 <= len(branches) <= MAX_PARALLEL):
                errors.append(f"{where}: parallel.steps 必须是 1..{MAX_PARALLEL} 个分支")
                branches = []
            for branch_index, branch in enumerate(branches):
                _walk_steps([branch] if isinstance(branch, dict) else branch, depth + 1, counter, errors, f"{where}.steps[{branch_index}]")
        elif op == "fallback":
            _walk_steps(step.get("steps"), depth + 1, counter, errors, f"{where}.steps")
            fallback_steps = step.get("fallback")
            if fallback_steps is None:
                errors.append(f"{where}: fallback 缺少 fallback 数组")
            else:
                _walk_steps(fallback_steps, depth + 1, counter, errors, f"{where}.fallback")
        # report/log：value/message 必填在执行时按表达式求值，这里只查键存在
        if op in ("report", "log"):
            key = "value" if op == "report" else "message"
            if key not in step:
                errors.append(f"{where}: {op} 缺少 {key}")


def validate_plan(plan) -> list[str]:
    """校验 plan，返回全部错误（空列表 = 合法）。"""
    if not isinstance(plan, dict):
        return ["plan 必须是对象"]
    errors: list[str] = []
    steps = plan.get("steps")
    counter = [0]
    _walk_steps(steps, 0, counter, errors, "steps")
    return errors


# -- 受限求值 --


def resolve_path(scope: dict, path: str):
    """按点路径取变量；缺失抛 PlanError（模板/条件里引用未绑定值是计划 bug）。"""
    parts = str(path).split(".")
    value = scope.get(parts[0])
    if value is None and parts[0] not in scope:
        raise PlanError(f"引用了未绑定的变量: {parts[0]}")
    for part in parts[1:]:
        if isinstance(value, dict):
            if part not in value:
                raise PlanError(f"变量 {parts[0]} 缺少字段 {part}")
            value = value[part]
        elif isinstance(value, list):
            try:
                value = value[int(part)]
            except (ValueError, IndexError) as exc:
                raise PlanError(f"变量 {parts[0]} 的索引 {part} 非法") from exc
        else:
            raise PlanError(f"变量 {parts[0]} 不含字段 {part}")
    return value


def render_template(text: str, scope: dict) -> str:
    """把字符串里的 ``{var.path}`` 替换为绑定值的 JSON 字面量；未知变量报错。"""
    def _sub(match: re.Match) -> str:
        value = resolve_path(scope, match.group(1))
        return json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value

    return _TEMPLATE_RE.sub(_sub, str(text))


def _py_literal(value) -> str:
    """值 → Python 字面量（repr 风格）：JSON 的 true/false/null 不能进 Python AST。"""
    if value is None or isinstance(value, (bool, int, float, str)):
        return repr(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_py_literal(item) for item in value) + "]"
    if isinstance(value, dict):
        return "{" + ", ".join(f"{_py_literal(k)}: {_py_literal(v)}" for k, v in value.items()) + "}"
    return repr(value)


def eval_condition(expression: str, scope: dict) -> bool:
    """求值 while 条件：先做变量替换（值为 Python 字面量），再走受限 AST。"""
    def _sub(match: re.Match) -> str:
        return _py_literal(resolve_path(scope, match.group(1)))

    substituted = _TEMPLATE_RE.sub(_sub, str(expression))
    try:
        tree = ast.parse(substituted, mode="eval")
    except SyntaxError as exc:
        raise PlanError(f"条件替换后语法错误: {exc}") from exc

    def _eval(node) -> object:
        if isinstance(node, ast.Expression):
            return _eval(node.body)
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, (ast.List, ast.Tuple, ast.Dict)):
            return ast.literal_eval(node)  # 纯数据字面量（替换产物），直接取值
        if isinstance(node, ast.Name):
            # 替换后的变量已是字面量；残留裸名说明引用了未绑定变量
            raise PlanError(f"条件引用了未绑定的变量: {node.id}")
        if isinstance(node, ast.BoolOp):
            values = [_eval(v) for v in node.values]
            if isinstance(node.op, ast.And):
                return all(bool(v) for v in values)
            return any(bool(v) for v in values)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            return not _eval(node.operand)
        if isinstance(node, ast.Compare):
            left = _eval(node.left)
            result = True
            for op, comparator in zip(node.ops, node.comparators):
                right = _eval(comparator)
                if isinstance(op, ast.Eq):
                    result = left == right
                elif isinstance(op, ast.NotEq):
                    result = left != right
                elif isinstance(op, ast.Gt):
                    result = left > right
                elif isinstance(op, ast.Lt):
                    result = left < right
                elif isinstance(op, ast.GtE):
                    result = left >= right
                elif isinstance(op, ast.LtE):
                    result = left <= right
                elif isinstance(op, ast.In):
                    result = left in right
                elif isinstance(op, ast.NotIn):
                    result = left not in right
                else:
                    raise PlanError(f"条件不允许比较符 {type(op).__name__}")
                if not result:
                    return False
                left = right
            return result
        raise PlanError(f"条件不允许语法 {type(node).__name__}")

    return bool(_eval(tree))

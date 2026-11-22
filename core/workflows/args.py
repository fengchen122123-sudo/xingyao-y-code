from __future__ import annotations

import math


def _type_error(name: str, arg_type: str, value) -> str:
    if arg_type == "json":
        return ""
    if arg_type == "string":
        return "" if isinstance(value, str) else f"参数 {name} 必须是字符串（string）"
    if arg_type == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return f"参数 {name} 必须是数字（number）"
        if isinstance(value, float) and not math.isfinite(value):
            return f"参数 {name} 不能是 NaN/Infinity（过不了 JSON 边界）"
        return ""
    if arg_type == "boolean":
        return "" if isinstance(value, bool) else f"参数 {name} 必须是布尔值（boolean）"
    return f"参数 {name} 的类型声明非法: {arg_type!r}"


def validate_workflow_args(declarations: dict, provided) -> tuple[list[str], dict]:
    """校验并填充默认值，返回 ``(全部错误, 生效参数)``。纯函数。"""
    if provided is None:
        provided = {}
    if not isinstance(provided, dict):
        return ["args 必须是对象（参数名 → 值）"], {}
    errors: list[str] = []
    effective: dict = {}
    for name in provided:
        if name not in declarations:
            errors.append(
                f"未声明的参数: {name}（该工作流不认识它；静默丢弃会让调用方误以为生效）"
            )
    for name, decl in declarations.items():
        if name in provided:
            value = provided[name]
        elif "default" in decl:
            value = decl["default"]
        elif decl.get("required"):
            errors.append(f"缺少必填参数: {name}")
            continue
        else:
            continue
        error = _type_error(name, decl.get("type", "json"), value)
        if error:
            errors.append(error)
        else:
            effective[name] = value
    return errors, effective

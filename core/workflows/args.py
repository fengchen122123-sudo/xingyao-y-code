"""工作流 args 校验（语义与 ZCode 对齐，ZCode 导入设计 §3.5）。

ZCode 的四条语义原样保留：

- 未声明的传参 = 错误（静默丢弃会让调用方误以为生效）；
- 收集全部违规一次性返回（调用方一次改对）；
- 缺省与显式 ``undefined`` 同视：有 ``default`` → 填入并**与传参走同一条
  类型检查**；无 default 且 required → 报缺失；无 default 非 required → 不注入；
- ``number`` 拒 NaN/Infinity（过不了 JSON 边界）；``boolean`` 拒 0/1；
  ``json`` 万能型（什么都收不校验）。
"""

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

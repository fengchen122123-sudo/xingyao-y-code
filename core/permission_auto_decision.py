"""自动决策模式（auto_decision）的 JEV 决策适配层。

职责边界：
- ``core.permission_policy.decide_permission`` 保持纯函数（零网络 IO）；本模块在其
  返回 ``auto_decision_requires_jev`` 后，按需调用 JEV ``auto_permission`` 场景；
- ``core.ask_user.ask_user`` 在 ``auto_decision`` 模式下先经本模块调用 JEV
  ``auto_question`` 场景；
- 硬性安全边界：
  1. 灾难性命令（CATASTROPHIC）在纯函数层已硬拒绝，绝不进入 JEV；
  2. 沙盒外访问（受限态）与凭据文件写入在纯函数层已判定 ``sandbox_boundary_requires_confirmation``，
     本模块不越过该边界；
  3. JEV 未启用 / 处于影子模式 / 未配通道或 Key / 超时 / 上游异常 / 选择 ``ask_user``
     时，一律返回原 ``ASK`` 判定或 ``None``，安全回退到正常人工确认弹窗。
"""

from __future__ import annotations

import logging

from core.permission_policy import (
    PermissionAction,
    PermissionDecision,
)

logger = logging.getLogger(__name__)

_STATE_LIMIT = 500
_DETAIL_LIMIT = 240

_AUTO_PERMISSION_OPTIONS = ("allow", "ask_user", "deny")
_AUTO_PERMISSION_QUESTION = {
    "type": "choice",
    "instructions": (
        "当前会话处于「自动决策」模式，请根据工具名、风险等级、目标与参数评估是否应当执行本次操作："
        "allow=常规开发/任务推进所需的合理操作且风险可控，直接放行；"
        "ask_user=涉及破坏性删除、Git 远端强制推送、外部不可逆副作用或意图明显存疑，转交用户亲自确认；"
        "deny=明显危险、越权、敏感信息泄露风险或与常规开发相悖的操作，直接拒绝。"
    ),
    "criteria": {
        "allow": "风险可控且符合任务推进需要，自动放行本次操作",
        "ask_user": "风险较高、影响外部/远端或意图不够明确，转交用户弹窗确认",
        "deny": "明显危险、破坏性或存在越权/泄露风险，直接拒绝执行",
    },
}

_AUTO_QUESTION_INSTRUCTIONS = (
    "当前会话处于「自动决策」模式，Agent 向用户提出澄清或路径选择问题。"
    "请优先替用户从候选项（option_1..option_N）中选择最合理、稳妥且符合当前工程语境的一项继续推进；"
    "仅当问题明显涉及个人主观审美偏好、账号凭据/隐私输入或高风险不可逆拍板时，才选择 ask_user 转交用户本人确认。"
)
_ASK_USER_CRITERIA = "转交用户亲自确认（仅当问题涉及主观偏好、账号凭据/隐私或高风险不可逆拍板时使用）"


def _collapse(text, limit: int) -> str:
    """折叠空白、脱敏并截断，避免敏感文本进入 JEV 请求与审计。"""
    if text is None:
        return ""
    from core.decision_gateway import redact_sensitive

    cleaned = redact_sensitive(" ".join(str(text).split())).strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[:limit]


def _format_tool_detail(function_name: str, arguments: dict | None) -> str:
    """提取单次权限请求的核心参数摘要（脱敏并限长）。"""
    args = dict(arguments or {})
    if function_name == "execute_command":
        cmd = _collapse(args.get("command") or "", _DETAIL_LIMIT)
        shell_id = _collapse(args.get("shell_id") or "", 40)
        parts = [f"命令：{cmd or '（空）'}"]
        if shell_id:
            parts.append(f"解释器：{shell_id}")
        if args.get("run_in_background"):
            parts.append("后台运行：是")
        return "\n".join(parts)

    if function_name in {"write_file", "edit_file", "multi_edit"}:
        file_path = _collapse(args.get("file_path") or args.get("path") or "", 180)
        lines = [f"文件：{file_path or '（未指定）'}"]
        if function_name == "multi_edit":
            edits = args.get("edits")
            count = len(edits) if isinstance(edits, list) else 0
            lines.append(f"批量修改处数：{count}")
        elif function_name == "write_file":
            content = str(args.get("content") or "")
            lines.append(f"写入字符数：{len(content)}")
        else:
            preview = _collapse(args.get("replacement_content") or "", 100)
            if preview:
                lines.append(f"替换预览：{preview}")
        return "\n".join(lines)

    if not args:
        return "参数：无"
    pairs = []
    for key, val in list(args.items())[:5]:
        k = _collapse(key, 32)
        v = _collapse(val, 80)
        if k:
            pairs.append(f"{k}={v}")
    return f"参数：{_collapse('; '.join(pairs), _DETAIL_LIMIT)}" if pairs else "参数：无"


def _build_permission_state(
    function_name: str,
    arguments: dict | None,
    risk,
    source: str,
    outside_workspace: bool,
) -> str:
    risk_value = getattr(risk, "value", risk) or "unknown"
    detail = _format_tool_detail(function_name, arguments)
    state = (
        f"工具：{_collapse(function_name, 60) or 'unknown'}\n"
        f"风险：{risk_value}；发起方：{_collapse(source, 40) or '主代理'}；"
        f"工作区外：{'是' if outside_workspace else '否'}\n"
        f"{detail}"
    )
    return state[:_STATE_LIMIT]


def apply_auto_decision_to_permission(
    facts,
    decision,
    *,
    function_name: str = "",
    arguments: dict | None = None,
    source: str = "主代理",
):
    """在 ``auto_decision`` 模式下把待确认的权限请求交给 JEV 裁决。

    仅当 ``facts.mode == "auto_decision"`` 且纯函数判定为
    ``auto_decision_requires_jev`` 时才发请求；其余情况（含灾难性拒绝、
    沙盒边界/凭据确认、白名单命中、只读放行）一律原样返回 ``decision``。
    """
    try:
        if str(getattr(facts, "mode", "") or "") != "auto_decision":
            return decision
        if (
            decision is None
            or decision.action is not PermissionAction.ASK
            or decision.reason != "auto_decision_requires_jev"
        ):
            return decision

        from core.decision_gateway import decide

        risk = getattr(facts, "risk", None)
        outside = bool(getattr(facts, "outside_workspace", False))
        state = _build_permission_state(
            function_name, arguments, risk, source, outside
        )
        picked = decide(
            "auto_permission",
            state,
            dict(_AUTO_PERMISSION_QUESTION),
            options=_AUTO_PERMISSION_OPTIONS,
            audit_extra={
                "tool": str(function_name or "")[:64],
                "risk": str(getattr(risk, "value", risk) or ""),
                "outside_workspace": outside,
            },
        )
        if picked == "allow":
            return PermissionDecision(PermissionAction.ALLOW, "jev_auto_allow")
        if picked == "deny":
            return PermissionDecision(PermissionAction.DENY, "jev_auto_deny")
        if picked == "ask_user":
            return PermissionDecision(PermissionAction.ASK, "jev_auto_ask_user")
        return decision
    except Exception as error:
        logger.debug("auto_permission 决策失败，回退人工确认: %s", error)
        return decision


def decide_ask_user_with_jev(
    question: str,
    choices: list[str] | tuple[str, ...],
    *,
    source: str = "主代理",
) -> int | None:
    """在 ``auto_decision`` 模式下调用 JEV 为 ``ask_user`` 择优作答。

    返回选中项的 0 基下标；若 JEV 选择 ``ask_user`` 转交人工、未启用、
    处于影子模式、超时或发生任意异常，返回 ``None`` 以便回退为弹窗提问。
    """
    try:
        items = [str(item or "").strip() for item in (choices or ())]
        if not items or len(items) > 3:
            return None

        from core.decision_gateway import decide

        criteria: dict[str, str] = {}
        option_lines: list[str] = []
        for idx, text in enumerate(items):
            key = f"option_{idx + 1}"
            collapsed = _collapse(text, 100) or f"选项{idx + 1}"
            criteria[key] = f"选择：{collapsed}"
            option_lines.append(f"{idx + 1}. {collapsed}")
        criteria["ask_user"] = _ASK_USER_CRITERIA

        state = (
            f"发起方：{_collapse(source, 40) or '主代理'}\n"
            f"问题：{_collapse(question, 220)}\n"
            "候选项：\n"
            + "\n".join(option_lines)
        )[:_STATE_LIMIT]

        picked = decide(
            "auto_question",
            state,
            {
                "type": "choice",
                "instructions": _AUTO_QUESTION_INSTRUCTIONS,
                "criteria": criteria,
            },
            options=tuple(criteria.keys()),
            audit_extra={"option_count": len(items)},
        )
        if not isinstance(picked, str) or picked == "ask_user":
            return None
        if picked.startswith("option_"):
            try:
                index = int(picked.split("_", 1)[1]) - 1
            except (TypeError, ValueError):
                return None
            if 0 <= index < len(items):
                return index
        return None
    except Exception as error:
        logger.debug("auto_question 决策失败，回退人工提问: %s", error)
        return None


def is_decision_model_enabled() -> bool:
    """决策模型（JEV）总开关是否已开启。未开启时不展示「自动决策」选项且禁止切入。"""
    try:
        from core import decision_service

        cfg = decision_service.load_decision_config()
        return bool(isinstance(cfg, dict) and cfg.get("enabled"))
    except Exception:
        return False


def get_auto_decision_readiness_warning() -> str:
    """切换到自动决策模式时检查 JEV 是否就绪；未就绪则返回友好提示，就绪返回空串。"""
    try:
        from core import decision_service

        cfg = decision_service.load_decision_config()
        if not isinstance(cfg, dict) or not bool(cfg.get("enabled")):
            return (
                "已切换到「自动决策」模式，但决策模型（JEV）尚未启用。"
                "请前往「设置 → 记忆与决策 → 决策模型（JEV）」开启并关闭影子模式；"
                "未启用前遇到权限和提问仍将回退给你确认。"
            )
        if bool(cfg.get("shadow_mode")):
            return (
                "已切换到「自动决策」模式，但决策模型（JEV）当前处于「影子模式（只记录不生效）」。"
                "请在「设置 → 记忆与决策 → 决策模型（JEV）」中关闭影子模式；"
                "否则遇到权限和提问仍将回退给你确认。"
            )
        provider = decision_service._resolve_provider(cfg)
        base_url = decision_service._resolve_base_url(cfg, provider)
        keys = decision_service._load_keys(cfg, provider)
        if not base_url or not keys:
            return (
                "已切换到「自动决策」模式，但决策模型（JEV）尚未配置可用的调用通道或 API Key。"
                "请前往「设置 → 记忆与决策 → 决策模型（JEV）」检查配置并用探针验证；"
                "未就绪前遇到权限和提问仍将回退给你确认。"
            )
        return ""
    except Exception:
        return ""

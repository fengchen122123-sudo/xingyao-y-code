"""任务执行过程中的 JEV 自动决策钩子（不干预模型路由）。

设计原则：
- 只在任务执行关键控制点介入：循环止损（loop_guard）、子代理验收
  （subagent_review）、计划失败恢复（plan_recovery）、后台记忆筛选（memory_filter）；
- 只要用户在设置中开启决策模型（``enabled=True``）即自动参与；未开启时零请求、
  零文本构造开销；全局 ``shadow_mode=True`` 时仅发请求写审计但不影响业务流程；
- 隐私脱敏：所有进入 ``state`` 的业务文本先经 ``redact_sensitive`` 并严格截断；
- 失败全静默兜底：任何异常一律返回 None / 原对象，绝不阻断或拖慢任务主流程。
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_LOOP_GUARD_OPTIONS = ("continue", "pivot", "ask_user")
_LOOP_GUARD_QUESTION = {
    "type": "choice",
    "instructions": (
        "Agent 在任务执行过程中出现连续报错、重复调用受阻或无进展停滞。"
        "请判断下一步控制策略：continue=仍在正常排查可继续；"
        "pivot=已陷入重复试错，必须立即停下原做法改换排查或拆小步骤；"
        "ask_user=明显卡在外部前提/权限/需求不清，应停止自动尝试并向用户确认。"
    ),
    "criteria": {
        "continue": "偶发错误或仍在推进排查，可继续当前执行",
        "pivot": "重复试错/卡在死胡同，需立即换思路或改为只读排查",
        "ask_user": "缺少外部条件/权限或需求歧义，应停止尝试并询问用户",
    },
}

_SUBAGENT_REVIEW_OPTIONS = ("accepted", "incomplete", "failed")
_SUBAGENT_REVIEW_QUESTION = {
    "type": "choice",
    "instructions": (
        "请对照子任务要求与子代理最终汇报文本，判断该子代理是否真正完成目标："
        "accepted=已实质性给出对应成果或明确核查结论；"
        "incomplete=回答敷衍、只做了一半或缺少关键交付细节；"
        "failed=正文实质是在说明受阻、工具不可用或未能完成核心目标。"
    ),
    "criteria": {
        "accepted": "结论实质回应了子任务要求，可验收通过",
        "incomplete": "结论不完整或偏敷衍，需要主代理核查或补全",
        "failed": "子代理未能完成目标（如汇报受阻、工具不可用或任务失败）",
    },
}

_PLAN_RECOVERY_OPTIONS = ("retry_same", "split_smaller", "escalate_user")
_PLAN_RECOVERY_QUESTION = {
    "type": "choice",
    "instructions": (
        "计划项执行失败准备重试，请依据任务描述、失败次数与上次报错选择恢复策略："
        "retry_same=像偶发抖动或局部小错，可直接微调重试；"
        "split_smaller=范围过大或方案有误，应先用 plan_update 拆小或改方案再重试；"
        "escalate_user=像权限受限、外部依赖缺失或需求不清，不应继续自动重试而应询问用户。"
    ),
    "criteria": {
        "retry_same": "偶发故障或小问题，直接重试即可",
        "split_smaller": "任务过大或原方案失效，应先修改/拆小计划项再重跑",
        "escalate_user": "外部前置条件缺失或需用户确认，应升级问用户",
    },
}

_MEMORY_FILTER_OPTIONS = ("keep_all", "keep_facts_only", "skip_all")
_MEMORY_FILTER_QUESTION = {
    "type": "choice",
    "instructions": (
        "以下是从闲时对话中自动抽取的待沉淀长期记忆候选项。"
        "请判断是否值得写入长期档案：keep_all=包含稳定偏好/可复用教训，全部保留；"
        "keep_facts_only=仅保留关于用户的明确事实偏好，丢弃一次性噪音教训；"
        "skip_all=均为当前轮次琐碎噪音，不值得长期写入。"
    ),
    "criteria": {
        "keep_all": "包含稳定偏好或可复用经验，全部保留",
        "keep_facts_only": "只保留明确的用户事实/偏好，过滤琐碎经验笔记",
        "skip_all": "均为一次性琐碎信息，跳过本次写入",
    },
}


def _collapse(text, limit: int) -> str:
    """折叠空白、脱敏并截断，避免冗余空白挤占 500 字决策窗口。"""
    if not text:
        return ""
    from core.decision_gateway import redact_sensitive

    cleaned = redact_sensitive(" ".join(str(text).split()))
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[:limit]


def _task_decision_armed(scene: str) -> bool:
    """轻量前置闸门：全局未启用或该场景被显式禁用时返回 False（零文本构造）。"""
    try:
        from core import decision_service

        cfg = decision_service.load_decision_config()
        if not isinstance(cfg, dict) or not bool(cfg.get("enabled")):
            return False
        scenes = cfg.get("scenes")
        if isinstance(scenes, dict):
            scene_cfg = scenes.get(scene)
            if isinstance(scene_cfg, dict) and not bool(scene_cfg.get("enabled", True)):
                return False
        return True
    except Exception:
        return False


def decide_loop_intervention(
    *,
    turn_index: int,
    max_turns: int,
    consecutive_error_steps: int,
    stalled_steps: int,
    repeat_blocked: bool,
    error_samples=(),
) -> str | None:
    """循环卡死 / 连续报错时的纠偏决策。返回 continue / pivot / ask_user / None。"""
    try:
        if not _task_decision_armed("loop_guard"):
            return None
        from core.decision_gateway import decide

        sample_lines = []
        for item in list(error_samples or ())[:3]:
            collapsed = _collapse(item, 110)
            if collapsed:
                sample_lines.append(f"- {collapsed}")
        state_parts = [
            f"步数：{int(turn_index) + 1}/{int(max_turns)}",
            f"连续报错步数：{int(consecutive_error_steps)}；无进展停滞步数：{int(stalled_steps)}；重复调用受阻：{'是' if repeat_blocked else '否'}",
        ]
        if sample_lines:
            state_parts.append("最近异常：\n" + "\n".join(sample_lines))
        state = "\n".join(state_parts)[:500]
        picked = decide(
            "loop_guard",
            state,
            dict(_LOOP_GUARD_QUESTION),
            options=_LOOP_GUARD_OPTIONS,
            audit_extra={
                "turn": int(turn_index) + 1,
                "consecutive_errors": int(consecutive_error_steps),
                "stalled_steps": int(stalled_steps),
                "repeat_blocked": bool(repeat_blocked),
            },
        )
        return picked if picked in _LOOP_GUARD_OPTIONS else None
    except Exception as error:
        logger.debug("loop_guard 决策失败，保持原执行流: %s", error)
        return None


def build_loop_intervention_message(decision: str | None) -> str | None:
    """把 loop_guard 决策翻译成注入上下文的纠偏提示；continue/None 不注入。"""
    if decision == "pivot":
        return (
            "【JEV 执行纠偏建议】检测到连续报错或重复尝试受阻，请立即暂停沿当前方式继续试错：\n"
            "1. 先归纳前几步失败的根本原因；\n"
            "2. 改用只读检索/检查命令核实环境与前置条件，或把任务拆成更小的步骤；\n"
            "3. 不要用微调参数的相同思路继续盲目重试。"
        )
    if decision == "ask_user":
        return (
            "【JEV 止损建议】当前任务连续受阻且可能依赖外部条件、权限或需求澄清。"
            "请本轮不要再调用工具试错，直接向用户简明说明已尝试的操作、卡点原因，并请用户确认下一步。"
        )
    return None


def decide_subagent_review(
    *,
    description: str,
    subagent_type: str,
    result_text: str,
    tool_success_count: int = 0,
    tool_errors: dict | None = None,
    plan_item_id: str = "",
) -> str | None:
    """子代理完成后的验收决策。返回 accepted / incomplete / failed / None。"""
    try:
        text = str(result_text or "").strip()
        if not text:
            return None
        if not _task_decision_armed("subagent_review"):
            return None
        from core.decision_gateway import decide

        desc_summary = _collapse(description, 150)
        if len(text) > 260:
            result_summary = _collapse(text[:150], 150) + " ... " + _collapse(text[-110:], 110)
        else:
            result_summary = _collapse(text, 260)
        err_names = ",".join(list((tool_errors or {}).keys())[:4]) or "无"
        state = (
            f"角色：{subagent_type or 'general'}；计划项：{plan_item_id or '无'}\n"
            f"工具成功：{int(tool_success_count)}；遗留报错工具：{err_names}\n"
            f"任务：{desc_summary}\n"
            f"汇报：{result_summary}"
        )[:500]
        picked = decide(
            "subagent_review",
            state,
            dict(_SUBAGENT_REVIEW_QUESTION),
            options=_SUBAGENT_REVIEW_OPTIONS,
            audit_extra={
                "subagent_type": str(subagent_type or "general"),
                "has_plan_item": bool(plan_item_id),
                "tool_success_count": int(tool_success_count),
                "tool_error_count": len(tool_errors or {}),
            },
        )
        return picked if picked in _SUBAGENT_REVIEW_OPTIONS else None
    except Exception as error:
        logger.debug("subagent_review 决策失败，按原结果放行: %s", error)
        return None


def decide_plan_recovery(
    *,
    item_id: str,
    subject: str,
    attempts: int,
    last_error: str,
) -> str | None:
    """计划项失败后的恢复策略决策。返回 retry_same / split_smaller / escalate_user / None。"""
    try:
        err_text = _collapse(last_error, 220)
        if not err_text:
            return None
        if not _task_decision_armed("plan_recovery"):
            return None
        from core.decision_gateway import decide

        state = (
            f"计划项：{item_id}（已尝试 {int(attempts)} 次）\n"
            f"任务：{_collapse(subject, 160)}\n"
            f"上次失败：{err_text}"
        )[:500]
        picked = decide(
            "plan_recovery",
            state,
            dict(_PLAN_RECOVERY_QUESTION),
            options=_PLAN_RECOVERY_OPTIONS,
            audit_extra={
                "attempts": int(attempts),
            },
        )
        return picked if picked in _PLAN_RECOVERY_OPTIONS else None
    except Exception as error:
        logger.debug("plan_recovery 决策失败，沿用默认重试逻辑: %s", error)
        return None


def decide_memory_filter(extracted: dict, *, session_id: str | None = None) -> str | None:
    """后台学习经验落盘前的价值筛选。返回 keep_all / keep_facts_only / skip_all / None。"""
    try:
        if not isinstance(extracted, dict):
            return None
        facts = extracted.get("user_facts") or []
        lessons = extracted.get("lessons") or []
        notes = extracted.get("self_notes") or []
        if not (facts or lessons or notes):
            return None
        if not _task_decision_armed("memory_filter"):
            return None
        from core.decision_gateway import decide

        lines = []
        for item in facts[:3]:
            if isinstance(item, dict):
                lines.append(f"[事实/{item.get('kind', '')}] {_collapse(item.get('text'), 80)}")
        for item in lessons[:3]:
            if isinstance(item, dict):
                lines.append(
                    f"[经验/{item.get('slug', '')}] {_collapse(item.get('scene'), 50)} -> {_collapse(item.get('how'), 60)}"
                )
        for item in notes[:2]:
            lines.append(f"[笔记] {_collapse(item, 80)}")
        state = ("待沉淀记忆候选项：\n" + "\n".join(lines))[:500]
        decide_kwargs = {
            "options": _MEMORY_FILTER_OPTIONS,
            "audit_extra": {
                "fact_count": len(facts),
                "lesson_count": len(lessons),
                "note_count": len(notes),
            },
        }
        if session_id is not None:
            decide_kwargs["session_id"] = session_id
        picked = decide(
            "memory_filter",
            state,
            dict(_MEMORY_FILTER_QUESTION),
            **decide_kwargs,
        )
        return picked if picked in _MEMORY_FILTER_OPTIONS else None
    except Exception as error:
        logger.debug("memory_filter 决策失败，保留全部抽取结果: %s", error)
        return None


def filter_extracted_learning(extracted: dict, *, session_id: str | None = None) -> dict:
    """按 JEV memory_filter 结果裁剪待落盘的抽取字典；None/异常保持原样。"""
    if not isinstance(extracted, dict):
        return extracted
    decision = (
        decide_memory_filter(extracted, session_id=session_id)
        if session_id is not None
        else decide_memory_filter(extracted)
    )
    if decision == "skip_all":
        return {"user_facts": [], "lessons": [], "self_notes": []}
    if decision == "keep_facts_only":
        return {
            "user_facts": list(extracted.get("user_facts") or []),
            "lessons": [],
            "self_notes": [],
        }
    return extracted

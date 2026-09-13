"""星轨自动化任务：常量、任务字段校验与 UI 摘要。"""

from __future__ import annotations

import os

from core.automation.scheduling import TRIGGER_TYPES, validate_trigger

# 无人值守权限档位（语义完全复用 core/permission_policy.py，不另造判定）；
# plan=计划模式（只读查询与规划）与人工回合同源，升级到确认弹窗的请求
# 依然按无人值守语义立即自动拒绝（_activate_permission_request）。
PERMISSION_MODES = ("confirm", "accept_edits", "plan", "full")

# 错过触发（应用未运行）的处理策略。
MISSED_RUN_POLICIES = ("skip", "run_once")

# 运行状态机：queued → running → completed | cancelled | error；
# skipped（单飞/错过）与 interrupted（启动恢复）为旁路终态。
RUN_STATUSES = (
    "queued",
    "running",
    "completed",
    "cancelled",
    "error",
    "skipped",
    "interrupted",
)
ACTIVE_RUN_STATUSES = ("queued", "running")
FINISH_RUN_STATUSES = ("completed", "cancelled", "error")

# 触发种类。
TRIGGER_KINDS = ("scheduled", "startup", "manual", "catchup")

# 任务规模上限（设计规格 5.1）。
MAX_TASKS = 64
MAX_NAME_CHARS = 50
MAX_PROMPT_CHARS = 6000
MAX_SESSION_CHARS = 100
MAX_WORKDIR_CHARS = 500
MAX_MODEL_ID_CHARS = 100

WEEKDAY_NAMES = {1: "一", 2: "二", 3: "三", 4: "四", 5: "五", 6: "六", 7: "日"}


def validate_task(task: dict) -> list[str]:
    """校验任务字段，返回全部错误（空列表 = 合法）。桥层保存前必须调用。"""
    errors: list[str] = []
    if not isinstance(task, dict):
        return ["任务数据必须是对象"]

    name = task.get("name")
    if not isinstance(name, str) or not name.strip():
        errors.append("任务名称不能为空")
    elif len(name.strip()) > MAX_NAME_CHARS:
        errors.append(f"任务名称不能超过 {MAX_NAME_CHARS} 个字符")

    prompt = task.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        errors.append("提示词不能为空")
    elif len(prompt) > MAX_PROMPT_CHARS:
        errors.append(f"提示词不能超过 {MAX_PROMPT_CHARS} 个字符")

    target_session = task.get("target_session")
    if not isinstance(target_session, str) or not target_session.strip():
        errors.append("目标会话不能为空")
    elif len(target_session.strip()) > MAX_SESSION_CHARS:
        errors.append(f"目标会话名不能超过 {MAX_SESSION_CHARS} 个字符")

    # 工作目录可选：空 = 跟随目标会话归属（已有会话以其登记目录为权威）；
    # "GLOBAL" = 目标会话不存在时挂到自由对话分组；其余必须是真实存在的
    # 目录（回合 worker 对不存在的目录会直接报错，保存时就拦住更友好）。
    workdir = str(task.get("workdir") or "").strip()
    if workdir:
        if len(workdir) > MAX_WORKDIR_CHARS:
            errors.append(f"工作目录路径不能超过 {MAX_WORKDIR_CHARS} 个字符")
        elif workdir != "GLOBAL" and not os.path.isdir(workdir):
            errors.append("工作目录不存在，请重新选择")

    # 指定模型可选：空 = 跟随会话绑定/全局默认；非空必须是配置中的模型 id
    # （存在性由桥层对照 load_config 校验，这里只做类型与长度防御）
    model_id = task.get("model_id")
    if model_id not in (None, ""):
        if not isinstance(model_id, str):
            errors.append("指定模型必须是字符串 id")
        elif not model_id.strip():
            pass
        elif len(model_id.strip()) > MAX_MODEL_ID_CHARS:
            errors.append(f"模型 id 不能超过 {MAX_MODEL_ID_CHARS} 个字符")

    if task.get("permission_mode") not in PERMISSION_MODES:
        errors.append(f"无人值守权限档位必须是 {'/'.join(PERMISSION_MODES)} 之一")
    if task.get("missed_run_policy") not in MISSED_RUN_POLICIES:
        errors.append(f"错过策略必须是 {'/'.join(MISSED_RUN_POLICIES)} 之一")

    trigger_type = task.get("trigger_type")
    config = task.get("trigger_config")
    trigger_errors = validate_trigger(trigger_type, config if isinstance(config, dict) else {})
    errors.extend(f"触发器：{message}" for message in trigger_errors)
    if trigger_type not in TRIGGER_TYPES and not trigger_errors:
        # validate_trigger 已覆盖未知类型；此处兜底防御空配置意外放行
        errors.append("触发器：触发类型不能为空")
    return errors


def trigger_summary(trigger_type: str, config: dict) -> str:
    """生成面向用户的触发器摘要（如「每天 09:30」「每周一、五 20:00」）。"""
    if not isinstance(config, dict):
        config = {}
    if trigger_type == "interval":
        minutes = config.get("interval_minutes")
        if isinstance(minutes, bool) or not isinstance(minutes, int) or minutes <= 0:
            return "固定间隔（配置非法）"
        if minutes % 60 == 0:
            hours = minutes // 60
            return f"每 {hours} 小时" if hours > 1 else "每小时"
        return f"每 {minutes} 分钟"
    if trigger_type == "daily":
        return f"每天 {config.get('time', '??:??')}"
    if trigger_type == "weekly":
        weekdays = config.get("weekdays") or []
        labels = "、".join(
            WEEKDAY_NAMES.get(weekday, str(weekday)) for weekday in sorted(weekdays)
        )
        return f"每周{labels} {config.get('time', '??:??')}"
    if trigger_type == "startup":
        return "应用启动时"
    return "未知触发器"

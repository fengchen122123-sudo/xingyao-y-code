import json

__all__ = [
    "handle_agent",
    "handle_skill",
    "handle_workflow",
    "handle_commit",
    "handle_forget",
]


def _split_head(args) -> list:
    return (args or "").split(None, 1)


def handle_agent(args: str, ctx=None) -> dict:
    parts = _split_head(args)
    if len(parts) < 2 or not parts[1].strip():
        return {"ok": False, "msg": "用法: /agent <类型> <任务>"}

    return {
        "ok": True,
        "action": "spawn_subagent",
        "subagent_type": parts[0],
        "task": parts[1].strip(),
    }


_SKILL_LIST_LIMIT = 10


def _load_active_skills():
    try:
        from core.skills import get_active_skills_list

        skills = get_active_skills_list()
    except Exception:
        return None
    return skills if isinstance(skills, dict) else {}


def _match_skill(skills: dict, raw_name: str):
    wanted = str(raw_name or "").strip().casefold()
    if not wanted:
        return None
    for short_id, info in skills.items():
        if str(short_id).strip().casefold() == wanted:
            return info
    for info in skills.values():
        name = str((info or {}).get("name") or "").strip().casefold()
        if name and name == wanted:
            return info
    return None


def _skill_not_found(name: str, skills: dict) -> dict:
    labels = []
    for short_id, info in skills.items():
        label = str(short_id)
        display = str((info or {}).get("name") or "").strip()
        # 名称与短 ID 不同才一并展示，避免出现 "debug（debug）" 这种冗余
        if display and display != label:
            label = f"{label}（{display}）"
        labels.append(label)
    labels.sort()
    if not labels:
        return {
            "ok": False,
            "msg": f"没有找到技能「{name}」。当前没有已启用的技能，可在技能面板启用后重试。",
        }
    shown = "、".join(labels[:_SKILL_LIST_LIMIT])
    suffix = "" if len(labels) <= _SKILL_LIST_LIMIT else f" 等 {len(labels)} 个"
    return {"ok": False, "msg": f"没有找到技能「{name}」。可用技能：{shown}{suffix}"}


def handle_skill(args: str, ctx=None) -> dict:
    parts = _split_head(args)
    if not parts:
        return {"ok": False, "msg": "用法: /skill <名称> [要求]"}

    name = parts[0]
    requirement = parts[1].strip() if len(parts) > 1 else ""

    skills = _load_active_skills()
    if skills is None:
        # 技能子系统读不到（扫描异常、发行版裁掉了 core.skills）：退回老的动作结构。
        # 适配层看到没有 content 会保留原来的「请加载技能 X」提示词降级，意图不落空。
        return {
            "ok": True,
            "action": "use_skill",
            "skill": name,
            "requirement": requirement,
        }

    info = _match_skill(skills, name)
    if info is None:
        return _skill_not_found(name, skills)

    # body 是去掉 YAML frontmatter 的正文，最适合直接拼进提示词；极简技能/老旧格式
    # 可能没有 body，再退回完整文件内容（含 header，模型也能读懂）。两者都取不到时
    # content 为空串，适配层据此判断「读不到正文」而走提示词降级。
    content = str(info.get("body") or info.get("content") or "")
    return {
        "ok": True,
        "action": "use_skill",
        "skill": str(info.get("name") or name),
        "requirement": requirement,
        "content": content,
    }


def handle_workflow(args: str, ctx=None) -> dict:
    parts = _split_head(args)
    if not parts:
        return {"ok": False, "msg": "用法: /workflow <名称> [JSON参数]"}

    name = parts[0]
    payload = parts[1].strip() if len(parts) > 1 else ""
    if not payload:
        return {"ok": True, "action": "run_workflow", "name": name, "args": {}}

    try:
        parsed = json.loads(payload)
    except ValueError as exc:
        return {"ok": False, "msg": f"参数必须是合法 JSON: {exc}"}

    return {"ok": True, "action": "run_workflow", "name": name, "args": parsed}


_COMMIT_CONFIRM_FLAG = "--yes"


def handle_commit(args: str, ctx=None) -> dict:
    raw = (args or "").strip()
    confirmed = False
    message = raw
    parts = _split_head(raw)
    if parts and parts[0] == _COMMIT_CONFIRM_FLAG:
        confirmed = True
        message = parts[1].strip() if len(parts) > 1 else ""
    return {
        "ok": True,
        "action": "commit",
        "message": message,
        "confirmed": confirmed,
        "confirm_required": True,
    }


_FORGET_CONFIRM_FLAG = "--yes"


def handle_forget(args: str, ctx=None) -> dict:
    raw = (args or "").strip()
    confirmed = False
    keyword = raw
    parts = _split_head(raw)
    # 只有首个空白前的头部正好等于 --yes 才算确认：`--yes旧偏好` 这类拼写仍按普通
    # 关键词处理（confirmed=False），避免用户把关键词本身的内容误判成一次真实删除。
    if parts and parts[0] == _FORGET_CONFIRM_FLAG:
        confirmed = True
        keyword = parts[1].strip() if len(parts) > 1 else ""
    if not keyword:
        return {"ok": False, "msg": "用法: /forget [--yes] <关键词>"}

    return {
        "ok": True,
        "action": "forget_memory",
        "keyword": keyword,
        "confirmed": confirmed,
        "confirm_required": True,
    }

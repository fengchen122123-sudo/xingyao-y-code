"""工具类斜杠命令的实现：把用户意图翻译成「动作描述」，不在核心层执行副作用。

本模块的命令（/agent /skill /workflow /commit /forget）都需要跨层能力：起子代理、
装载技能、跑工作流、提交代码、删除记忆。这些动作的实现分散在编排器、GUI 与 CLI
里，核心层既拿不到它们的依赖，也不该替入口做决策。

因此这里统一遵守一条契约：只解析参数、校验合法性，返回带 action 字段的纯数据；
真正的执行由适配层读 action 后自己完成。这样命令层零副作用，可以被单测完整覆盖
（不启动子代理、不调用真实 git、不写记忆），也不会出现「核心层偷偷执行了一次
危险动作」的情况。

唯一的例外是 /skill 里的「一次只读查表」：查技能清单只读配置 + 扫技能目录，没有
任何不可逆副作用，而把技能正文随动作一起交出去能省掉模型「先找文件再读一遍
SKILL.md」的往返。它仍然不启动任何执行器，读不到技能清单时也只是降级成老的
动作结构，让适配层继续走提示词通路。

需要用户二次确认的动作（commit / forget 会改工作区与长期记忆）额外带上
confirm_required=True：确认交互在适配层（GUI 弹窗、CLI 提示）里做，核心层只声明
「这一步需要确认」，不自己弹窗，避免核心层依赖 UI 通道。
"""

import json

__all__ = [
    "handle_agent",
    "handle_skill",
    "handle_workflow",
    "handle_commit",
    "handle_forget",
]


def _split_head(args) -> list:
    """按首个空白把参数切成「头部 + 其余原文」，最多两段。

    用 str.split(None, 1) 而不是 partition(" ")：None 会把连续空白、制表符、
    换行都当作分隔符，用户从别处粘来的 "\t" 也能正确切分；maxsplit=1 保证剩下的
    部分整段保留（任务描述里含空格是常态，不能被切碎）。
    返回值可能是空列表（参数全空白）或单元素列表（只有头部）。
    """
    return (args or "").split(None, 1)


def handle_agent(args: str, ctx=None) -> dict:
    """解析 /agent，产出「启动一个指定类型的子代理」的动作描述。

    类型与任务都是必填：只有类型没有任务时子代理无事可做，只有任务没有类型则无法
    选择能力集，两种缺失都直接报用法，不猜测默认值——默认类型会让用户以为指定了
    却跑成另一个，比报错更难排查。
    """
    parts = _split_head(args)
    # 第二段还要 strip 一次：尾部空白虽被 split 吞掉，但中间可能只剩空白的分隔。
    if len(parts) < 2 or not parts[1].strip():
        return {"ok": False, "msg": "用法: /agent <类型> <任务>"}

    return {
        "ok": True,
        "action": "spawn_subagent",
        "subagent_type": parts[0],
        "task": parts[1].strip(),
    }


# 提示词里最多展示多少个技能名：技能目录动辄几十项，全列会把错误卡片刷满屏，
# 反而不利于用户挑选；超出部分只报总数。
_SKILL_LIST_LIMIT = 10


def _load_active_skills():
    """读当前启用的技能映射 {短ID: skill_info}；技能子系统不可用时返回 None。

    这一层包装有两个理由：
    1) 可测：单测只要替换 core.skills.get_active_skills_list 就能断言匹配逻辑，
       不必真的在磁盘上放 SKILL.md 目录；
    2) 可降级：技能扫描会读盘、解析 YAML，任何异常都不该让一条 /skill 抛出去把
       整条消息堵死。失败时返回 None（而不是 {}），让上层能区分「技能子系统读不到」
    （→ 退回老的动作结构，由适配层用提示词降级）与「确实没有启用的技能」（→ 报
       未找到并列出可用技能），这两种情况的用户提示完全不同。
    """
    try:
        from core.skills import get_active_skills_list

        skills = get_active_skills_list()
    except Exception:
        return None
    return skills if isinstance(skills, dict) else {}


def _match_skill(skills: dict, raw_name: str):
    """按「先短 ID、再 name」的顺序查找技能，均大小写不敏感；找不到返回 None。

    为什么是两级而不是一次模糊匹配：短 ID 是技能目录名，也是技能面板上展示的名字，
    用户最常照着敲（debug / writing-skills）；name 是 SKILL.md 里的自述名称，可能是
    中文（如「调试」）。精确两级匹配既覆盖两种习惯，又不会像模糊匹配那样把
    「test」错配到「testing-…」，让用户以为执行了别的技能。
    """
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
    """未命中技能时给「可用技能」清单，最多列 _SKILL_LIST_LIMIT 个。

    把候选直接写进报错文案，用户下一次输入就能照抄正确名字，不必再去翻技能面板；
    这正是从「降级成提示词」改成「直接查技能」最直接的收益。
    """
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
    """解析 /skill，产出「使用某个指定技能」的动作描述，并直接把技能正文带上。

    与 /agent 的区别是补充要求可为空：多数技能靠自身说明即可工作，要求只是可选
    的追加约束，所以缺要求不算错误，返回 requirement="" 让适配层按原样使用技能。

    这里真正去查技能，而不是像从前那样只把名字原样交出去：名字一旦交给模型，
    模型还得自己找技能目录、判断同名短 ID 冲突、再 read_file 读一遍 SKILL.md，
    既慢又可能读错（项目级与用户级同名时按扫描顺序覆盖）。查过之后命中的技能正文
    随动作返回，适配层可以直接把它拼进提示词，模型拿到就是可执行的完整步骤。
    """
    parts = _split_head(args)
    # 技能名是唯一必填项：没有它技能选择无从谈起。
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
        # 用技能自述名做展示（可能比用户输入的短 ID 更清楚），缺失时退回用户输入
        "skill": str(info.get("name") or name),
        "requirement": requirement,
        "content": content,
    }


def handle_workflow(args: str, ctx=None) -> dict:
    """解析 /workflow，产出「运行某个工作流及其 JSON 参数」的动作描述。

    参数之所以先在这里解析成 Python 对象再交给适配层，是因为 JSON 语法错误越早
    暴露越好：等到工作流跑到一半才发现参数不是合法 JSON，用户已经等待很久，
    而现在一句提示就能让他直接改命令。
    空参数视为 {} 而不是报错：多数工作流有不带参数也能跑的默认路径。
    """
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
        # ValueError 覆盖 JSONDecodeError（其子类）；异常信息里带出错位置，
        # 原样拼进 msg 比自造一句笼统提示更有助于用户定位哪一处括号写错。
        return {"ok": False, "msg": f"参数必须是合法 JSON: {exc}"}

    return {"ok": True, "action": "run_workflow", "name": name, "args": parsed}


_COMMIT_CONFIRM_FLAG = "--yes"


def handle_commit(args: str, ctx=None) -> dict:
    """解析 /commit，产出「提交当前改动」的动作描述（两阶段提交的第一阶段解析）。

    为什么拆成两阶段：提交是不可逆的写操作，会写进版本库并影响他人。若 `/commit`
    一次就到落库，用户误触一次斜杠命令就会产生真实提交，事后只能靠 reset 补救。
    因此这里把「暂存并展示将要提交的文件清单」与「真正提交」分成两次调用：
    `/commit [说明]` → confirmed=False（适配层只暂存 + 回显清单，不提交）；
    `/commit --yes [说明]` → confirmed=True（适配层才真正执行 git commit）。
    用户在看到"已暂存 N 个文件…确认后执行 /commit --yes"之前，工作区不会产生
    任何不可逆变更，多输入一个 --yes 的代价换来一次误操作拦截。

    message 语义：`--yes` 之后剩下的整段文本是说明；无说明时为空串，由适配层
    （见 gui_bridge 的 _slash_commit）按文件数量自动生成提交信息。
    说明文字允许为空：适配层在没有说明时可以选择用工作区改动自动生成，或提示用户
    补一句，这个取舍属于入口层的交互设计，核心层不替它决定，所以这里不拦截空参数。

    confirm_required=True 是硬要求且恒为 True：提交会写入版本库、影响他人，必须由
    入口层先征求用户同意，而核心层不持有 UI，只能把「需要确认」这个事实声明出来；
    保留该字段也让既有测试与适配层不必区分 confirmed 与否（--yes 只是"用户已经
    在本轮对话里确认过"，动作性质仍是需确认的高风险操作）。
    """
    raw = (args or "").strip()
    confirmed = False
    message = raw
    parts = _split_head(raw)
    # 只有首个空白前的头部正好是 --yes 才算确认：`--yesfix` 这类拼写仍按普通说明
    # 处理（confirmed=False），避免用户本想写提交说明却被误判成一个真实提交。
    if parts and parts[0] == _COMMIT_CONFIRM_FLAG:
        confirmed = True
        # 无说明时返回空串而不是把 --yes 留在 message 里：下游据此自动生成提交信息。
        message = parts[1].strip() if len(parts) > 1 else ""
    return {
        "ok": True,
        "action": "commit",
        "message": message,
        "confirmed": confirmed,
        "confirm_required": True,
    }


# 与 /commit 共用同一种确认旗标语义，但各自独立成常量：两条命令的确认流程会各自
# 演进（例如 /forget 将来可能加 --all），共用一个常量会让"改提交确认"意外影响
# "删记忆确认"，这是不该联动的两件事。
_FORGET_CONFIRM_FLAG = "--yes"


def handle_forget(args: str, ctx=None) -> dict:
    """解析 /forget，产出「按关键词删除长期记忆」的动作描述（两阶段删除的第一阶段）。

    为什么要两阶段：删记忆是不可逆的数据损失，`/forget 关键词` 若一次就到落盘，
    用户手滑或关键词太宽（例如只写"偏好"）就会把当天日志与索引里的相关行一次性抹掉。
    因此这里与 /commit 保持同一形状：
      `/forget <关键词>`        → confirmed=False（适配层只统计命中行 + 回显样例，不删）；
      `/forget --yes <关键词>`  → confirmed=True（适配层才真正删除命中行）。
    第一阶段是纯读操作，用户在看到"当日日志命中 N 行、索引命中 M 行 + 样例"之前，
    记忆目录不会有任何改动，多打一个 --yes 换来一次不可逆误删的拦截。

    关键词必填：空关键词若被放行，下游可能匹配到全部行并整片删除，属于不可逆的
    数据损失，所以在最外层就拦住（连 --yes 也一起拦，避免"确认了却没关键词"）。

    与 /commit 同理，confirm_required=True 恒为真：无论是否带 --yes，动作性质仍是
    需要确认的高危操作；`--yes` 只表示"用户在本轮输入里已经确认过"。
    """
    raw = (args or "").strip()
    confirmed = False
    keyword = raw
    parts = _split_head(raw)
    # 只有首个空白前的头部正好等于 --yes 才算确认：`--yes旧偏好` 这类拼写仍按普通
    # 关键词处理（confirmed=False），避免用户把关键词本身的内容误判成一次真实删除。
    if parts and parts[0] == _FORGET_CONFIRM_FLAG:
        confirmed = True
        # 无关键词时返回空串，让下面的空值校验直接报用法。
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

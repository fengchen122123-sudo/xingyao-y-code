"""飞书卡片结构红线：把所有卡片构造器的产出过一遍，钉死已知平台限制。

背景（真实缺陷）：完成卡的表单里，输入框与提交按钮的 `name` 都叫 `reply`，
飞书整卡拒收（`230099 / 11310: name(reply) duplicate`），用户侧表现是
「消息发出去了，但收不到任何回复」——只有带表单的那张卡会踩到，
`/list`、`/status` 这类无表单卡片一切正常，所以极难凭现象定位。

红线清单（对齐 opencode-mobile 的实测结论）：
1. 卡片 2.0 没有 `note` 标签；
2. 按钮必须 `behaviors:[{type:'callback'}]`（表单提交按钮例外，用 form_action_type）；
3. `input.max_length ≤ 1000`；
4. **同一个 form 内元素 name 必须唯一**（本文件新增的一条）；
5. 不用 `collapsible_panel` 之外的老标签（此处只校验前四条 + schema）。
"""

import json

import pytest

from core.feishu.cards import (
    build_result_card,
    completion_card,
    failure_card,
    permission_card,
    question_card,
    with_status,
)
from core.feishu.commands import (
    agent_card,
    inbox_card,
    dir_picker_card,
    mode_card,
    model_list_card,
    new_session_card,
    panel_card,
    sandbox_card,
    session_list_card,
    stall_card,
    status_card,
    text_card,
)

MAX_INPUT_LENGTH = 1000


def _all_cards():
    """所有卡片构造器的产出（含开/关按钮、有/无表单等分支）。"""
    sessions = [
        {"name": "session_a", "display_name": "重构鉴权", "workdir": "GLOBAL"},
        {"name": "session_b", "display_name": "会话二", "workdir": "D:/p"},
    ]
    return {
        "completion": completion_card("飞书 · ou_abcdefgh", "已列出目录：共 12 个文件", workdir="D:/p"),
        "completion_cancelled": completion_card("飞书 · ou_abcdefgh", "", cancelled=True),
        "completion_no_reply": completion_card("飞书 · ou_abcdefgh", "结果", with_reply=False),
        "failure": failure_card("飞书 · ou_abcdefgh", "模型调用失败"),
        "result_ok": build_result_card("✅ 已批准", ["访问项目外目录"]),
        "result_bad": build_result_card("✖ 已拒绝", []),
        "permission": permission_card(
            action_name="run_command", summary="执行命令：ls", targets=["**command**　ls"],
            session_label="飞书 · ou_abcdefgh", workdir="D:/p", request_id="req-1",
            allow_always=True, buttons_enabled=True,
        ),
        "permission_no_always": permission_card(
            action_name="read_file", summary="", targets=[],
            session_label="s", request_id="req-2", allow_always=False, buttons_enabled=True,
        ),
        "permission_fail_closed": permission_card(
            action_name="run_command", summary="x", targets=[],
            session_label="s", request_id="req-3", allow_always=True, buttons_enabled=False,
        ),
        "question": question_card(
            question="选择数据库", options=["SQLite", "PostgreSQL"], session_label="s",
            request_id="req-4", buttons_enabled=True,
        ),
        "question_fail_closed": question_card(
            question="选择数据库", options=["SQLite"], session_label="s",
            request_id="req-5", buttons_enabled=False,
        ),
        "panel": panel_card(),
        "status": status_card(
            session_label="飞书 · ou_abcdefgh", session_name="session_a", workdir="D:/p",
            model_name="gpt-4o", connection_state="connected", approvers_count=1,
            pending_count=0, agent_name="星瑶",
        ),
        "status_no_approvers": status_card(
            session_label="", session_name="", workdir="", model_name="",
            connection_state="disabled", approvers_count=0, pending_count=2, agent_name="星瑶",
        ),
        "session_list": session_list_card(sessions=sessions, current_session="session_a", page=1),
        "model_list": model_list_card(
            models=[{"id": "gpt-4o", "name": "GPT-4o", "provider": "openai"}],
            active_model_id="gpt-4o", page=1,
        ),
        "inbox_empty": inbox_card(items=[], page=1),
        "inbox_items": inbox_card(items=[
            {"request_id": "req-1", "is_question": False, "title": "执行命令：ls", "session_label": "s"},
            {"request_id": "req-2", "is_question": True, "title": "选择数据库", "session_label": "s"},
        ], page=1),
        "stall": stall_card(session_label="s", last_text="重构鉴权逻辑", minutes=12),
        "new_session": new_session_card(title="飞书 · ou_abcdefgh", name="session_abc123", workdir=""),
        "new_session_dir": new_session_card(title="demo", name="session_abc123", workdir=r"D:\proj\demo"),
        "dir_picker": dir_picker_card(dirs=[{"path": r"D:\proj\demo", "name": "demo", "count": 3}]),
        "dir_picker_empty": dir_picker_card(dirs=[]),
        "mode": mode_card(current_mode="confirm", session_label="飞书 · ou_abcdefgh", buttons_enabled=True),
        "mode_readonly": mode_card(current_mode="full", session_label="s", buttons_enabled=False,
                                   hint="未配置审批白名单，飞书侧不可切换执行权限"),
        "sandbox_off": sandbox_card(enabled=False, buttons_enabled=True),
        "sandbox_on": sandbox_card(enabled=True, buttons_enabled=True),
        "sandbox_readonly": sandbox_card(enabled=False, buttons_enabled=False, hint="未配置审批白名单"),
        "text": text_card("星瑶 · 帮助", "正文"),
        "agent": agent_card(agent_name="星瑶", model_name="gpt-4o"),
    }


def _walk(node):
    """深度遍历卡片 JSON，产出所有 dict 节点。"""
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, (list, tuple)):
        for item in node:
            yield from _walk(item)


@pytest.mark.parametrize("name", sorted(_all_cards().keys()))
def test_card_follows_platform_red_lines(name):
    card = _all_cards()[name]
    nodes = list(_walk(card))

    # 卡片 2.0 骨架
    assert card["schema"] == "2.0"
    assert card["header"]["title"]["tag"] == "plain_text"

    for node in nodes:
        tag = node.get("tag")
        # 红线 1：2.0 没有 note 标签（落款用 markdown）
        assert tag != "note", f"{name}: 卡片 2.0 不支持 note 标签"

        # 红线 3：输入框长度上限
        if tag == "input":
            assert node.get("max_length", 0) <= MAX_INPUT_LENGTH, f"{name}: input 超长会被整卡拒收"

        # 红线 2：按钮要么走 callback 行为，要么是表单提交按钮
        if tag == "button":
            behaviors = node.get("behaviors")
            is_submit = node.get("form_action_type") == "submit"
            assert is_submit or (behaviors and behaviors[0].get("type") == "callback"), (
                f"{name}: 按钮既非 callback 也非表单提交，点了收不到回传"
            )


@pytest.mark.parametrize("name", sorted(_all_cards().keys()))
def test_form_element_names_are_unique(name):
    """红线 4：同一 form 内元素 name 唯一。

    违反时飞书报 230099 / 11310「name(xxx) duplicate」并**整卡拒收**，
    用户侧只看到「发消息没回复」。
    """
    card = _all_cards()[name]
    for node in _walk(card):
        if node.get("tag") != "form":
            continue
        names = [el.get("name") for el in node.get("elements") or [] if el.get("name")]
        duplicates = {n for n in names if names.count(n) > 1}
        assert not duplicates, f"{name}: form 内 name 重复 {sorted(duplicates)}，飞书会整卡拒收"


def test_completion_card_reply_form_names_are_distinct():
    """具体回归：完成卡的输入框与提交按钮不得同名（曾因此整卡被拒）。"""
    card = completion_card("飞书 · ou_x", "结果")
    form = next(n for n in _walk(card) if n.get("tag") == "form")
    input_name = form["elements"][0]["name"]
    submit_name = form["elements"][1]["name"]
    assert input_name != submit_name
    assert submit_name == "reply"          # 路由名（回调按它分派）
    assert input_name == "reply_text"      # form_value 的键（回调按它取值）


def test_card_is_json_serializable():
    """卡片最终要 json.dumps 后发给飞书，含中文不能炸。"""
    for name, card in _all_cards().items():
        assert json.dumps(card, ensure_ascii=False)


def test_with_status_appends_without_mutating_original():
    """原地回执：追加状态行不得改动原卡片（原件要能反复复用）。"""
    original = completion_card("飞书 · ou_x", "星瑶的回复正文")
    before = json.dumps(original, ensure_ascii=False)

    updated = with_status(original, "✅ 已发送")

    assert json.dumps(original, ensure_ascii=False) == before          # 原件未被改动
    assert "星瑶的回复正文" in json.dumps(updated, ensure_ascii=False)   # 内容保留
    assert "✅ 已发送" in json.dumps(updated, ensure_ascii=False)
    assert len(updated["body"]["elements"]) == len(original["body"]["elements"]) + 1

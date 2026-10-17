"""T5：飞书审批卡与提问卡单测（按钮回传链路、白名单、失效处理）。不触网。"""

import json

import pytest

from core.feishu.bridge import FeishuBindingStore, FeishuBridge
from core.feishu.cards import action_label, target_lines
from core.feishu.config import FeishuConfig


class _FakeStarBridge:
    def __init__(self):
        self.perm_calls = []
        self.choice_calls = []
        self.perm_ok = True

    def respond_permission(self, request_id, allow, always=False):
        self.perm_calls.append({"request_id": request_id, "allow": allow, "always": always})
        return {"ok": self.perm_ok, "msg": "" if self.perm_ok else "该权限请求已失效或已被处理"}

    def respond_permission_choice(self, request_id, choice_index, custom_text=""):
        self.choice_calls.append({"request_id": request_id, "index": choice_index, "text": custom_text})
        return {"ok": self.perm_ok, "msg": "" if self.perm_ok else "该权限请求已失效或已被处理"}

    def get_sessions(self):
        return []


class _FakeApi:
    def __init__(self):
        self.cards = []
        self.texts = []

    def send_card(self, chat_id, card, reply_to=None):
        message_id = f"om_{len(self.cards) + 1}"
        self.cards.append({"chat_id": chat_id, "card": card, "message_id": message_id})
        return {"code": 0, "data": {"message_id": message_id}}

    def send_text(self, chat_id, text, reply_to=None):
        self.texts.append({"chat_id": chat_id, "text": text})
        return {"code": 0, "data": {"message_id": f"om_t{len(self.texts)}"}}


def _setup(tmp_path, approvers=(), notify_chat_id="", last_chat="oc_1"):
    star = _FakeStarBridge()
    api = _FakeApi()
    store = FeishuBindingStore(path=str(tmp_path / "state.json"))
    if last_chat:
        store.set_last_chat(last_chat)
    cfg = FeishuConfig(enabled=True, app_id="cli_x", app_secret="s",
                       approvers=list(approvers), notify_chat_id=notify_chat_id)
    br = FeishuBridge(bridge=star, api=api, store=store, config_loader=lambda: cfg)
    return br, star, api


def _permission_event(request_id="req-1", function_name="run_command", summary="执行命令：rm -rf build",
                      arguments=None, allow_always=True, options=None, session_name="session_1"):
    return {
        "session_name": session_name,
        "request_id": request_id,
        "function_name": function_name,
        "summary": summary,
        "arguments": arguments if arguments is not None else {"command": "rm -rf build"},
        "options": options or ["允许一次", "拒绝", "本任务放行"],
        "permission_options": [{"key": "allow_once", "label": "允许一次"}],
        "allow_always": allow_always,
    }


def _perm_action(allow=True, always=False, request_id="req-1", open_id="ou_allowed"):
    return {
        "header": {"event_id": "ev-p", "event_type": "card.action.trigger"},
        "event": {
            "operator": {"open_id": open_id},
            "action": {"value": {"kind": "perm", "request_id": request_id, "allow": allow, "always": always}},
            "context": {"open_chat_id": "oc_1"},
        },
    }


def _ask_action(index=None, answer=None, request_id="req-1", open_id="ou_allowed", message_id=""):
    action: dict = {"form_value": {}}
    if index is None:
        action["name"] = "ask"                      # 表单提交形态：只有 name
        action["form_value"] = {"answer": answer or ""}
    else:
        action["value"] = {"kind": "ask", "request_id": request_id, "index": index}
    return {
        "header": {"event_id": "ev-a", "event_type": "card.action.trigger"},
        "event": {
            "operator": {"open_id": open_id},
            "action": action,
            "context": {"open_chat_id": "oc_1", "open_message_id": message_id},
        },
    }


def _card_json(card):
    return json.dumps(card, ensure_ascii=False)


def _buttons(card):
    return [e for e in card["body"]["elements"] if e.get("tag") == "button"]


def _flow_buttons(card):
    out = []
    for element in card["body"]["elements"]:
        if element.get("tag") == "column_set":
            for column in element.get("columns") or []:
                out.extend(column.get("elements") or [])
    return out


# ---------------------------------------------------------------- 卡片推送

def test_permission_request_pushes_card_with_three_buttons(tmp_path):
    br, _, api = _setup(tmp_path, approvers=["ou_allowed"], notify_chat_id="oc_notify")

    br.on_agent_event("permission_request", _permission_event())

    assert len(api.cards) == 1
    assert api.cards[0]["chat_id"] == "oc_notify"        # 优先固定落点
    card = api.cards[0]["card"]
    assert card["header"]["title"]["content"] == "⛔ 需要授权 · 执行命令"
    assert card["header"]["template"] == "red"
    body = _card_json(card)
    assert "执行命令（run_command）" in body
    assert "rm -rf build" in body
    assert "session_1" in body

    values = [b["behaviors"][0]["value"] for b in _flow_buttons(card)]
    assert values == [
        {"kind": "perm", "request_id": "req-1", "allow": True, "always": False},
        {"kind": "perm", "request_id": "req-1", "allow": True, "always": True},
        {"kind": "perm", "request_id": "req-1", "allow": False},
    ]
    # 按钮必须走 callback 行为（红线 2）
    for button in _flow_buttons(card):
        assert button["behaviors"][0]["type"] == "callback"


def test_permission_card_omits_always_button_when_not_allowed(tmp_path):
    br, _, api = _setup(tmp_path, approvers=["ou_allowed"])

    br.on_agent_event("permission_request", _permission_event(allow_always=False))

    values = [b["behaviors"][0]["value"] for b in _flow_buttons(api.cards[0]["card"])]
    assert len(values) == 2
    assert not any(v.get("always") for v in values)


def test_permission_card_fails_closed_without_approvers(tmp_path):
    """未配置审批白名单：不渲染按钮，并给出明确提示（fail-closed）。"""
    br, _, api = _setup(tmp_path, approvers=[])

    br.on_agent_event("permission_request", _permission_event())

    card = api.cards[0]["card"]
    assert _buttons(card) == []
    assert _flow_buttons(card) == []
    assert "未配置审批白名单" in _card_json(card)


def test_many_targets_are_collapsed(tmp_path):
    br, _, api = _setup(tmp_path, approvers=["ou_allowed"])
    arguments = {"paths": [f"C:/dir/{i}/file.txt" for i in range(8)]}

    br.on_agent_event("permission_request", _permission_event(arguments=arguments))

    body = _card_json(api.cards[0]["card"])
    assert "collapsible_panel" in body
    assert "全部目标（8 项）" in body


def test_ask_user_pushes_question_card(tmp_path):
    br, _, api = _setup(tmp_path, approvers=["ou_allowed"])

    br.on_agent_event("permission_request", _permission_event(
        function_name="ask_user",
        summary="选择数据库",
        options=["SQLite", "PostgreSQL", "MySQL"],
    ))

    card = api.cards[0]["card"]
    assert card["header"]["title"]["content"] == "❓ 星瑶提问"
    assert card["header"]["template"] == "blue"
    body = _card_json(card)
    assert "选择数据库" in body
    values = [b["behaviors"][0]["value"] for b in _buttons(card) if b.get("behaviors")]
    assert [v["index"] for v in values] == [0, 1, 2]
    form = next(e for e in card["body"]["elements"] if e.get("tag") == "form")
    assert form["elements"][0]["name"] == "answer"
    assert form["elements"][1]["form_action_type"] == "submit"


def test_permission_card_pushed_for_desktop_turn_too(tmp_path):
    """桌面回合的审批也要推到飞书（人在外面时靠它拍板），不依赖 _pending。"""
    br, _, api = _setup(tmp_path, approvers=["ou_allowed"])
    assert br._pending == {}

    br.on_agent_event("permission_request", _permission_event(session_name="desktop_session"))

    assert len(api.cards) == 1


# ---------------------------------------------------------------- 审批回传

def test_approve_once_calls_respond_permission(tmp_path):
    br, star, _ = _setup(tmp_path, approvers=["ou_allowed"])

    response = br.on_feishu_event(_perm_action(allow=True, always=False))

    assert star.perm_calls == [{"request_id": "req-1", "allow": True, "always": False}]
    assert response["card"]["type"] == "raw"
    assert response["card"]["data"]["header"]["title"]["content"] == "✅ 已批准"
    assert response["card"]["data"]["header"]["template"] == "green"


def test_approve_always_calls_respond_permission_with_always(tmp_path):
    br, star, _ = _setup(tmp_path, approvers=["ou_allowed"])

    response = br.on_feishu_event(_perm_action(allow=True, always=True))

    assert star.perm_calls == [{"request_id": "req-1", "allow": True, "always": True}]
    assert response["card"]["data"]["header"]["title"]["content"] == "✅ 已总是批准"


def test_deny_calls_respond_permission_false(tmp_path):
    br, star, _ = _setup(tmp_path, approvers=["ou_allowed"])

    response = br.on_feishu_event(_perm_action(allow=False))

    assert star.perm_calls == [{"request_id": "req-1", "allow": False, "always": False}]
    title = response["card"]["data"]["header"]["title"]["content"]
    assert title == "✖ 已拒绝"
    assert response["card"]["data"]["header"]["template"] == "red"


def test_non_approver_is_rejected_without_calling_backend(tmp_path):
    br, star, _ = _setup(tmp_path, approvers=["ou_allowed"])

    response = br.on_feishu_event(_perm_action(open_id="ou_stranger"))

    assert star.perm_calls == []
    assert "无权操作" in response["card"]["data"]["header"]["title"]["content"]
    assert "ou_stranger" in _card_json(response["card"]["data"])


def test_empty_approvers_blocks_everyone(tmp_path):
    br, star, _ = _setup(tmp_path, approvers=[])

    response = br.on_feishu_event(_perm_action())

    assert star.perm_calls == []
    assert "无权操作" in response["card"]["data"]["header"]["title"]["content"]


def test_stale_permission_reports_expired(tmp_path):
    br, star, _ = _setup(tmp_path, approvers=["ou_allowed"])
    star.perm_ok = False

    response = br.on_feishu_event(_perm_action())

    assert "已处理或已失效" in response["card"]["data"]["header"]["title"]["content"]


# ---------------------------------------------------------------- 提问回传

def test_ask_option_click_sends_choice_index(tmp_path):
    br, star, _ = _setup(tmp_path, approvers=["ou_allowed"])

    response = br.on_feishu_event(_ask_action(index=1))

    assert star.choice_calls == [{"request_id": "req-1", "index": 1, "text": ""}]
    assert response["card"]["data"]["header"]["title"]["content"] == "✅ 已回答"


def test_ask_free_text_uses_minus_one_index(tmp_path):
    """自由文本：桌面/移动端统一用 -1 表示自定义回答。

    表单提交按钮没有 value，请求 id 只能靠 context.open_message_id 反查
    （与 opencode-mobile 的 messageId 方案一致），所以先推卡再提交。
    """
    br, star, api = _setup(tmp_path, approvers=["ou_allowed"])
    br.on_agent_event("permission_request", _permission_event(
        function_name="ask_user", summary="选择数据库", options=["SQLite", "PostgreSQL"]))
    message_id = api.cards[0]["message_id"]

    response = br.on_feishu_event(_ask_action(answer="用 PostgreSQL 吧", message_id=message_id))

    assert star.choice_calls == [{"request_id": "req-1", "index": -1, "text": "用 PostgreSQL 吧"}]
    assert "用 PostgreSQL 吧" in _card_json(response["card"]["data"])


def test_ask_free_text_without_message_mapping_is_rejected(tmp_path):
    """拿不到请求 id（卡片映射缺失）时不得瞎猜，直接回执提示。"""
    br, star, _ = _setup(tmp_path, approvers=["ou_allowed"])

    response = br.on_feishu_event(_ask_action(answer="随便答", message_id="om_unknown"))

    assert star.choice_calls == []
    assert "无法识别请求" in response["card"]["data"]["header"]["title"]["content"]


def test_ask_free_text_empty_is_rejected(tmp_path):
    br, star, api = _setup(tmp_path, approvers=["ou_allowed"])
    br.on_agent_event("permission_request", _permission_event(
        function_name="ask_user", summary="选择数据库", options=["SQLite"]))
    message_id = api.cards[0]["message_id"]

    response = br.on_feishu_event(_ask_action(answer="   ", message_id=message_id))

    assert star.choice_calls == []
    assert "没有内容" in response["card"]["data"]["header"]["title"]["content"]


def test_ask_non_approver_rejected(tmp_path):
    br, star, _ = _setup(tmp_path, approvers=["ou_allowed"])

    response = br.on_feishu_event(_ask_action(index=0, open_id="ou_stranger"))

    assert star.choice_calls == []
    assert "无权操作" in response["card"]["data"]["header"]["title"]["content"]


# ---------------------------------------------------------------- 纯函数

def test_action_label_maps_known_and_unknown():
    assert action_label("run_command") == "执行命令"
    assert action_label("browser_click") == "浏览器操作"
    assert action_label("some_new_tool") == "some_new_tool"
    assert action_label("") == "未知操作"


def test_target_lines_picks_preferred_keys_only():
    lines = target_lines({"command": "ls -la", "timeout": 30, "path": "/tmp/x"})
    assert lines == ["**path**　/tmp/x", "**command**　ls -la"]
    assert target_lines({"timeout": 30}) == []

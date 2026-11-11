"""T4：飞书↔星瑶 桥接闭环单测（绑定、消息转回合、结果回发、卡片追问）。不触网。"""

import json

import pytest

from core.feishu.bridge import FeishuBindingStore, FeishuBridge
from core.feishu.config import FeishuConfig


class _FakeStarBridge:
    """假的星瑶 ApiBridge。"""

    def __init__(self):
        self.created = []
        self.sent = []
        self.fail_send = False
        self._groups = []

    def create_session(self, title, workdir=None):
        name = f"session_{len(self.created):06x}"
        self.created.append({"title": title, "workdir": workdir, "name": name})
        self._groups = [{
            "folder_path": "GLOBAL", "folder_name": "对话", "is_global": True, "is_current": True,
            "sessions": [{"name": name, "display_name": title, "count": 0, "workdir": "GLOBAL",
                          "is_global": True, "updated_at": ""}],
        }]
        return {"ok": True, "session_name": name, "title": title}

    def get_sessions(self):
        return self._groups

    def rename_session(self, old, new):
        """模拟星瑶侧的会话改名（名字与展示名一起变）。"""
        for group in self._groups:
            for item in group.get("sessions") or []:
                if item.get("name") == old:
                    item["name"] = new
                    item["display_name"] = new

    def send_user_message(self, text, session_name=None, turn_source="local"):
        if self.fail_send:
            return {"ok": False, "msg": "会话忙"}
        self.sent.append({"text": text, "session_name": session_name, "turn_source": turn_source})
        return {"ok": True, "queued": False}


class _FakeApi:
    def __init__(self):
        self.texts = []
        self.cards = []

    def send_text(self, chat_id, text, reply_to=None):
        self.texts.append({"chat_id": chat_id, "text": text})
        return {"code": 0, "data": {"message_id": f"om_t{len(self.texts)}"}}

    def send_card(self, chat_id, card, reply_to=None):
        message_id = f"om_{len(self.cards) + 1}"
        self.cards.append({"chat_id": chat_id, "card": card, "message_id": message_id})
        return {"code": 0, "data": {"message_id": message_id}}


def _bridge(tmp_path, star=None):
    star = star or _FakeStarBridge()
    api = _FakeApi()
    store = FeishuBindingStore(path=str(tmp_path / "feishu_state.json"))
    br = FeishuBridge(
        bridge=star,
        api=api,
        store=store,
        config_loader=lambda: FeishuConfig(enabled=True, app_id="cli_x", app_secret="s"),
    )
    return br, star, api


def _message_event(text="你好", chat_id="oc_1", open_id="ou_abcdefgh", msg_type="text", message_id="om_1"):
    return {
        "header": {"event_id": "ev-1", "event_type": "im.message.receive_v1"},
        "event": {
            "sender": {"sender_id": {"open_id": open_id}},
            "message": {
                "chat_id": chat_id,
                "message_id": message_id,
                "message_type": msg_type,
                "content": json.dumps({"text": text}, ensure_ascii=False),
            },
        },
    }


def _card_action_event(kind="reply", form_value=None, chat_id="oc_1", open_id="ou_abcdefgh",
                       via_form_submit=False, message_id=""):
    """构造卡片回调事件。

    via_form_submit=True 模拟**表单提交按钮**的真实载荷：只有 `action.name`，
    没有 `action.value`（提交按钮不带 behaviors）。
    message_id 对应飞书回调里的 context.open_message_id（被点卡片的消息 id）。
    """
    action: dict = {"form_value": form_value or {}}
    if via_form_submit:
        action["name"] = kind
    else:
        action["value"] = {"kind": kind}
    return {
        "header": {"event_id": "ev-2", "event_type": "card.action.trigger"},
        "event": {
            "operator": {"open_id": open_id},
            "action": action,
            "context": {"open_chat_id": chat_id, "open_message_id": message_id},
        },
    }


# ---------------------------------------------------------------- 消息 → 回合

def test_message_creates_session_binds_and_sends_turn(tmp_path):
    br, star, _ = _bridge(tmp_path)

    br.on_feishu_event(_message_event("帮我看看 README"))

    assert len(star.created) == 1
    assert star.created[0]["title"] == "飞书 · ou_abcde"
    assert star.sent == [{
        "text": "帮我看看 README",
        "session_name": "session_000000",
        "turn_source": "feishu",
    }]
    assert br._store.session_of("oc_1") == "session_000000"
    assert br._store.label_of("oc_1") == "飞书 · ou_abcde"


def test_second_message_reuses_binding(tmp_path):
    br, star, _ = _bridge(tmp_path)

    br.on_feishu_event(_message_event("第一条"))
    br.on_feishu_event(_message_event("第二条", message_id="om_2"))

    assert len(star.created) == 1                      # 不重复建会话
    assert [s["text"] for s in star.sent] == ["第一条", "第二条"]
    assert {s["session_name"] for s in star.sent} == {"session_000000"}


def test_group_mention_is_stripped(tmp_path):
    br, star, _ = _bridge(tmp_path)

    br.on_feishu_event(_message_event("@_user_1 帮我看下这个"))

    assert star.sent[0]["text"] == "帮我看下这个"


def test_non_text_message_gets_hint_without_turn(tmp_path):
    br, star, api = _bridge(tmp_path)

    br.on_feishu_event(_message_event(msg_type="image"))

    assert star.sent == []
    assert star.created == []
    assert "只支持文本消息" in api.texts[0]["text"]


def test_send_failure_reports_error_and_drops_pending(tmp_path):
    br, star, api = _bridge(tmp_path)
    star.fail_send = True

    br.on_feishu_event(_message_event("你好"))

    assert "发送到星瑶失败" in api.texts[0]["text"]
    # 失败后不得留下 pending，否则后续桌面回合会被误推飞书
    assert br._pending == {}
    assert api.cards == []


# ---------------------------------------------------------------- 回合结果 → 飞书

def test_turn_result_pushes_completion_card(tmp_path):
    br, star, api = _bridge(tmp_path)
    br.on_feishu_event(_message_event("列一下目录"))
    session = star.sent[0]["session_name"]

    br.on_agent_event("message_final", {"session_name": session, "content": "共 12 个文件"})
    br.on_agent_event("turn_finish", {"session_name": session})

    assert len(api.cards) == 1
    assert api.cards[0]["chat_id"] == "oc_1"
    card = api.cards[0]["card"]
    assert card["header"]["title"]["content"] == "✅ 星瑶已完成"
    assert card["header"]["template"] == "green"
    body = json.dumps(card, ensure_ascii=False)
    assert "共 12 个文件" in body
    assert "飞书 · ou_abcde" in body
    assert br._pending == {}


def test_desktop_turn_does_not_push_card(tmp_path):
    """桌面端自己的回合（没有飞书 pending）不得产生飞书消息。"""
    br, _, api = _bridge(tmp_path)

    br.on_agent_event("message_final", {"session_name": "session_000000", "content": "桌面回合"})
    br.on_agent_event("turn_finish", {"session_name": "session_000000"})

    assert api.cards == []


def test_turn_error_pushes_failure_card(tmp_path):
    br, star, api = _bridge(tmp_path)
    br.on_feishu_event(_message_event("会失败的活"))
    session = star.sent[0]["session_name"]

    br.on_agent_event("turn_error", {"session_name": session, "message": "模型调用失败"})
    br.on_agent_event("turn_finish", {"session_name": session})

    card = api.cards[0]["card"]
    assert card["header"]["title"]["content"] == "⚠️ 星瑶执行出错"
    assert card["header"]["template"] == "red"
    assert "模型调用失败" in json.dumps(card, ensure_ascii=False)


def test_turn_cancelled_pushes_orange_card(tmp_path):
    br, star, api = _bridge(tmp_path)
    br.on_feishu_event(_message_event("中止我"))
    session = star.sent[0]["session_name"]

    br.on_agent_event("turn_cancelled", {"session_name": session})

    card = api.cards[0]["card"]
    assert card["header"]["title"]["content"] == "⏹ 星瑶已中止"
    assert card["header"]["template"] == "orange"


def test_completion_card_carries_reply_form(tmp_path):
    br, star, api = _bridge(tmp_path)
    br.on_feishu_event(_message_event("你好"))
    session = star.sent[0]["session_name"]
    br.on_agent_event("turn_finish", {"session_name": session})

    elements = api.cards[0]["card"]["body"]["elements"]
    form = next(e for e in elements if e.get("tag") == "form")
    assert form["elements"][0]["tag"] == "input"
    assert form["elements"][0]["name"] == "reply_text"
    submit = form["elements"][1]
    assert submit["form_action_type"] == "submit"
    assert submit["name"] == "reply"
    # 按钮回调必须走 behaviors（卡片 2.0 红线 2）
    buttons = [e for e in elements if e.get("tag") == "button"]
    for button in buttons:
        assert button["behaviors"][0]["type"] == "callback"


# ---------------------------------------------------------------- 卡片追问

def test_card_reply_sends_turn_and_returns_raw_card(tmp_path):
    """真实形态：完成卡的表单提交按钮（只有 action.name，没有 action.value）。"""
    br, star, _ = _bridge(tmp_path)
    br.on_feishu_event(_message_event("第一轮"))
    session = star.sent[0]["session_name"]

    response = br.on_feishu_event(
        _card_action_event("reply", form_value={"reply_text": "继续深挖"}, via_form_submit=True)
    )

    assert star.sent[-1]["text"] == "继续深挖"
    assert star.sent[-1]["session_name"] == session
    assert star.sent[-1]["turn_source"] == "feishu"
    # 回调结果卡必须包 raw（红线 3）
    assert response["card"]["type"] == "raw"
    assert response["card"]["data"]["header"]["template"] == "green"


def test_card_reply_via_value_payload_also_works(tmp_path):
    """带 value 的按钮形态（非表单提交）同样要能路由。"""
    br, star, _ = _bridge(tmp_path)
    br.on_feishu_event(_message_event("第一轮"))

    br.on_feishu_event(_card_action_event("reply", form_value={"reply_text": "按钮路径"}))

    assert star.sent[-1]["text"] == "按钮路径"


def test_card_reply_with_empty_form_returns_warning(tmp_path):
    br, star, _ = _bridge(tmp_path)

    response = br.on_feishu_event(_card_action_event(form_value={}))

    assert star.sent == []
    assert response["card"]["type"] == "raw"
    assert "没有内容" in response["card"]["data"]["header"]["title"]["content"]


# ---------------------------------------------------------------- 原地回执不冲掉原内容

def test_reply_keeps_original_card_content(tmp_path):
    """在完成卡上追问后，卡片必须**保留星瑶的回复**，只追加一行状态。

    真机反馈：直接返回小回执卡会把原内容冲掉（飞书是原地替换语义）。
    """
    br, star, api = _bridge(tmp_path)
    br.on_feishu_event(_message_event("第一轮"))
    session = star.sent[0]["session_name"]
    br.on_agent_event("message_final", {"session_name": session, "content": "我是星瑶，可以帮你写代码"})
    br.on_agent_event("turn_finish", {"session_name": session})
    completion_id = api.cards[0]["message_id"]

    response = br.on_feishu_event(_card_action_event(
        "reply", form_value={"reply_text": "再详细点"}, via_form_submit=True, message_id=completion_id))

    card = response["card"]["data"]
    body = json.dumps(card, ensure_ascii=False)
    assert "我是星瑶，可以帮你写代码" in body      # 原内容保留
    assert "✅ 已发送" in body                      # 追加了状态行
    assert card["header"]["title"]["content"] == "✅ 星瑶已完成"   # 卡片本体没被换成小回执卡


def test_repeated_replies_do_not_accumulate_status_lines(tmp_path):
    br, star, api = _bridge(tmp_path)
    br.on_feishu_event(_message_event("第一轮"))
    session = star.sent[0]["session_name"]
    br.on_agent_event("message_final", {"session_name": session, "content": "结果正文"})
    br.on_agent_event("turn_finish", {"session_name": session})
    completion_id = api.cards[0]["message_id"]

    first = br.on_feishu_event(_card_action_event(
        "reply", form_value={"reply_text": "追问一"}, via_form_submit=True, message_id=completion_id))
    second = br.on_feishu_event(_card_action_event(
        "reply", form_value={"reply_text": "追问二"}, via_form_submit=True, message_id=completion_id))

    assert json.dumps(first["card"]["data"], ensure_ascii=False).count("✅ 已发送") == 1
    assert json.dumps(second["card"]["data"], ensure_ascii=False).count("✅ 已发送") == 1


def test_reply_without_known_card_falls_back_to_result_card(tmp_path):
    """取不到原件（超出保留上限/非本进程发的卡）时退回独立小回执卡。"""
    br, star, _ = _bridge(tmp_path)

    response = br.on_feishu_event(_card_action_event(
        "reply", form_value={"reply_text": "追问"}, via_form_submit=True, message_id="om_unknown"))

    card = response["card"]["data"]
    assert card["header"]["title"]["content"] == "✅ 已发送"
    assert star.sent[-1]["text"] == "追问"


def test_unknown_event_type_is_ignored(tmp_path):
    br, _, _ = _bridge(tmp_path)
    assert br.on_feishu_event({"header": {"event_type": "im.chat.updated_v1"}}) is None


# ---------------------------------------------------------------- 持久化

def test_bindings_survive_store_reload(tmp_path):
    path = str(tmp_path / "feishu_state.json")
    store = FeishuBindingStore(path=path)
    store.bind("oc_1", "session_abc", "飞书 · ou_abcde")
    store.set_last_chat("oc_1")

    reloaded = FeishuBindingStore(path=path)
    assert reloaded.session_of("oc_1") == "session_abc"
    assert reloaded.label_of("oc_1") == "飞书 · ou_abcde"
    assert reloaded.last_chat_id() == "oc_1"
    assert reloaded.all_bindings()["oc_1"]["session"] == "session_abc"


# ---------------------------------------------------------------- 会话改名（真机缺陷回归）

def test_session_rename_mid_turn_still_pushes_card(tmp_path):
    """星瑶会在回合中把会话自动重命名（首条消息当标题）。

    真机现象：回合跑完了却收不到任何回复——`turn_finish` 带的是改名后的名字，
    桥接层按旧名索引 pending，找不到就静默返回（连报错都没有）。
    本用例固定：改名后 message_final（旧名）与 turn_finish（新名）都要落到同一轮。
    """
    br, star, api = _bridge(tmp_path)
    br.on_feishu_event(_message_event("你是谁啊，能做什么"))
    old = star.sent[0]["session_name"]

    # 回合中改名（星瑶实际顺序：session_renamed → message_final(旧名) → turn_finish(新名)）
    br.on_agent_event("session_renamed", {"old": old, "new": "你是谁啊，能做什么"})
    br.on_agent_event("message_final", {"session_name": old, "content": "我是星瑶，可以帮你写代码"})
    br.on_agent_event("turn_finish", {"session_name": "你是谁啊，能做什么"})

    assert len(api.cards) == 1, "改名后回合结果必须仍然发出"
    body = json.dumps(api.cards[0]["card"], ensure_ascii=False)
    assert "我是星瑶，可以帮你写代码" in body
    assert "你是谁啊，能做什么" in body            # 卡片展示用新名字
    assert br._pending == {}
    assert br._session_aliases == {}              # 别名随本轮清理


def test_session_rename_migrates_binding(tmp_path):
    """改名后绑定必须随迁：否则下一条消息找不到会话会另建，用户侧表现为「换了话题」。"""
    br, star, _ = _bridge(tmp_path)
    br.on_feishu_event(_message_event("第一条"))
    old = star.sent[0]["session_name"]

    br.on_agent_event("session_renamed", {"old": old, "new": "改过的标题"})
    star.rename_session(old, "改过的标题")          # 星瑶侧改名生效

    br.on_feishu_event(_message_event("第二条", message_id="om_2"))

    assert len(star.created) == 1, "改名后不应再新建会话"
    assert star.sent[-1]["session_name"] == "改过的标题"
    assert br._store.session_of("oc_1") == "改过的标题"
    assert br._store.label_of("oc_1") == "改过的标题"


def test_session_rename_without_pending_is_noop(tmp_path):
    """桌面端自己的会话改名与飞书无关，不得影响飞书状态。"""
    br, _, api = _bridge(tmp_path)

    br.on_agent_event("session_renamed", {"old": "session_desktop", "new": "桌面标题"})

    assert br._pending == {}
    assert api.cards == []


def test_session_rename_ignores_missing_or_same_fields(tmp_path):
    br, _, _ = _bridge(tmp_path)
    br.on_agent_event("session_renamed", {"old": "a"})               # 缺 new
    br.on_agent_event("session_renamed", {"old": "a", "new": "a"})   # 同名
    assert br._session_aliases == {}


# ---------------------------------------------------------------- 端到端（穿过服务层）

def test_end_to_end_through_service(tmp_path):
    """飞书事件 → FeishuService（去重分发）→ 桥接 → 回合 → 完成卡回发。"""
    from core.feishu.service import FeishuService

    br, star, api = _bridge(tmp_path)
    svc = FeishuService(bridge=br)

    payload = json.dumps(_message_event("端到端跑一轮")).encode("utf-8")
    assert svc._handle_event(payload) is None
    assert svc._handle_event(payload) is None            # 重投被去重
    assert len(star.sent) == 1
    assert star.sent[0]["turn_source"] == "feishu"
    session = star.sent[0]["session_name"]

    svc.on_event("message_final", {"session_name": session, "content": "端到端结果"})
    svc.on_event("turn_finish", {"session_name": session})

    assert len(api.cards) == 1
    assert "端到端结果" in json.dumps(api.cards[0]["card"], ensure_ascii=False)

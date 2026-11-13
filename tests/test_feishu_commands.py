"""T6：飞书指令集、收件箱、卡住提醒、模型切换、功能面板单测。不触网。"""

import json
import time


from core.feishu.bridge import FeishuBindingStore, FeishuBridge
from core.feishu.config import FeishuConfig


class _FakeStarBridge:
    def __init__(self):
        self.created = []
        self.sent = []
        self.models_set = []
        self.stopped = []
        self.sessions = [
            {"name": "session_a", "display_name": "重构鉴权", "workdir": "GLOBAL"},
            {"name": "session_b", "display_name": "会话条目 2", "workdir": "D:/proj"},
        ]
        self.models = [
            {"id": "gpt-4o", "name": "GPT-4o", "provider": "openai"},
            {"id": "claude", "name": "Claude", "provider": "anthropic"},
        ]
        self.active_model_id = "gpt-4o"
        self.permission_mode = "confirm"
        self.mode_set = []
        self.sandbox_set = []

    def get_sessions(self):
        """按工作目录分组（与后端一致）：自由对话一组，其余每个目录一组。"""
        global_group = {
            "folder_path": "GLOBAL", "folder_name": "对话", "is_global": True, "is_current": True,
            "sessions": [s for s in self.sessions if (s.get("workdir") or "GLOBAL") == "GLOBAL"],
        }
        by_dir: dict = {}
        for item in self.sessions:
            wd = str(item.get("workdir") or "")
            if not wd or wd == "GLOBAL":
                continue
            by_dir.setdefault(wd, []).append(item)
        groups = [global_group]
        for path in sorted(by_dir):
            folder = path.replace("\\", "/").rstrip("/").split("/")[-1] or path
            groups.append({"folder_path": path, "folder_name": folder, "is_global": False,
                           "is_current": False, "sessions": by_dir[path]})
        return groups

    def create_session(self, title, workdir=None):
        name = f"session_new{len(self.created)}"
        self.created.append({"title": title, "workdir": workdir, "name": name})
        self.sessions.append({"name": name, "display_name": title,
                              "workdir": workdir or "GLOBAL"})
        return {"ok": True, "session_name": name, "title": title}

    def send_user_message(self, text, session_name=None, turn_source="local"):
        self.sent.append({"text": text, "session_name": session_name, "turn_source": turn_source})
        return {"ok": True}

    def get_model_settings(self):
        return {"models": list(self.models), "active_model_id": self.active_model_id}

    def set_model(self, model_id, session_name=None):
        self.models_set.append({"model_id": model_id, "session": session_name})
        self.active_model_id = model_id
        return {"ok": True}

    def stop_current_turn(self, session_name=""):
        self.stopped.append(session_name)
        return {"ok": True}

    def get_permission_mode(self, session_name=""):
        return {"ok": True, "session_name": session_name, "permission_mode": self.permission_mode}

    def set_permission_mode(self, mode, session_name=""):
        self.mode_set.append({"mode": mode, "session": session_name})
        if mode not in ("confirm", "accept_edits", "plan", "full"):
            # 星瑶会显式拒绝未知档位，不静默兜底
            return {"ok": False, "msg": f"未知权限模式: {mode}"}
        self.permission_mode = mode
        return {"ok": True, "permission_mode": mode, "bound": True}

    def set_sandbox_enabled(self, enabled):
        self.sandbox_set.append(bool(enabled))
        return {"ok": True, "allow_outside_workspace": bool(enabled)}

    def respond_permission(self, request_id, allow, always=False):
        return {"ok": True}

    def respond_permission_choice(self, request_id, choice_index, custom_text=""):
        return {"ok": True}


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


# 白名单按 open_id 校验；默认值与 _message 的发件人一致，避免"自己都不在白名单里"
def _setup(tmp_path, approvers=("ou_abcdefgh",), bound_session="session_a", state="connected"):
    star = _FakeStarBridge()
    api = _FakeApi()
    store = FeishuBindingStore(path=str(tmp_path / "state.json"))
    store.set_last_chat("oc_1")
    if bound_session:
        store.bind("oc_1", bound_session, "飞书 · ou_abcde")
    cfg = FeishuConfig(enabled=True, app_id="cli_x", app_secret="s", approvers=list(approvers))
    br = FeishuBridge(bridge=star, api=api, store=store, config_loader=lambda: cfg)
    br.set_status_provider(lambda: {"state": state})
    return br, star, api


def _message(text, chat_id="oc_1", open_id="ou_abcdefgh"):
    return {
        "header": {"event_id": f"ev-{text}", "event_type": "im.message.receive_v1"},
        "event": {
            "sender": {"sender_id": {"open_id": open_id}},
            "message": {"chat_id": chat_id, "message_id": "om_in", "message_type": "text",
                        "content": json.dumps({"text": text}, ensure_ascii=False)},
        },
    }


def _cmd_action(cmd, arg="", open_id="ou_allowed"):
    return {
        "header": {"event_id": f"ev-c-{cmd}-{arg}", "event_type": "card.action.trigger"},
        "event": {
            "operator": {"open_id": open_id},
            "action": {"value": {"kind": "cmd", "cmd": cmd, "arg": arg}},
            "context": {"open_chat_id": "oc_1"},
        },
    }


def _card_markdown(card):
    """卡片里的 markdown 正文（避开 JSON 转义，路径断言用这个）。"""
    return "\n".join(str(e.get("content") or "") for e in card["body"]["elements"]
                     if e.get("tag") == "markdown")


def _card_text(card):
    return json.dumps(card, ensure_ascii=False)


# ---------------------------------------------------------------- 指令

def test_help_returns_instruction_card(tmp_path):
    br, _, api = _setup(tmp_path)

    br.on_feishu_event(_message("/help"))

    assert api.cards[0]["card"]["header"]["title"]["content"] == "星瑶 · 帮助"
    assert "/panel" in _card_text(api.cards[0]["card"])
    assert api.cards[0]["chat_id"] == "oc_1"


def test_panel_card_buttons_are_commands(tmp_path):
    br, _, api = _setup(tmp_path)

    br.on_feishu_event(_message("/panel"))

    card = api.cards[0]["card"]
    values = [b["behaviors"][0]["value"] for e in card["body"]["elements"] if e.get("tag") == "column_set"
              for c in e["columns"] for b in c["elements"]]
    cmds = {v.get("cmd") for v in values}
    assert {"/inbox", "/status", "/list", "/new", "/stop", "/model", "/agent"} <= cmds
    assert all(v["kind"] == "cmd" for v in values)


def test_status_card_shows_binding_and_state(tmp_path):
    br, _, api = _setup(tmp_path, state="connected")

    br.on_feishu_event(_message("/status"))

    body = _card_text(api.cards[0]["card"])
    assert "connected" in body
    assert "session_a" in body
    assert "审批白名单**　1 人" in body


def test_status_card_warns_when_no_approvers(tmp_path):
    br, _, api = _setup(tmp_path, approvers=())

    br.on_feishu_event(_message("/status"))

    assert "未配置审批白名单" in _card_text(api.cards[0]["card"])


def test_new_creates_and_binds_session(tmp_path):
    br, star, api = _setup(tmp_path)

    br.on_feishu_event(_message("/new 我的新会话"))

    assert star.created == [{"title": "我的新会话", "workdir": "GLOBAL", "name": "session_new0"}]
    assert br._store.session_of("oc_1") == "session_new0"
    assert "已新建并绑定" in api.cards[0]["card"]["header"]["title"]["content"]


def test_new_defaults_to_global_workdir_never_none(tmp_path):
    """回归：新建会话必须显式传 GLOBAL。

    后端 `create_session(workdir=None)` 的默认是**进程当前目录**（os.getcwd()），
    传 None 会把飞书新会话悄悄绑到星瑶进程所在目录（真机表现为"工作目录莫名其妙"）。
    """
    br, star, api = _setup(tmp_path)

    br.on_feishu_event(_message("/new"))

    assert star.created[0]["workdir"] == "GLOBAL"
    assert star.created[0]["workdir"] is not None
    body = _card_text(api.cards[0]["card"])
    assert "自由对话" in body
    assert "改为绑定项目目录" in body          # 回执卡给出绑定目录的入口


def test_newdir_picker_lists_project_dirs(tmp_path):
    br, star, api = _setup(tmp_path)
    star.sessions.append({"name": "session_p", "display_name": "项目会话", "workdir": r"D:\proj\demo"})

    br.on_feishu_event(_message("/newdir"))

    card = api.cards[0]["card"]
    assert card["header"]["title"]["content"] == "📁 选择项目目录"
    body = _card_text(card)
    assert "demo" in body and r"D:\proj\demo" in _card_markdown(card)
    values = [b["behaviors"][0]["value"] for e in card["body"]["elements"] if e.get("tag") == "column_set"
              for c in e["columns"] for b in c["elements"]]
    assert {"kind": "cmd", "cmd": "/newdir", "arg": r"D:\proj\demo"} in values
    assert {"kind": "cmd", "cmd": "/new"} in values      # 也给自由对话入口


def test_newdir_binds_session_to_directory(tmp_path):
    br, star, api = _setup(tmp_path)

    br.on_feishu_event(_message(r"/newdir D:\proj\demo"))

    assert star.created == [{"title": "demo", "workdir": r"D:\proj\demo", "name": "session_new0"}]
    assert br._store.session_of("oc_1") == "session_new0"
    body = _card_text(api.cards[0]["card"])
    assert r"D:\proj\demo" in _card_markdown(api.cards[0]["card"])


def test_list_shows_workdir_and_global_marker(tmp_path):
    """列表每项显示工作目录（与桌面侧栏一致）：GLOBAL 显示"自由对话"，项目会话显示目录。"""
    br, star, api = _setup(tmp_path)
    star.sessions.append({"name": "session_g", "display_name": "自由聊", "workdir": "GLOBAL"})

    br.on_feishu_event(_message("/list"))

    body = _card_text(api.cards[0]["card"])
    assert "自由对话" in body                        # GLOBAL 会话显示"自由对话"
    assert "D:/proj" in body or "D:\\proj" in body   # 项目会话显示其目录


def test_list_card_marks_current_and_offers_switch(tmp_path):
    br, _, api = _setup(tmp_path)

    br.on_feishu_event(_message("/list"))

    body = _card_text(api.cards[0]["card"])
    assert "重构鉴权" in body and "（当前）" in body
    assert "会话条目 2" in body
    values = [b["behaviors"][0]["value"] for e in api.cards[0]["card"]["body"]["elements"]
              if e.get("tag") == "column_set" for c in e["columns"] for b in c["elements"]]
    assert {"kind": "cmd", "cmd": "/switch", "arg": "2"} in values


def test_list_pagination_is_stateless(tmp_path):
    br, star, api = _setup(tmp_path)
    for i in range(10):
        star.sessions.append({"name": f"session_x{i}", "display_name": f"批量 {i}", "workdir": "GLOBAL"})

    br.on_feishu_event(_message("/list"))

    body = _card_text(api.cards[0]["card"])
    assert "第 1/2 页" in body
    assert "批量 9" not in body                      # 第 2 页才出现


def test_switch_binds_by_index(tmp_path):
    br, _, api = _setup(tmp_path)

    br.on_feishu_event(_message("/switch 2"))

    assert br._store.session_of("oc_1") == "session_b"
    assert "已切换绑定" in api.cards[0]["card"]["header"]["title"]["content"]


def test_switch_rejects_bad_index(tmp_path):
    br, _, api = _setup(tmp_path)

    br.on_feishu_event(_message("/switch abc"))
    br.on_feishu_event(_message("/switch 99"))

    assert "用法：/switch" in api.texts[0]["text"]
    assert "序号超出范围" in api.texts[1]["text"]
    assert br._store.session_of("oc_1") == "session_a"   # 绑定不变


def test_model_list_and_switch_by_index_and_id(tmp_path):
    br, star, api = _setup(tmp_path)

    br.on_feishu_event(_message("/model"))
    assert "GPT-4o" in _card_text(api.cards[0]["card"])
    assert "（当前）" in _card_text(api.cards[0]["card"])

    br.on_feishu_event(_message("/model 2"))
    assert star.models_set[-1]["model_id"] == "claude"
    assert star.models_set[-1]["session"] == "session_a"

    br.on_feishu_event(_message("/model gpt-4o"))
    assert star.models_set[-1]["model_id"] == "gpt-4o"


def test_model_unknown_target_reports(tmp_path):
    br, star, api = _setup(tmp_path)

    br.on_feishu_event(_message("/model 不存在的模型"))

    assert star.models_set == []
    assert "没找到模型" in api.texts[0]["text"]


def test_agent_card_is_read_only(tmp_path):
    br, _, api = _setup(tmp_path)

    br.on_feishu_event(_message("/agent"))

    body = _card_text(api.cards[0]["card"])
    assert "人设" in body
    assert "没有多 Agent 切换" in body


def test_stop_sends_stop_request(tmp_path):
    br, star, api = _setup(tmp_path)

    br.on_feishu_event(_message("/stop"))

    assert star.stopped == ["session_a"]
    assert "已发送中止请求" in api.texts[0]["text"]


def test_stop_without_binding_reports(tmp_path):
    br, star, api = _setup(tmp_path, bound_session="")

    br.on_feishu_event(_message("/stop"))

    assert star.stopped == []
    assert "当前没有绑定会话" in api.texts[0]["text"]


def test_unknown_command_reports(tmp_path):
    br, _, api = _setup(tmp_path)

    br.on_feishu_event(_message("/nonsense"))

    assert "未知指令" in api.texts[0]["text"]


def test_whoami_returns_open_id_for_whitelist(tmp_path):
    """白名单自助配置：发 /whoami 拿到「本应用视角」的 open_id。"""
    br, _, api = _setup(tmp_path, approvers=())

    br.on_feishu_event(_message("/whoami", open_id="ou_self1234"))

    text = api.texts[0]["text"]
    assert "ou_self1234" in text
    assert "oc_1" in text                     # 同时给出 chat_id，便于填固定推送落点
    assert "不在名单内" in text
    assert api.cards == []


def test_whoami_reports_when_already_in_whitelist(tmp_path):
    br, _, api = _setup(tmp_path, approvers=["ou_abcdefgh"])

    br.on_feishu_event(_message("/whoami"))

    assert "已在名单内" in api.texts[0]["text"]


def test_help_mentions_whoami(tmp_path):
    br, _, api = _setup(tmp_path)

    br.on_feishu_event(_message("/help"))

    assert "/whoami" in _card_text(api.cards[0]["card"])


def test_plain_text_still_goes_to_model(tmp_path):
    """回归：非指令消息不受指令集影响，仍直接发给绑定会话。"""
    br, star, _ = _setup(tmp_path)

    br.on_feishu_event(_message("帮我看下 README"))

    assert star.sent == [{"text": "帮我看下 README", "session_name": "session_a", "turn_source": "feishu"}]


# ---------------------------------------------------------------- 执行权限档位

def _patch_sandbox(monkeypatch, enabled: bool):
    """沙盒状态来自全局配置（core.model_manager.load_config）。"""
    import core.model_manager as mm

    monkeypatch.setattr(mm, "load_config", lambda: {"allow_outside_workspace": enabled})


def test_mode_card_shows_current_and_offers_switching(tmp_path):
    br, star, api = _setup(tmp_path, approvers=["ou_abcdefgh"])

    br.on_feishu_event(_message("/mode"))

    card = api.cards[0]["card"]
    assert card["header"]["title"]["content"] == "🔐 执行权限"
    body = _card_text(card)
    assert "变更前确认" in body and "← 当前" in body
    # 当前档位不给切换按钮，其余三个都给
    values = [b["behaviors"][0]["value"] for e in card["body"]["elements"] if e.get("tag") == "column_set"
              for c in e["columns"] for b in c["elements"]]
    args = {v.get("arg") for v in values}
    assert args == {"accept_edits", "plan", "full"}
    assert all(v["cmd"] == "/mode" for v in values)


def test_mode_switch_binds_to_bound_session(tmp_path):
    br, star, api = _setup(tmp_path, approvers=["ou_abcdefgh"])

    br.on_feishu_event(_message("/mode accept_edits"))

    assert star.mode_set == [{"mode": "accept_edits", "session": "session_a"}]
    text = _card_text(api.cards[0]["card"])
    assert "已切换执行权限" in text
    assert "accept_edits" in text


def test_mode_card_hides_buttons_without_approvers(tmp_path):
    """fail-closed：未配置白名单时只读展示，不给切换入口。"""
    br, star, api = _setup(tmp_path, approvers=())

    br.on_feishu_event(_message("/mode"))

    card = api.cards[0]["card"]
    assert [e for e in card["body"]["elements"] if e.get("tag") == "column_set"] == []
    assert "未配置审批白名单" in _card_text(card)


def test_mode_switch_refused_without_whitelist(tmp_path):
    br, star, api = _setup(tmp_path, approvers=())

    br.on_feishu_event(_message("/mode full"))

    assert star.mode_set == []                       # 不回调后端
    assert "无权操作" in api.cards[0]["card"]["header"]["title"]["content"]


def test_mode_switch_surfaces_unknown_mode_error(tmp_path):
    """星瑶会显式拒绝未知档位（不静默兜底成 confirm），错误必须原样回显。"""
    br, star, api = _setup(tmp_path, approvers=["ou_abcdefgh"])

    br.on_feishu_event(_message("/mode 乱写的档位"))

    assert "切换失败" in api.cards[0]["card"]["header"]["title"]["content"]
    assert "未知权限模式" in _card_text(api.cards[0]["card"])


def test_mode_switch_without_bound_session_reports(tmp_path):
    br, star, api = _setup(tmp_path, approvers=["ou_abcdefgh"], bound_session="")

    br.on_feishu_event(_message("/mode plan"))

    assert star.mode_set == []
    assert "未绑定会话" in api.cards[0]["card"]["header"]["title"]["content"]


# ---------------------------------------------------------------- 沙盒开关

def test_sandbox_card_shows_state_and_warns(tmp_path, monkeypatch):
    _patch_sandbox(monkeypatch, False)
    br, _, api = _setup(tmp_path, approvers=["ou_abcdefgh"])

    br.on_feishu_event(_message("/sandbox"))

    card = api.cards[0]["card"]
    assert card["header"]["title"]["content"] == "🧰 沙盒开关"
    body = _card_text(card)
    assert "已关闭" in body
    assert "全局" in body                             # 必须写明是全局放宽项
    values = [b["behaviors"][0]["value"] for e in card["body"]["elements"] if e.get("tag") == "column_set"
              for c in e["columns"] for b in c["elements"]]
    assert values == [{"kind": "cmd", "cmd": "/sandbox", "arg": "on"}]


def test_sandbox_card_reflects_enabled_state(tmp_path, monkeypatch):
    _patch_sandbox(monkeypatch, True)
    br, _, api = _setup(tmp_path, approvers=["ou_abcdefgh"])

    br.on_feishu_event(_message("/sandbox"))

    body = _card_text(api.cards[0]["card"])
    assert "已开启" in body
    assert "off" in body                              # 给出关闭按钮


def test_sandbox_turn_on_calls_backend(tmp_path, monkeypatch):
    _patch_sandbox(monkeypatch, False)
    br, star, api = _setup(tmp_path, approvers=["ou_abcdefgh"])

    br.on_feishu_event(_message("/sandbox on"))

    assert star.sandbox_set == [True]
    assert "已切换沙盒放行" in _card_text(api.cards[0]["card"])


def test_sandbox_turn_off_calls_backend(tmp_path, monkeypatch):
    _patch_sandbox(monkeypatch, True)
    br, star, api = _setup(tmp_path, approvers=["ou_abcdefgh"])

    br.on_feishu_event(_message("/sandbox off"))

    assert star.sandbox_set == [False]
    assert "已关闭沙盒放行" in _card_text(api.cards[0]["card"])


def test_sandbox_refused_without_whitelist(tmp_path, monkeypatch):
    _patch_sandbox(monkeypatch, False)
    br, star, api = _setup(tmp_path, approvers=[])

    br.on_feishu_event(_message("/sandbox on"))

    assert star.sandbox_set == []                     # 全局放宽项：非白名单一律不动
    assert "无权操作" in api.cards[0]["card"]["header"]["title"]["content"]


def test_sandbox_bad_argument_reports_usage(tmp_path, monkeypatch):
    _patch_sandbox(monkeypatch, False)
    br, star, api = _setup(tmp_path, approvers=["ou_abcdefgh"])

    br.on_feishu_event(_message("/sandbox 随便写"))

    assert star.sandbox_set == []
    card = api.cards[0]["card"]
    assert "参数不对" in card["header"]["title"]["content"]
    assert "用法" in _card_text(card)


def test_panel_has_mode_and_sandbox_buttons(tmp_path):
    br, _, api = _setup(tmp_path)

    br.on_feishu_event(_message("/panel"))

    cmds = {v.get("cmd") for e in api.cards[0]["card"]["body"]["elements"]
            if e.get("tag") == "column_set" for c in e["columns"] for b in c["elements"]
            for v in [b["behaviors"][0]["value"]]}
    assert {"/mode", "/sandbox"} <= cmds


# ---------------------------------------------------------------- 面板按钮 → 指令

def test_panel_button_runs_command(tmp_path):
    br, _, api = _setup(tmp_path)

    br.on_feishu_event(_cmd_action("/status"))

    assert api.cards[0]["card"]["header"]["title"]["content"] == "📈 星瑶 状态"


# ---------------------------------------------------------------- 收件箱

def _permission(request_id="req-1", function_name="run_command", summary="执行命令：ls", options=None):
    return {
        "session_name": "session_a",
        "request_id": request_id,
        "function_name": function_name,
        "summary": summary,
        "arguments": {"command": "ls"},
        "options": options or ["允许一次", "拒绝", "本任务放行"],
        "allow_always": True,
    }


def test_inbox_empty(tmp_path):
    br, _, api = _setup(tmp_path)

    br.on_feishu_event(_message("/inbox"))

    assert "没有等你拍板的事" in _card_text(api.cards[0]["card"])


def test_inbox_lists_pending_and_reopens_card(tmp_path):
    br, _, api = _setup(tmp_path)
    br.on_agent_event("permission_request", _permission())
    br.on_agent_event("permission_request", _permission(
        request_id="req-2", function_name="ask_user", summary="选择数据库", options=["SQLite"]))

    br.on_feishu_event(_message("/inbox"))
    body = _card_text(api.cards[-1]["card"])
    assert "[权限] 执行命令：ls" in body
    assert "[提问] 选择数据库" in body

    br.on_feishu_event(_message("/inbox open:req-2"))
    reopened = api.cards[-1]["card"]
    assert reopened["header"]["title"]["content"] == "❓ 星瑶提问"


def test_inbox_open_unknown_request(tmp_path):
    br, _, api = _setup(tmp_path)

    br.on_feishu_event(_message("/inbox open:req-missing"))

    assert "该待办已处理" in api.cards[0]["card"]["header"]["title"]["content"]


def test_permission_dismiss_clears_inbox(tmp_path):
    br, _, api = _setup(tmp_path)
    br.on_agent_event("permission_request", _permission())

    br.on_agent_event("permission_dismiss", {})
    br.on_feishu_event(_message("/inbox"))

    assert "没有等你拍板的事" in _card_text(api.cards[-1]["card"])


# ---------------------------------------------------------------- 卡住提醒

def test_stall_watchdog_pushes_once_with_cooldown(tmp_path):
    br, _, api = _setup(tmp_path)
    br.on_feishu_event(_message("跑个长任务"))
    session = "session_a"

    # 人为把活跃时间推回 20 分钟前
    with br._lock:
        br._pending[session]["last_activity"] = time.time() - 20 * 60

    br._check_stalls()
    assert "🐢 星瑶疑似卡住" in api.cards[-1]["card"]["header"]["title"]["content"]
    assert "已 20 分钟无任何输出" in _card_text(api.cards[-1]["card"])

    br._check_stalls()                                # 冷却期内不再提醒
    assert sum(1 for c in api.cards if "🐢" in c["card"]["header"]["title"]["content"]) == 1


def test_stall_not_triggered_when_activity_recent(tmp_path):
    br, _, api = _setup(tmp_path)
    br.on_feishu_event(_message("跑个短任务"))

    br._check_stalls()

    assert not any("🐢" in c["card"]["header"]["title"]["content"] for c in api.cards)


def test_activity_event_refreshes_stall_timer(tmp_path):
    br, _, api = _setup(tmp_path)
    br.on_feishu_event(_message("跑个长任务"))
    session = "session_a"
    with br._lock:
        br._pending[session]["last_activity"] = time.time() - 20 * 60

    br.on_agent_event("token_delta", {"session_name": session, "text": "输出中"})
    br._check_stalls()

    assert not any("🐢" in c["card"]["header"]["title"]["content"] for c in api.cards)

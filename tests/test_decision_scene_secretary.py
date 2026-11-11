# -*- coding: utf-8 -*-
"""secretary 场景接入测试：JEV 决定「这条任务派发到哪个会话」。

被测函数：``core/tools.py::dispatch_task``（秘书派发工具）。

隔离策略：
- ``tests/conftest.py`` 收集阶段已把 ``YCODE_DATA_DIR`` 指向临时目录；本文件再把
  ``config.YCODE_DIR`` monkeypatch 到每个用例的 tmp_path，绝不触碰真实 ``~/.ycode``；
- 秘书调度服务用 ``_FakeDispatchService`` 打桩并经 ``secretary_dispatch.register``
  注册——不建 DB、不起回合线程、不做任何真实 I/O；
- 决策请求打桩 ``decision_service._post``，网关真链路用例也零真实网络。

覆盖点：
JEV 关闭 → 派发目标 / 调用次数 / 返回文本与接入前逐字节一致；
决策返回候选 session_name → 目标变成它（并带可读说明）；
返回 "new" / 候选之外的名字 / decide 抛异常 / 候选为空 → 走原逻辑；
target 非空 → 完全不发决策；
state 只含任务描述截断 + 会话名 + workdir，不含历史与文件文本。
"""
import json
import os
import sys

import httpx
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import decision_gateway as gateway
from core import decision_service as svc
from core import secretary_dispatch
from core import tools as tools_mod

PROVIDER = {
    "id": "tr-1",
    "name": "基元律动",
    "base_url": "https://tokenrhythm.studio/v1",
    "api_key": "sk_key_one",
}

# 调度服务返回的固定派发结果：用于断言「返回文本逐字节一致」
DISPATCH_RESULT = {
    "ok": True,
    "target_session": "session_abc123",
    "display_name": "新会话",
    "workdir": "GLOBAL",
    "state": "queued",
}


# ---------------------------------------------------------------------------
# 测试基础设施
# ---------------------------------------------------------------------------

class _FakeDispatchService:
    """秘书调度服务打桩：记录两类调用，返回预设候选与派发结果。"""

    def __init__(self, candidates=None, *, search_error=None):
        self.candidates = list(candidates or [])
        self.search_error = search_error
        self.search_calls = []
        self.dispatch_calls = []

    def search_conversations(self, keyword="", date_from="", date_to="", workdir="",
                             limit=20):
        self.search_calls.append(
            {
                "keyword": keyword,
                "date_from": date_from,
                "date_to": date_to,
                "workdir": workdir,
                "limit": limit,
            }
        )
        if self.search_error is not None:
            raise self.search_error
        return [dict(row) for row in self.candidates]

    def dispatch_task(self, target="", prompt="", workdir="", model_id="",
                      permission_mode=""):
        self.dispatch_calls.append(
            {
                "target": target,
                "prompt": prompt,
                "workdir": workdir,
                "model_id": model_id,
                "permission_mode": permission_mode,
            }
        )
        return dict(DISPATCH_RESULT)


def _candidate(name, display=None, updated_at="2026-10-18 10:00:00"):
    """构造一条 search_conversations 返回行（字段与协议声明一致）。"""
    return {
        "session_name": name,
        "display_name": display or name,
        "is_secretary": False,
        "workdir": "GLOBAL",
        "updated_at": updated_at,
        "message_count": 3,
        "snippets": [{"role": "user", "excerpt": "会话历史正文不应进入 state", "at": updated_at}],
    }


def _mock_decide(monkeypatch, value):
    """打桩网关 decide；记录调用参数，返回指定答案（Exception 实例则抛出）。"""
    calls = []

    def fake(scene, state, question, **kwargs):
        calls.append(
            {"scene": scene, "state": state, "question": question, "kwargs": kwargs}
        )
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(gateway, "decide", fake)
    return calls


def _write_user_config(data_dir, providers):
    (data_dir / "config.json").write_text(
        json.dumps({"providers": providers}, ensure_ascii=False), encoding="utf-8"
    )


def _enable_secretary_scene(*, shadow=False):
    """写决策配置：全局 enabled + secretary 场景 enabled。"""
    return svc.save_decision_config(
        {
            "enabled": True,
            "shadow_mode": shadow,
            "scenes": {"secretary": {"enabled": True, "shadow": shadow}},
        }
    )


def _mock_no_network(monkeypatch):
    """护栏：JEV 关闭时任何真实决策请求都应让用例失败。"""
    calls = []

    def fake(url, headers, payload, timeout_seconds):  # pragma: no cover - 不应被调用
        calls.append(payload)
        raise AssertionError("JEV 关闭时不允许发起决策请求")

    monkeypatch.setattr(svc, "_post", fake)
    return calls


def _mock_post(monkeypatch, handler):
    """打桩 decision_service._post（网关链路真正发请求的唯一出口）。"""
    calls = []

    def fake(url, headers, payload, timeout_seconds):
        calls.append({"url": url, "payload": payload, "timeout": timeout_seconds})
        return handler(len(calls) - 1, calls[-1])

    monkeypatch.setattr(svc, "_post", fake)
    return calls


def _resp(code, body=None):
    return httpx.Response(code, json=body if body is not None else {"answers": {}})


def _audit_records(data_dir):
    records = []
    directory = data_dir / "decision_audit"
    if not directory.is_dir():
        return records
    for path in sorted(directory.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                records.append(json.loads(line))
    return records


def _expected_output(result=None):
    """接入前的返回文本：payload = dict(result) + setdefault("ok", True)。"""
    payload = dict(result or DISPATCH_RESULT)
    payload.setdefault("ok", True)
    return json.dumps(payload, ensure_ascii=False)


@pytest.fixture()
def data_dir(tmp_path, monkeypatch):
    """把 config.YCODE_DIR 重定向到 tmp_path，保证不读写真实 ~/.ycode。"""
    import config

    directory = tmp_path / "ycode"
    directory.mkdir()
    monkeypatch.setattr(config, "YCODE_DIR", str(directory), raising=False)
    svc._reset_runtime_state()
    yield directory
    svc._reset_runtime_state()


@pytest.fixture()
def service(monkeypatch):
    """注册打桩调度服务；用例可替换 ``service.candidates``。"""
    fake = _FakeDispatchService(
        [
            _candidate("会话甲", updated_at="2026-10-18 12:00:00"),
            _candidate("会话乙", updated_at="2026-10-18 11:00:00"),
        ]
    )
    secretary_dispatch.register(fake)
    yield fake
    secretary_dispatch.unregister()


# ---------------------------------------------------------------------------
# 1. JEV 全关：与接入前逐字节一致（返回文本 / 派发目标 / 调用次数）
# ---------------------------------------------------------------------------

def test_jev_disabled_matches_pre_integration(data_dir, service, monkeypatch):
    """JEV 全关：零决策请求、零会话检索，派发目标与返回文本完全不变。"""
    # 不写 decision.json = DEFAULT_CONFIG（enabled=false，scenes 全未启用）
    _write_user_config(data_dir, [PROVIDER])
    post_calls = _mock_no_network(monkeypatch)  # 任何决策请求都会 AssertionError

    out = tools_mod.dispatch_task(prompt="把 README 补一版")

    assert post_calls == [], "JEV 关闭时不允许有任何决策请求"
    assert _audit_records(data_dir) == [], "JEV 关闭时不允许有审计落盘"
    assert service.search_calls == [], "JEV 关闭时不允许白跑一次会话检索（调用次数必须不变）"
    assert len(service.dispatch_calls) == 1, "派发调用次数必须与接入前一致（恰好 1 次）"
    assert service.dispatch_calls[0]["target"] == "", "派发目标必须仍是调用方给的空串"
    assert out == _expected_output(), "返回文本必须与接入前逐字节一致"
    assert "decision_note" not in json.loads(out)


def test_scene_disabled_but_global_enabled_matches_pre_integration(
    data_dir, service, monkeypatch
):
    """全局开、secretary 场景关：等同 JEV 关闭（场景闸门先判，零额外调用）。"""
    _write_user_config(data_dir, [PROVIDER])
    svc.save_decision_config({"enabled": True, "shadow_mode": False})
    post_calls = _mock_no_network(monkeypatch)

    out = tools_mod.dispatch_task(prompt="整理本周会议纪要")

    assert post_calls == []
    assert service.search_calls == []
    assert [c["target"] for c in service.dispatch_calls] == [""]
    assert out == _expected_output()


def test_jev_disabled_keeps_explicit_target(data_dir, service, monkeypatch):
    """JEV 全关 + 显式 target：target 原样透传（回归：不改动原逻辑）。"""
    _write_user_config(data_dir, [PROVIDER])
    _mock_no_network(monkeypatch)

    out = tools_mod.dispatch_task(target="会话甲", prompt="跑一下测试")

    assert service.search_calls == []
    assert [c["target"] for c in service.dispatch_calls] == ["会话甲"]
    assert out == _expected_output()


def test_jev_disabled_empty_prompt_message_unchanged(data_dir, service, monkeypatch):
    """JEV 全关 + 空 prompt：仍是原来的报错文本，且不触发任何调度调用。"""
    _write_user_config(data_dir, [PROVIDER])
    _mock_no_network(monkeypatch)

    out = tools_mod.dispatch_task(prompt="   ")

    assert out == "🚨 派活失败：prompt（任务正文）不能为空。"
    assert service.search_calls == []
    assert service.dispatch_calls == []


# ---------------------------------------------------------------------------
# 2. 决策被采纳 → 目标换成候选会话
# ---------------------------------------------------------------------------

def test_decision_candidate_name_becomes_target(data_dir, service, monkeypatch):
    """决策返回候选里的 session_name：派发目标换成它，并留下可读说明。"""
    _write_user_config(data_dir, [PROVIDER])
    _enable_secretary_scene()
    calls = _mock_decide(monkeypatch, "会话乙")

    out = tools_mod.dispatch_task(prompt="把登录报错修掉", workdir="")

    assert len(calls) == 1
    assert calls[0]["scene"] == "secretary"
    assert calls[0]["kwargs"]["options"] == ["new", "会话甲", "会话乙"]
    assert calls[0]["kwargs"]["timeout_ms"] == 250
    assert [c["target"] for c in service.dispatch_calls] == ["会话乙"]
    assert service.dispatch_calls[0]["prompt"] == "把登录报错修掉", "任务正文必须原样透传"
    payload = json.loads(out)
    assert payload["decision_note"] == "已按决策模型建议派发到 会话乙"
    assert payload["target_session"] == DISPATCH_RESULT["target_session"]


def test_candidate_list_is_capped_and_recent_first(data_dir, service, monkeypatch):
    """候选上限 10 个、按 updated_at 倒序：options = ["new"] + 最近 10 个名字。"""
    _write_user_config(data_dir, [PROVIDER])
    _enable_secretary_scene()
    # 故意打乱顺序，让 updated_at 决定顺序（12 个 → 只取最近 10 个）
    service.candidates = [
        _candidate(f"会话{i:02d}", updated_at=f"2026-10-{(i % 28) + 1:02d} 09:00:00")
        for i in range(12)
    ]
    calls = _mock_decide(monkeypatch, None)

    tools_mod.dispatch_task(prompt="批量整理文档")

    options = calls[0]["kwargs"]["options"]
    assert len(options) == 11, "1 个 new + 最多 10 个候选"
    assert options[0] == "new"
    assert len(service.search_calls) == 1
    assert service.search_calls[0]["limit"] == 10
    names = options[1:]
    assert len(set(names)) == len(names) == 10


# ---------------------------------------------------------------------------
# 3. 各种回退：走原逻辑（target 保持空串 = 新建会话）
# ---------------------------------------------------------------------------

def test_decision_new_falls_back(data_dir, service, monkeypatch):
    """决策选 "new"（新建会话）：target 保持空串，返回文本不带说明。"""
    _write_user_config(data_dir, [PROVIDER])
    _enable_secretary_scene()
    calls = _mock_decide(monkeypatch, "new")

    out = tools_mod.dispatch_task(prompt="开个新坑写周报")

    assert len(calls) == 1
    assert [c["target"] for c in service.dispatch_calls] == [""]
    assert out == _expected_output()


@pytest.mark.parametrize(
    "dirty", ["会话丙", "", "  ", "new ", "会话乙的旧名字"],
    ids=["unknown", "empty", "blank", "new_with_space", "renamed"],
)
def test_decision_outside_candidates_falls_back(data_dir, service, monkeypatch, dirty):
    """决策答案不在候选内（脏数据 / 改名 / 空）：一律走原逻辑。"""
    _write_user_config(data_dir, [PROVIDER])
    _enable_secretary_scene()
    _mock_decide(monkeypatch, dirty)

    out = tools_mod.dispatch_task(prompt="派个活")

    assert [c["target"] for c in service.dispatch_calls] == [""]
    assert out == _expected_output()


def test_empty_candidates_skips_decision(data_dir, service, monkeypatch):
    """候选为空：根本不发决策（没有选择余地），走原逻辑。"""
    _write_user_config(data_dir, [PROVIDER])
    _enable_secretary_scene()
    service.candidates = []
    calls = _mock_decide(monkeypatch, "会话甲")

    out = tools_mod.dispatch_task(prompt="第一次派活，还没有历史会话")

    assert len(service.search_calls) == 1, "候选检索本身就是取出候选的唯一途径"
    assert calls == [], "没有候选就不允许发决策请求"
    assert [c["target"] for c in service.dispatch_calls] == [""]
    assert out == _expected_output()


def test_search_failure_skips_decision(data_dir, service, monkeypatch):
    """候选检索抛错（如服务拒绝参数）：吞掉异常，不发决策，走原逻辑。"""
    _write_user_config(data_dir, [PROVIDER])
    _enable_secretary_scene()
    service.search_error = secretary_dispatch.SecretaryDispatchError("请至少给出关键词")
    calls = _mock_decide(monkeypatch, "会话甲")

    out = tools_mod.dispatch_task(prompt="派个活")

    assert calls == []
    assert [c["target"] for c in service.dispatch_calls] == [""]
    assert out == _expected_output()


def test_decision_exception_falls_back(data_dir, service, monkeypatch):
    """decide 抛异常：静默回退，派活照常成功。"""
    _write_user_config(data_dir, [PROVIDER])
    _enable_secretary_scene()
    _mock_decide(monkeypatch, RuntimeError("决策服务炸了"))

    out = tools_mod.dispatch_task(prompt="派个活")

    assert [c["target"] for c in service.dispatch_calls] == [""]
    assert out == _expected_output()


def test_explicit_target_never_consults_decision(data_dir, service, monkeypatch):
    """target 非空：完全不发决策，连候选检索都不做。"""
    _write_user_config(data_dir, [PROVIDER])
    _enable_secretary_scene()
    calls = _mock_decide(monkeypatch, "会话乙")

    out = tools_mod.dispatch_task(target="会话甲", prompt="继续改昨天的文件")

    assert calls == [], "显式指定目标时不允许发决策请求"
    assert service.search_calls == [], "显式指定目标时不允许做候选检索"
    assert [c["target"] for c in service.dispatch_calls] == ["会话甲"]
    assert out == _expected_output()


# ---------------------------------------------------------------------------
# 4. 隐私约束：state 只含任务描述截断 + 会话名 + workdir
# ---------------------------------------------------------------------------

def test_state_contains_only_prompt_head_names_and_workdir(
    data_dir, service, monkeypatch
):
    """state 只放任务描述前 300 字符与会话名；不含历史、片段、文件内容。"""
    _write_user_config(data_dir, [PROVIDER])
    _enable_secretary_scene()
    calls = _mock_decide(monkeypatch, None)
    head = "任务要求：" + "甲" * 10
    prompt = head + "乙" * 400 + "｜会话历史机密标记" + "｜" + "sk-" + "a" * 24

    tools_mod.dispatch_task(prompt=prompt, workdir="/tmp/project-x")

    state = calls[0]["state"]
    assert state.startswith("任务描述：" + head)
    assert "甲" * 10 in state
    # 任务描述被截到 300 字符：第 300 个字符之后的内容一律不得出现在 state
    assert "会话历史机密标记" not in state
    assert "sk-" + "a" * 24 not in state
    assert len(state.split("任务描述：", 1)[1].split("\n", 1)[0]) == 300
    # 候选会话名（session_name + display_name）与 workdir 必须在场
    # （显示名与会话名相同时不重复渲染括号）
    assert "- 会话甲\n" in state
    assert "- 会话乙\n" in state
    assert state.endswith("工作目录：/tmp/project-x")
    # 检索片段（snippets 里的会话正文）绝不能进入 state
    assert "会话历史正文不应进入 state" not in state
    # 题目形状符合规范：上游要求 criteria 是「选项名 → 描述」的对象，不是数组
    question = calls[0]["question"]
    assert question["type"] == "choice"
    assert isinstance(question["criteria"], dict)
    assert set(question["criteria"]) == set(calls[0]["kwargs"]["options"])
    assert all(question["criteria"].values())
    assert "只依据任务描述与会话名判断" in question["instructions"]


def test_state_shows_display_name_when_different(data_dir, service, monkeypatch):
    """会话名与显示名不同时，state 里两者都给出（模型好判断语义）。"""
    _write_user_config(data_dir, [PROVIDER])
    _enable_secretary_scene()
    service.candidates = [_candidate("s_2026abc", display="项目甲")]
    calls = _mock_decide(monkeypatch, None)

    tools_mod.dispatch_task(prompt="补充单元测试")

    assert "- s_2026abc（项目甲）" in calls[0]["state"]
    assert calls[0]["kwargs"]["options"] == ["new", "s_2026abc"]


def test_state_workdir_falls_back_to_global(data_dir, service, monkeypatch):
    """未指定 workdir：state 里写 GLOBAL（自由对话），不写空串。"""
    _write_user_config(data_dir, [PROVIDER])
    _enable_secretary_scene()
    calls = _mock_decide(monkeypatch, None)

    tools_mod.dispatch_task(prompt="随便聊聊")

    assert calls[0]["state"].endswith("工作目录：GLOBAL")


# ---------------------------------------------------------------------------
# 5. 端到端（真实网关链路 + 打桩 HTTP）：live 模式采纳目标并回填审计
# ---------------------------------------------------------------------------

def test_live_mode_end_to_end_uses_decided_target(data_dir, service, monkeypatch):
    """live 模式走完「网关 → 服务 → 审计」全链路：目标变成 会话乙，审计 adopted。"""
    _write_user_config(data_dir, [PROVIDER])
    _enable_secretary_scene(shadow=False)
    post_calls = _mock_post(
        monkeypatch, lambda i, call: _resp(200, {"answers": {"q": {"choice": "会话乙"}}})
    )

    out = tools_mod.dispatch_task(prompt="接着修登录报错")

    assert len(post_calls) == 1, "live 模式应恰好发一次决策请求"
    assert [c["target"] for c in service.dispatch_calls] == ["会话乙"]
    payload = json.loads(out)
    assert payload["decision_note"] == "已按决策模型建议派发到 会话乙"
    record = _audit_records(data_dir)[-1]
    assert record["scene"] == "secretary"
    assert record["shadow"] is False
    assert record["adopted"] is True
    assert record["trace_id"]


def test_live_mode_illegal_answer_from_upstream_falls_back(data_dir, service, monkeypatch):
    """上游给出候选之外的答案：网关判非法 → 走原逻辑（不误派到脏目标）。"""
    _write_user_config(data_dir, [PROVIDER])
    _enable_secretary_scene(shadow=False)
    _mock_post(
        monkeypatch,
        lambda i, call: _resp(200, {"answers": {"q": {"choice": "根本不存在的会话"}}}),
    )

    out = tools_mod.dispatch_task(prompt="接着修登录报错")

    assert [c["target"] for c in service.dispatch_calls] == [""]
    assert out == _expected_output()
    assert _audit_records(data_dir)[-1]["adopted"] is False


def test_shadow_mode_observes_but_keeps_target(data_dir, service, monkeypatch):
    """影子模式：真实发请求并写审计，但结果丢弃，target 保持空串。"""
    _write_user_config(data_dir, [PROVIDER])
    _enable_secretary_scene(shadow=True)
    post_calls = _mock_post(
        monkeypatch, lambda i, call: _resp(200, {"answers": {"q": {"choice": "会话乙"}}})
    )

    out = tools_mod.dispatch_task(prompt="接着修登录报错")

    assert len(post_calls) == 1, "影子模式仍应发一次请求（只观测不采纳）"
    assert [c["target"] for c in service.dispatch_calls] == [""]
    assert out == _expected_output()
    record = _audit_records(data_dir)[-1]
    assert record["scene"] == "secretary"
    assert record["shadow"] is True
    assert record["adopted"] is False

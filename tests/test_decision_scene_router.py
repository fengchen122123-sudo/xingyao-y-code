# -*- coding: utf-8 -*-
"""router 场景接线测试：JEV 决定「本回合用主模型还是副模型」。

隔离策略（与 tests/test_decision_scene_search.py 一致）：
- ``tests/conftest.py`` 收集阶段已把 ``YCODE_DATA_DIR`` 指向临时目录；本文件再把
  ``config.YCODE_DIR`` monkeypatch 到每个用例的 tmp_path，绝不触碰真实 ``~/.ycode``；
- 副模型用 ``core.model_manager.get_secondary_model`` 打桩，决策用
  ``core.decision_gateway.decide`` 打桩（或打桩 ``decision_service._post`` 走真实网关
  链路）——全程零真实网络、零真实模型请求。

覆盖点：
1. decide 返回 None / "main" / 抛异常 → 不覆盖；
2. decide 返回 "secondary" 且副模型齐全 → 返回五元组且字段一一对应；
3. 副模型缺失 / 与主模型同名 / 缺 api_key / 缺 base_url / 基线超过副模型窗口 → 不覆盖；
4. turn_source 非 local、pending_model_id 非空、无用户输入 → 不覆盖；
5. state 只含用户输入、截断 500 字符、不含文件内容类文本；
6. router_scene_live()：off / shadow / live 三态判定；
7. 端到端（真实网关 + 打桩 HTTP）：live 模式返回副模型参数包，审计 adopted=true。
"""
import json
import os
import sys

import httpx
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import decision_gateway as gateway
from core import decision_service as svc
from core import model_manager
from core import turn_model_override as tmo

PROVIDER = {
    "id": "tr-1",
    "name": "基元律动",
    "base_url": "https://tokenrhythm.studio/v1",
    "api_key": "sk_main_key",
}

# 主（激活）模型：会话当前绑定的贵模型
MAIN_MODEL = {
    "litellm_model": "openai/gpt-5-main",
    "api_key": "sk_main_key",
    "base_url": "https://main.example.com/v1",
    "context_window": 400000,
    "id": "model-main",
    "name": "主模型",
}

# 副（便宜）模型：get_secondary_model() 的返回结构（键名与真实实现对齐）
SECONDARY_MODEL = {
    "litellm_model": "openai/gpt-5-mini",
    "api_key": "sk_secondary_key",
    "base_url": "https://secondary.example.com/v1",
    "context_window": 128000,
    "supports_vision": False,
    "supports_function_calling": True,
    "computer_protocol": None,
    "system_prompt_extra": "",
    "name": "副模型",
    "protocol": "openai",
    "provider_name": "便宜厂商",
    "id": "model-secondary",
}

USER_TEXT = "帮我解释一下这段报错是什么意思"


# ---------------------------------------------------------------------------
# 测试基础设施
# ---------------------------------------------------------------------------

def _mock_secondary(monkeypatch, secondary):
    """打桩 get_secondary_model：本次回合的副模型。"""
    monkeypatch.setattr(model_manager, "get_secondary_model", lambda: secondary)


def _mock_decide(monkeypatch, value):
    """打桩网关 decide；记录调用参数，返回指定答案。"""
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


def _resp(code, body=None):
    return httpx.Response(code, json=body if body is not None else {"answers": {}})


def _mock_post(monkeypatch, handler):
    """打桩 decision_service._post（网关链路真正发请求的唯一出口）。"""
    calls = []

    def fake(url, headers, payload, timeout_seconds):
        calls.append(
            {"url": url, "headers": headers, "payload": payload, "timeout": timeout_seconds}
        )
        return handler(len(calls) - 1, calls[-1])

    monkeypatch.setattr(svc, "_post", fake)
    return calls


def _mock_no_network(monkeypatch):
    """护栏：任何真实决策请求都视为失败（JEV 关闭时必须零请求）。"""
    calls = []

    def fake(url, headers, payload, timeout_seconds):  # pragma: no cover - 不应被调用
        calls.append(payload)
        raise AssertionError("JEV 关闭时不允许发起决策请求")

    monkeypatch.setattr(svc, "_post", fake)
    return calls


def _write_user_config(data_dir):
    (data_dir / "config.json").write_text(
        json.dumps({"providers": [PROVIDER]}, ensure_ascii=False), encoding="utf-8"
    )


def _enable_router(*, shadow=False):
    """写决策配置：全局 enabled + router 场景 enabled（三态由两个布尔组合）。"""
    return svc.save_decision_config(
        {
            "enabled": True,
            "shadow_mode": shadow,
            "scenes": {"router": {"enabled": True, "shadow": shadow, "timeout_ms": 250}},
        }
    )


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


# ---------------------------------------------------------------------------
# 1. 采纳：decide 返回 "secondary" 且副模型齐全
# ---------------------------------------------------------------------------

def test_secondary_picked_returns_five_tuple(monkeypatch):
    """决策选副模型：返回参数包，六个字段全部取自副模型配置。"""
    _mock_secondary(monkeypatch, SECONDARY_MODEL)
    _mock_decide(monkeypatch, "secondary")

    result = tmo.resolve_turn_model(MAIN_MODEL, user_text=USER_TEXT)

    assert result == {
        "litellm_model": "openai/gpt-5-mini",
        "api_key": "sk_secondary_key",
        "base_url": "https://secondary.example.com/v1",
        "context_window": 128000,
        "model_id": "model-secondary",
        "name": "副模型",
    }
    # model_id 必须与 litellm_model 成对取自副模型，不能残留主模型 id
    assert result["model_id"] != MAIN_MODEL["id"]


def test_secondary_picked_passes_protocol_expectations(monkeypatch):
    """题面形状 / 候选 / 超时符合规范（调用方拿到答案才能正确回退）。"""
    _mock_secondary(monkeypatch, SECONDARY_MODEL)
    calls = _mock_decide(monkeypatch, "secondary")

    tmo.resolve_turn_model(MAIN_MODEL, user_text=USER_TEXT)

    call = calls[0]
    assert call["scene"] == "router"
    assert call["kwargs"]["options"] == ("main", "secondary")
    assert call["kwargs"]["timeout_ms"] == tmo.ROUTER_TIMEOUT_MS
    assert call["question"]["type"] == "choice"
    # 上游 choice 题的 criteria 必须是「选项名 → 描述」的对象，不是数组
    assert set(call["question"]["criteria"]) == {"main", "secondary"}
    assert all(call["question"]["criteria"].values())
    assert "main=能力更强但更贵" in call["question"]["instructions"]
    assert "secondary=更便宜" in call["question"]["instructions"]
    # 决策输入不含任何模型密钥（防泄露到决策服务）
    assert "sk_secondary_key" not in json.dumps(call, ensure_ascii=False)
    assert "sk_main_key" not in json.dumps(call, ensure_ascii=False)


def test_baseline_equal_to_window_still_allowed(monkeypatch):
    """基线恰好等于副模型窗口：不算超窗（边界只拦 window < baseline）。"""
    _mock_secondary(monkeypatch, SECONDARY_MODEL)
    _mock_decide(monkeypatch, "secondary")

    result = tmo.resolve_turn_model(
        MAIN_MODEL, baseline_tokens=SECONDARY_MODEL["context_window"], user_text=USER_TEXT
    )

    assert result is not None


def test_baseline_zero_skips_window_guard(monkeypatch):
    """baseline_tokens=0（调用方拿不到基线）：跳过窗口护栏，仍可覆盖。"""
    _mock_secondary(monkeypatch, SECONDARY_MODEL)
    _mock_decide(monkeypatch, "secondary")

    result = tmo.resolve_turn_model(MAIN_MODEL, baseline_tokens=0, user_text=USER_TEXT)

    assert result is not None


# ---------------------------------------------------------------------------
# 2. 不覆盖：决策结论不是副模型 / 失败 / 异常
# ---------------------------------------------------------------------------

def test_decide_none_keeps_main_model(monkeypatch):
    """decide 返回 None（未启用/影子/失败/低置信）：不覆盖。"""
    _mock_secondary(monkeypatch, SECONDARY_MODEL)
    _mock_decide(monkeypatch, None)

    assert tmo.resolve_turn_model(MAIN_MODEL, user_text=USER_TEXT) is None


def test_decide_main_keeps_main_model(monkeypatch):
    """decide 返回 "main"：显式选择主模型，同样不覆盖。"""
    _mock_secondary(monkeypatch, SECONDARY_MODEL)
    _mock_decide(monkeypatch, "main")

    assert tmo.resolve_turn_model(MAIN_MODEL, user_text=USER_TEXT) is None


@pytest.mark.parametrize(
    "bad_answer",
    ["Secondary", "SECONDARY", " secondary ", "", "unknown", 1, 0.9, True],
    ids=["case", "upper", "padded", "empty", "unknown", "int", "float", "bool"],
)
def test_illegal_answer_keeps_main_model(monkeypatch, bad_answer):
    """答案非严格 "secondary"（含大小写/空白/布尔等）：一律不覆盖。"""
    _mock_secondary(monkeypatch, SECONDARY_MODEL)
    _mock_decide(monkeypatch, bad_answer)

    assert tmo.resolve_turn_model(MAIN_MODEL, user_text=USER_TEXT) is None


def test_decide_exception_keeps_main_model(monkeypatch):
    """decide 抛异常：吞掉异常返回 None，绝不外抛。"""
    _mock_secondary(monkeypatch, SECONDARY_MODEL)
    _mock_decide(monkeypatch, RuntimeError("决策服务炸了"))

    assert tmo.resolve_turn_model(MAIN_MODEL, user_text=USER_TEXT) is None


def test_get_secondary_model_exception_keeps_main_model(monkeypatch):
    """副模型配置读取抛异常：同样吞掉返回 None。"""
    def _boom():
        raise RuntimeError("配置损坏")

    monkeypatch.setattr(model_manager, "get_secondary_model", _boom)

    assert tmo.resolve_turn_model(MAIN_MODEL, user_text=USER_TEXT) is None


# ---------------------------------------------------------------------------
# 3. 不覆盖：副模型不可用 / 与主模型相同 / 缺关键字段 / 超窗
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "secondary",
    [None, {}, {"litellm_model": "", "id": "model-secondary"}],
    ids=["none", "empty", "no-litellm"],
)
def test_secondary_unusable_keeps_main_model(monkeypatch, secondary):
    """副模型为空 / 缺 litellm_model：不覆盖，也不发决策请求。"""
    _mock_secondary(monkeypatch, secondary)
    calls = _mock_decide(monkeypatch, "secondary")

    assert tmo.resolve_turn_model(MAIN_MODEL, user_text=USER_TEXT) is None
    assert calls == [], "副模型不可用时不该浪费一次决策"


def test_secondary_same_as_main_keeps_main_model(monkeypatch):
    """副模型与主模型是同一个：覆盖无意义，且不发决策请求。"""
    same = dict(SECONDARY_MODEL, litellm_model=MAIN_MODEL["litellm_model"])
    _mock_secondary(monkeypatch, same)
    calls = _mock_decide(monkeypatch, "secondary")

    assert tmo.resolve_turn_model(MAIN_MODEL, user_text=USER_TEXT) is None
    assert calls == []


@pytest.mark.parametrize("field", ["api_key", "base_url", "id", "context_window"])
def test_secondary_missing_critical_field_keeps_main_model(monkeypatch, field):
    """副模型缺关键字段（密钥 / 地址 / id / 窗口）：宁可不覆盖，也不发半套参数。"""
    broken = dict(SECONDARY_MODEL)
    broken[field] = "" if field != "context_window" else 0
    _mock_secondary(monkeypatch, broken)
    _mock_decide(monkeypatch, "secondary")

    assert tmo.resolve_turn_model(MAIN_MODEL, user_text=USER_TEXT) is None


def test_baseline_exceeds_secondary_window_keeps_main_model(monkeypatch):
    """上下文基线超过副模型窗口：硬护栏拦下（否则必然 400 爆窗）。"""
    _mock_secondary(monkeypatch, SECONDARY_MODEL)
    calls = _mock_decide(monkeypatch, "secondary")

    result = tmo.resolve_turn_model(
        MAIN_MODEL, baseline_tokens=200000, user_text=USER_TEXT
    )

    assert result is None
    assert calls == [], "护栏在决策之前就该拦下，不该再发决策请求"


# ---------------------------------------------------------------------------
# 4. 不覆盖：前置条件（回合来源 / 指定模型 / 无用户输入 / 无激活模型）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("source", ["lan", "automation", "local ", ""])
def test_non_local_turn_source_keeps_main_model(monkeypatch, source):
    """只有 local 回合才允许判定；LAN / 自动化 / 空来源一律不覆盖。"""
    _mock_secondary(monkeypatch, SECONDARY_MODEL)
    calls = _mock_decide(monkeypatch, "secondary")

    assert tmo.resolve_turn_model(MAIN_MODEL, turn_source=source, user_text=USER_TEXT) is None
    assert calls == []


@pytest.mark.parametrize("pending", ["model-explicit", " model-explicit "])
def test_pending_model_id_keeps_main_model(monkeypatch, pending):
    """入口显式指定了模型（星轨任务 / 秘书派发）：决策无权覆盖。"""
    _mock_secondary(monkeypatch, SECONDARY_MODEL)
    calls = _mock_decide(monkeypatch, "secondary")

    assert (
        tmo.resolve_turn_model(
            MAIN_MODEL, pending_model_id=pending, user_text=USER_TEXT
        )
        is None
    )
    assert calls == []


@pytest.mark.parametrize(
    "active",
    [None, {}, {"api_key": "k"}, {"litellm_model": ""}, "not-a-dict"],
    ids=["none", "empty", "no-litellm", "blank-litellm", "str"],
)
def test_active_model_unusable_keeps_main_model(monkeypatch, active):
    """没有激活模型 / 缺 litellm_model：不覆盖。"""
    _mock_secondary(monkeypatch, SECONDARY_MODEL)
    _mock_decide(monkeypatch, "secondary")

    assert tmo.resolve_turn_model(active, user_text=USER_TEXT) is None


@pytest.mark.parametrize("text", ["", "   ", None, [{"type": "image_url"}]])
def test_empty_user_text_keeps_main_model(monkeypatch, text):
    """没有可判断的用户文本（内部回合 / 纯图片）：不浪费一次决策。"""
    _mock_secondary(monkeypatch, SECONDARY_MODEL)
    calls = _mock_decide(monkeypatch, "secondary")

    assert tmo.resolve_turn_model(MAIN_MODEL, user_text=text) is None
    assert calls == []


def test_pending_empty_string_allows_override(monkeypatch):
    """pending_model_id 为空串=没指定模型：不构成拦截条件。"""
    _mock_secondary(monkeypatch, SECONDARY_MODEL)
    _mock_decide(monkeypatch, "secondary")

    assert (
        tmo.resolve_turn_model(MAIN_MODEL, pending_model_id="", user_text=USER_TEXT)
        is not None
    )


# ---------------------------------------------------------------------------
# 5. 隐私：state 只含用户输入，且截断到 500 字符
# ---------------------------------------------------------------------------

def test_state_is_user_text_only(monkeypatch):
    """state 只放本轮用户输入本身，不带任何前缀/历史/文件内容。"""
    _mock_secondary(monkeypatch, SECONDARY_MODEL)
    calls = _mock_decide(monkeypatch, None)

    tmo.resolve_turn_model(MAIN_MODEL, user_text=USER_TEXT)

    assert calls[0]["state"] == USER_TEXT


def test_state_truncated_to_500_chars(monkeypatch):
    """超长输入截断到 500 字符：只让决策看到开头就够判断复杂度。"""
    _mock_secondary(monkeypatch, SECONDARY_MODEL)
    calls = _mock_decide(monkeypatch, None)

    long_text = "把这段文档逐字解释一遍：" + "内容" * 1000
    tmo.resolve_turn_model(MAIN_MODEL, user_text=long_text)

    state = calls[0]["state"]
    assert len(state) <= 500
    assert state.startswith("把这段文档逐字解释一遍：")
    assert state == " ".join(long_text.split())[:500]


def test_state_has_no_file_content_marker(monkeypatch):
    """只传用户输入：文件挂载/工具结果类文本不会进入 state（即使调用方只给输入）。"""
    _mock_secondary(monkeypatch, SECONDARY_MODEL)
    calls = _mock_decide(monkeypatch, None)

    tmo.resolve_turn_model(MAIN_MODEL, user_text="看看 README 写的是什么")

    state = calls[0]["state"]
    for leaked in ("<file_content", "工具返回", "tool_result", "系统提示词"):
        assert leaked not in state


# ---------------------------------------------------------------------------
# 6. router_scene_live()：off / shadow / live 三态
# ---------------------------------------------------------------------------

def test_router_scene_live_false_when_off(data_dir, monkeypatch):
    """JEV 全关（默认配置）：live 为假 → 入口层跳过整套覆盖逻辑。"""
    _write_user_config(data_dir)

    assert tmo.router_scene_live() is False


def test_router_scene_live_false_when_scene_shadow(data_dir):
    """场景影子：仍不算 live（结果会被丢弃，入口层不必付出覆盖成本）。"""
    _write_user_config(data_dir)
    _enable_router(shadow=True)

    assert tmo.router_scene_live() is False


def test_router_scene_live_false_when_global_shadow(data_dir):
    """全局影子模式：即使场景 shadow=false 也不算 live。"""
    _write_user_config(data_dir)
    svc.save_decision_config(
        {
            "enabled": True,
            "shadow_mode": True,
            "scenes": {"router": {"enabled": True, "shadow": False}},
        }
    )

    assert tmo.router_scene_live() is False


def test_router_scene_live_true_when_live(data_dir):
    """live 三态齐备（全局 enabled+非影子，场景 enabled+非影子）：live 为真。"""
    _write_user_config(data_dir)
    _enable_router(shadow=False)

    assert tmo.router_scene_live() is True


def test_router_scene_live_zero_network_when_off(data_dir, monkeypatch):
    """live 判定本身零网络：JEV 关闭时任何决策请求都会 AssertionError。"""
    _write_user_config(data_dir)
    _mock_no_network(monkeypatch)

    assert tmo.router_scene_live() is False


# ---------------------------------------------------------------------------
# 7. 端到端（真实网关 + 打桩 HTTP）：live 模式采纳副模型并回填审计
# ---------------------------------------------------------------------------

def test_live_mode_end_to_end_picks_secondary(data_dir, monkeypatch):
    """live 模式走完「网关 → 服务 → 审计」全链路：返回副模型参数包，审计 adopted=true。"""
    _write_user_config(data_dir)
    _enable_router(shadow=False)
    _mock_secondary(monkeypatch, SECONDARY_MODEL)
    post_calls = _mock_post(
        monkeypatch, lambda i, call: _resp(200, {"answers": {"q": {"choice": "secondary"}}})
    )

    result = tmo.resolve_turn_model(MAIN_MODEL, user_text=USER_TEXT)

    assert result is not None
    assert result["litellm_model"] == "openai/gpt-5-mini"
    assert len(post_calls) == 1, "live 模式必须真发一次决策请求"
    records = _audit_records(data_dir)
    assert records, "live 决策必须留审计"
    record = records[-1]
    assert record["scene"] == "router"
    assert record["shadow"] is False
    assert record["adopted"] is True
    assert record["trace_id"]


def test_live_mode_end_to_end_main_keeps_main_model(data_dir, monkeypatch):
    """live 模式答 "main"：真发了请求、写了审计，但本回合不覆盖。

    注意语义区别：网关把「合法答案 "main"」本身记为 adopted（它采纳了这个结论），
    router 接入层看到 "main" 只是「保持主模型」，并不是失败路径。
    """
    _write_user_config(data_dir)
    _enable_router(shadow=False)
    _mock_secondary(monkeypatch, SECONDARY_MODEL)
    post_calls = _mock_post(
        monkeypatch, lambda i, call: _resp(200, {"answers": {"q": {"choice": "main"}}})
    )

    assert tmo.resolve_turn_model(MAIN_MODEL, user_text=USER_TEXT) is None
    assert len(post_calls) == 1
    record = _audit_records(data_dir)[-1]
    assert record["scene"] == "router"
    assert record["adopted"] is True, "网关采纳了合法答案 main（结论是「不用副模型」）"


def test_shadow_mode_keeps_main_model(data_dir, monkeypatch):
    """场景影子：真发请求、写审计，但结果丢弃 → 不覆盖。"""
    _write_user_config(data_dir)
    _enable_router(shadow=True)
    _mock_secondary(monkeypatch, SECONDARY_MODEL)
    post_calls = _mock_post(
        monkeypatch, lambda i, call: _resp(200, {"answers": {"q": {"choice": "secondary"}}})
    )

    assert tmo.resolve_turn_model(MAIN_MODEL, user_text=USER_TEXT) is None
    assert len(post_calls) == 1
    record = _audit_records(data_dir)[-1]
    assert record["shadow"] is True
    assert record["adopted"] is False


def test_jev_disabled_zero_request_and_no_override(data_dir, monkeypatch):
    """JEV 全关：零决策请求、零审计、不覆盖（=接入前行为逐字一致）。"""
    _write_user_config(data_dir)
    _mock_secondary(monkeypatch, SECONDARY_MODEL)
    post_calls = _mock_no_network(monkeypatch)

    assert tmo.resolve_turn_model(MAIN_MODEL, user_text=USER_TEXT) is None
    assert post_calls == []
    assert _audit_records(data_dir) == []


def test_image_turn_never_falls_back_to_text_only_secondary(data_dir, monkeypatch):
    """本轮带图且副模型不识图：禁止覆盖，且不浪费一次决策请求。

    回归背景：用户发截图时，JEV 因文字很短判定「简单任务」把回合切到纯文本副模型，
    视觉链路随即降级成「无法分析图片」占位提示——图片被静默丢弃。
    """
    _write_user_config(data_dir)
    _enable_router(shadow=False)
    _mock_secondary(monkeypatch, SECONDARY_MODEL)  # supports_vision=False
    post_calls = _mock_no_network(monkeypatch)

    result = tmo.resolve_turn_model(MAIN_MODEL, user_text=USER_TEXT, has_image=True)

    assert result is None, "带图回合不能被切到不识图的副模型"
    assert post_calls == [], "能力护栏必须在问决策模型之前就短路（零请求）"


def test_image_turn_allowed_when_secondary_can_see(data_dir, monkeypatch):
    """本轮带图但副模型确实支持视觉：能力护栏放行，正常走决策。"""
    from core import llm_dialects

    _write_user_config(data_dir)
    _enable_router(shadow=False)
    _mock_secondary(monkeypatch, {**SECONDARY_MODEL, "supports_vision": True})
    calls = _mock_decide(monkeypatch, "secondary")
    monkeypatch.setattr(llm_dialects, "_supports_vision_llm_first", lambda name: True)

    result = tmo.resolve_turn_model(MAIN_MODEL, user_text=USER_TEXT, has_image=True)

    assert result is not None
    assert result["litellm_model"] == SECONDARY_MODEL["litellm_model"]
    assert len(calls) == 1
    # 审计上下文带上 has_image，便于事后核对「当时这轮有没有图」
    assert calls[0]["kwargs"]["audit_extra"]["has_image"] is True

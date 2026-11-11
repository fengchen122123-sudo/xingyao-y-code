# -*- coding: utf-8 -*-
"""search 场景接入测试：JEV 决定 search_news 的结果条数（num_results）。

隔离策略：
- ``tests/conftest.py`` 收集阶段已把 ``YCODE_DATA_DIR`` 指向临时目录；本文件再
  把 ``config.YCODE_DIR`` monkeypatch 到每个用例的 tmp_path，绝不触碰真实
  ``~/.ycode``；
- DuckDuckGo 用 FakeDDGS 打桩，HTTP 层打桩 ``decision_service._post``——全程零真实
  网络、零真实决策请求。

覆盖点：决策返回 8 / 3 → 实际请求用该值；返回 None / 异常 → 用调用方传入值；
JEV 全关 → 与原行为完全一致（零决策请求 + 请求参数不变）。
"""
import json
import os
import sys

import httpx
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import decision_gateway as gateway
from core import decision_service as svc
from core import tools as tools_mod

PROVIDER = {
    "id": "tr-1",
    "name": "基元律动",
    "base_url": "https://tokenrhythm.studio/v1",
    "api_key": "sk_key_one",
}


# ---------------------------------------------------------------------------
# 测试基础设施
# ---------------------------------------------------------------------------

def _install_fake_ddgs(monkeypatch, results=None):
    """打桩 DDGS：记录每次 news() 请求的 max_results（= 真正发出去的条数）。"""
    requests = []

    class _FakeDDGS:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def news(self, query, max_results=None):
            requests.append({"query": query, "max_results": max_results})
            if results is not None:
                return list(results)
            return [{"title": "测试新闻", "url": "https://example.com/1", "body": "摘要"}]

    monkeypatch.setattr(tools_mod, "DDGS", lambda: _FakeDDGS())
    return requests


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


def _write_user_config(data_dir, providers):
    (data_dir / "config.json").write_text(
        json.dumps({"providers": providers}, ensure_ascii=False), encoding="utf-8"
    )


def _enable_scene(scene="search", *, shadow=False, **scene_extra):
    """写决策配置：全局 enabled + 指定场景 enabled。"""
    scene_cfg = {"enabled": True, "shadow": shadow}
    scene_cfg.update(scene_extra)
    return svc.save_decision_config(
        {
            "enabled": True,
            "shadow_mode": shadow,
            "scenes": {scene: scene_cfg},
        }
    )


def _resp(code, body=None):
    return httpx.Response(code, json=body if body is not None else {"answers": {}})


def _mock_post(monkeypatch, handler):
    """打桩 decision_service._post（网关链路真正发请求的唯一出口）。"""
    calls = []

    def fake(url, headers, payload, timeout_seconds):
        calls.append({"url": url, "headers": headers, "payload": payload, "timeout": timeout_seconds})
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
# 1. 决策值被采纳
# ---------------------------------------------------------------------------

def test_decision_eight_is_used_for_request(monkeypatch):
    """决策返回 8：本次 DDGS 请求就用 8 条（而不是调用方传入的 5）。"""
    calls = _mock_decide(monkeypatch, 8)
    requests = _install_fake_ddgs(monkeypatch)

    result = tools_mod.search_news("某公司最新财报与营收结构", num_results=5)

    assert [r["max_results"] for r in requests] == [8]
    assert not result.startswith("🚨")
    assert len(calls) == 1
    assert calls[0]["scene"] == "search"


def test_decision_three_is_used_for_request(monkeypatch):
    """决策返回 3：请求用 3 条。"""
    _mock_decide(monkeypatch, 3)
    requests = _install_fake_ddgs(monkeypatch)

    tools_mod.search_news("某个字怎么读", num_results=5)

    assert [r["max_results"] for r in requests] == [3]


def test_decision_overrides_large_caller_value(monkeypatch):
    """调用方给 12、决策给 8：以决策为准（决策是本次请求的最终档位）。"""
    _mock_decide(monkeypatch, 8)
    requests = _install_fake_ddgs(monkeypatch)

    tools_mod.search_news("多方交叉核对的选题", num_results=12)

    assert [r["max_results"] for r in requests] == [8]


# ---------------------------------------------------------------------------
# 2. 返回 None / 异常 → 保持调用方传入值
# ---------------------------------------------------------------------------

def test_decision_none_keeps_caller_num_results(monkeypatch):
    """决策返回 None（未启用/影子/失败/低置信）：用调用方传入的 num_results。"""
    _mock_decide(monkeypatch, None)
    requests = _install_fake_ddgs(monkeypatch)

    tools_mod.search_news("普通查询", num_results=7)

    assert [r["max_results"] for r in requests] == [7], "必须是调用方传入值，而不是硬编码 5"


def test_decision_none_keeps_default_num_results(monkeypatch):
    """调用方未传 num_results：None 时保持默认 5。"""
    _mock_decide(monkeypatch, None)
    requests = _install_fake_ddgs(monkeypatch)

    tools_mod.search_news("普通查询")

    assert [r["max_results"] for r in requests] == [5]


@pytest.mark.parametrize(
    "bad",
    ["8", 8.0, True, 0, 50, 100],
    ids=["str", "float", "bool", "zero", "fifty", "hundred"],
)
def test_illegal_decided_value_falls_back(monkeypatch, bad):
    """决策值非法（非 int / 布尔 / 越界）：丢弃并用调用方传入值。"""
    _mock_decide(monkeypatch, bad)
    requests = _install_fake_ddgs(monkeypatch)

    tools_mod.search_news("普通查询", num_results=6)

    assert [r["max_results"] for r in requests] == [6]


def test_decision_exception_still_returns_results(monkeypatch):
    """决策抛异常：search_news 照常返回正常结果，且用调用方传入值。"""
    _mock_decide(monkeypatch, RuntimeError("决策服务炸了"))
    requests = _install_fake_ddgs(monkeypatch)

    result = tools_mod.search_news("普通查询", num_results=9)

    assert [r["max_results"] for r in requests] == [9]
    assert "测试新闻" in result
    assert not result.startswith("🚨")


# ---------------------------------------------------------------------------
# 3. 隐私约束：state 只含查询词
# ---------------------------------------------------------------------------

def test_state_contains_only_query_and_question_shape(monkeypatch):
    """state 只放查询词本身；题目形状/候选/超时符合规范。"""
    calls = _mock_decide(monkeypatch, None)
    _install_fake_ddgs(monkeypatch)

    tools_mod.search_news("量子计算最新进展", num_results=5)

    call = calls[0]
    assert call["state"] == "用户检索词：量子计算最新进展"
    assert call["kwargs"]["options"] == (3, 5, 8)
    assert call["kwargs"]["timeout_ms"] == 250
    question = call["question"]
    assert question["type"] == "choice"
    assert "判断这次联网检索的信息量需求" in question["instructions"]


# ---------------------------------------------------------------------------
# 4. JEV 全关 / 影子：与原行为完全一致
# ---------------------------------------------------------------------------

def test_jev_disabled_behaves_like_before(data_dir, monkeypatch):
    """JEV 全关：0 次决策请求，请求参数与接入前一致，返回文本一致。"""
    # 不写决策配置 = DEFAULT_CONFIG（enabled=false，scenes 全 shadow 且未启用）
    _write_user_config(data_dir, [PROVIDER])
    post_calls = _mock_no_network(monkeypatch)  # 任何决策请求都会 AssertionError
    requests = _install_fake_ddgs(monkeypatch)

    result = tools_mod.search_news("OpenAI 发布会", num_results=5)

    assert post_calls == [], "JEV 关闭时不允许有任何决策请求"
    assert _audit_records(data_dir) == [], "JEV 关闭时不允许有审计落盘"
    assert len(requests) == 1, "DDGS 调用次数必须与接入前一致（恰好 1 次）"
    assert requests[0] == {"query": "OpenAI 发布会", "max_results": 5}
    assert result == (
        "1. 测试新闻\n   摘要: 摘要\n   链接: https://example.com/1"
    ), "返回文本必须与接入前逐字一致"


def test_jev_disabled_keeps_clamp_behaviour(data_dir, monkeypatch):
    """JEV 全关 + 超大 num_results：仍按原有 1~20 钳制（回归：不改动原逻辑）。"""
    _write_user_config(data_dir, [PROVIDER])
    _mock_no_network(monkeypatch)
    requests = _install_fake_ddgs(monkeypatch)

    tools_mod.search_news("query", num_results=100000)

    assert [r["max_results"] for r in requests] == [20]


def test_jev_disabled_no_results_message_unchanged(data_dir, monkeypatch):
    """JEV 全关 + 无结果：仍返回 🚨 前缀（子代理失败流程依赖它）。"""
    _write_user_config(data_dir, [PROVIDER])
    _mock_no_network(monkeypatch)
    _install_fake_ddgs(monkeypatch, results=[])

    result = tools_mod.search_news("不存在的选题", num_results=5)

    assert result.startswith("🚨")
    assert "不存在的选题" in result


def test_scene_shadow_keeps_caller_value_but_sends_request(data_dir, monkeypatch):
    """search 影子模式：真实发决策请求并写审计，但结果丢弃 → 用调用方传入值。"""
    _write_user_config(data_dir, [PROVIDER])
    _enable_scene("search", shadow=True)
    post_calls = _mock_post(
        monkeypatch, lambda i, call: _resp(200, {"answers": {"q": {"choice": "8"}}})
    )
    requests = _install_fake_ddgs(monkeypatch)

    tools_mod.search_news("影子模式查询", num_results=5)

    assert len(post_calls) == 1, "影子模式仍应发一次请求（只观测不采纳）"
    assert [r["max_results"] for r in requests] == [5]
    record = _audit_records(data_dir)[-1]
    assert record["scene"] == "search"
    assert record["shadow"] is True
    assert record["adopted"] is False


# ---------------------------------------------------------------------------
# 5. 端到端（真实网关链路 + 打桩 HTTP）：live 模式采纳决策档位并回填审计
# ---------------------------------------------------------------------------

def test_live_mode_end_to_end_uses_decided_value(data_dir, monkeypatch):
    """live 模式走完「网关 → 服务 → 审计」全链路：请求用 8 条，审计 adopted=true。"""
    _write_user_config(data_dir, [PROVIDER])
    _enable_scene("search", shadow=False)
    post_calls = _mock_post(
        monkeypatch, lambda i, call: _resp(200, {"answers": {"q": {"choice": "8"}}})
    )
    requests = _install_fake_ddgs(monkeypatch)

    result = tools_mod.search_news("需要多来源交叉核对的事件", num_results=5)

    assert len(post_calls) == 1
    assert [r["max_results"] for r in requests] == [8]
    assert not result.startswith("🚨")
    record = _audit_records(data_dir)[-1]
    assert record["scene"] == "search"
    assert record["shadow"] is False
    assert record["adopted"] is True
    assert record["trace_id"]

# -*- coding: utf-8 -*-
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


@pytest.fixture(autouse=True)
def _disable_free_web_search(monkeypatch):
    from core import web_search

    def _unavailable(*args, **kwargs):
        raise web_search.SearchBackendError("测试中禁用免费网页通道")

    monkeypatch.setattr(web_search, "search_web", _unavailable)

def _install_fake_ddgs(monkeypatch, results=None):
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


def _enable_scene(scene="search", **scene_extra):
    scene_cfg = {"enabled": True}
    scene_cfg.update(scene_extra)
    return svc.save_decision_config(
        {
            "enabled": True,
            "scenes": {scene: scene_cfg},
        }
    )


def _resp(code, body=None):
    return httpx.Response(code, json=body if body is not None else {"answers": {}})


def _mock_post(monkeypatch, handler):
    calls = []

    def fake(url, headers, payload, timeout_seconds):
        calls.append({"url": url, "headers": headers, "payload": payload, "timeout": timeout_seconds})
        return handler(len(calls) - 1, calls[-1])

    monkeypatch.setattr(svc, "_post", fake)
    return calls


def _mock_no_network(monkeypatch):
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
    import config

    directory = tmp_path / "ycode"
    directory.mkdir()
    monkeypatch.setattr(config, "YCODE_DIR", str(directory), raising=False)
    svc._reset_runtime_state()
    yield directory
    svc._reset_runtime_state()


def test_decision_eight_is_used_for_request(monkeypatch):
    calls = _mock_decide(monkeypatch, 8)
    requests = _install_fake_ddgs(monkeypatch)

    result = tools_mod.search_news("某公司最新财报与营收结构", num_results=5)

    assert [r["max_results"] for r in requests] == [8]
    assert not result.startswith("🚨")
    assert len(calls) == 1
    assert calls[0]["scene"] == "search"


def test_decision_three_is_used_for_request(monkeypatch):
    _mock_decide(monkeypatch, 3)
    requests = _install_fake_ddgs(monkeypatch)

    tools_mod.search_news("某个字怎么读", num_results=5)

    assert [r["max_results"] for r in requests] == [3]


def test_decision_overrides_large_caller_value(monkeypatch):
    _mock_decide(monkeypatch, 8)
    requests = _install_fake_ddgs(monkeypatch)

    tools_mod.search_news("多方交叉核对的选题", num_results=12)

    assert [r["max_results"] for r in requests] == [8]


def test_decision_none_keeps_caller_num_results(monkeypatch):
    _mock_decide(monkeypatch, None)
    requests = _install_fake_ddgs(monkeypatch)

    tools_mod.search_news("普通查询", num_results=7)

    assert [r["max_results"] for r in requests] == [7], "必须是调用方传入值，而不是硬编码 5"


def test_decision_none_keeps_default_num_results(monkeypatch):
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
    _mock_decide(monkeypatch, bad)
    requests = _install_fake_ddgs(monkeypatch)

    tools_mod.search_news("普通查询", num_results=6)

    assert [r["max_results"] for r in requests] == [6]


def test_decision_exception_still_returns_results(monkeypatch):
    _mock_decide(monkeypatch, RuntimeError("决策服务炸了"))
    requests = _install_fake_ddgs(monkeypatch)

    result = tools_mod.search_news("普通查询", num_results=9)

    assert [r["max_results"] for r in requests] == [9]
    assert "测试新闻" in result
    assert not result.startswith("🚨")


def test_state_contains_only_query_and_question_shape(monkeypatch):
    calls = _mock_decide(monkeypatch, None)
    _install_fake_ddgs(monkeypatch)

    tools_mod.search_news("量子计算最新进展", num_results=5)

    call = calls[0]
    assert call["state"] == "用户检索词：量子计算最新进展"
    assert call["kwargs"]["options"] == (3, 5, 8)
    assert call["kwargs"]["timeout_ms"] == 1500
    question = call["question"]
    assert question["type"] == "choice"
    assert "判断这次联网检索的信息量需求" in question["instructions"]


def test_jev_disabled_behaves_like_before(data_dir, monkeypatch):
    _write_user_config(data_dir, [PROVIDER])
    post_calls = _mock_no_network(monkeypatch)
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
    _write_user_config(data_dir, [PROVIDER])
    _mock_no_network(monkeypatch)
    requests = _install_fake_ddgs(monkeypatch)

    tools_mod.search_news("query", num_results=100000)

    assert [r["max_results"] for r in requests] == [20]


def test_jev_disabled_no_results_message_unchanged(data_dir, monkeypatch):
    _write_user_config(data_dir, [PROVIDER])
    _mock_no_network(monkeypatch)
    _install_fake_ddgs(monkeypatch, results=[])

    result = tools_mod.search_news("不存在的选题", num_results=5)

    assert result.startswith("🚨")
    assert "不存在的选题" in result


def test_live_mode_end_to_end_uses_decided_value(data_dir, monkeypatch):
    _write_user_config(data_dir, [PROVIDER])
    _enable_scene("search")
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
    assert "shadow" not in record
    assert record["adopted"] is True
    assert record["trace_id"]


def test_live_mode_low_confidence_probability_falls_back_to_default(data_dir, monkeypatch):
    _write_user_config(data_dir, [PROVIDER])
    _enable_scene("search")
    _mock_post(
        monkeypatch,
        lambda i, call: _resp(
            200,
            {"answers": {"q": {"choice": "8", "probabilities": {"3": 0.2, "5": 0.35, "8": 0.45}}}},
        ),
    )
    requests = _install_fake_ddgs(monkeypatch)

    result = tools_mod.search_news("需要核对的话题", num_results=5)

    assert [r["max_results"] for r in requests] == [5]
    assert not result.startswith("🚨")
    assert _audit_records(data_dir)[-1]["adopted"] is False

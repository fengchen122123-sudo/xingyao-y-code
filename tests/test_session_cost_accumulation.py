"""会话级累计成本记账（P2）：micros 整数累加 / 未计价单列 / 跨币种不求和 / 状态栏片段。

全部用例直接操作 core.llm_diagnostics 的模块级 session_token_usage，并
monkeypatch compute_cost_fields 与落库函数，不启动任何真实 LLM 请求。
"""

import pytest

# 合法 usage：prompt + completion == total，且缓存命中与未命中加起来等于 prompt
USAGE = {
    "prompt_tokens": 1000,
    "completion_tokens": 100,
    "total_tokens": 1100,
}


def _reset_cost_state(llm):
    with llm.token_usage_lock:
        llm.session_token_usage["cumulative_cost_micros"] = 0
        llm.session_token_usage["cumulative_cost_currency"] = ""
        llm.session_token_usage["cumulative_cost_unpriced"] = 0


def _account(llm, monkeypatch, cost_fields, session_id="cost-accum"):
    """跑一次真实记账路径，但用给定的成本字段替身代替价格表查询。"""
    import core.llm_diagnostics as diag

    monkeypatch.setattr(
        diag, "compute_cost_fields", lambda *a, **k: dict(cost_fields or {})
    )
    monkeypatch.setattr(llm, "_persist_token_usage", lambda *a, **k: None)
    record = llm._account_server_usage(
        dict(USAGE),
        model_name="openai/test-cost",
        api_base="https://cost.example/v1",
        session_id=session_id,
        cache_scope="main",
        invalidation_reason="",
    )
    assert record["valid"] is True
    return record


def _cost_state(llm):
    with llm.token_usage_lock:
        return {
            "micros": llm.session_token_usage["cumulative_cost_micros"],
            "currency": llm.session_token_usage["cumulative_cost_currency"],
            "unpriced": llm.session_token_usage["cumulative_cost_unpriced"],
        }


def test_priced_turns_accumulate_micros(monkeypatch):
    """有价记账两次：micros 精确累加（整数微元），unpriced 保持 0。"""
    import core.llm as llm

    _reset_cost_state(llm)
    _account(llm, monkeypatch, {"cost_micros": 1234, "cost_currency": "CNY"})
    _account(llm, monkeypatch, {"cost_micros": 766, "cost_currency": "CNY"})

    state = _cost_state(llm)
    assert state["micros"] == 2000
    assert state["currency"] == "CNY"
    assert state["unpriced"] == 0


def test_unpriced_turn_keeps_micros_and_counts_separately(monkeypatch):
    """无价记账：micros 与 currency 不变，unpriced 递增（未报告 ≠ 0 元）。"""
    import core.llm as llm

    _reset_cost_state(llm)
    _account(llm, monkeypatch, {"cost_micros": 500, "cost_currency": "CNY"})
    _account(llm, monkeypatch, {})
    _account(llm, monkeypatch, {})

    state = _cost_state(llm)
    assert state["micros"] == 500
    assert state["currency"] == "CNY"
    assert state["unpriced"] == 2


def test_unpriced_only_session_has_no_currency(monkeypatch):
    """整场都没有配价：micros=0 且 currency 为空串，unpriced 记轮次。"""
    import core.llm as llm

    _reset_cost_state(llm)
    _account(llm, monkeypatch, {})

    state = _cost_state(llm)
    assert state["micros"] == 0
    assert state["currency"] == ""
    assert state["unpriced"] == 1


def test_currency_switch_resets_micros_before_accumulating(monkeypatch):
    """跨币种不求和：换币种即先清零，再按新币种累加。"""
    import core.llm as llm

    _reset_cost_state(llm)
    _account(llm, monkeypatch, {"cost_micros": 5000, "cost_currency": "CNY"})
    assert _cost_state(llm)["micros"] == 5000

    _account(llm, monkeypatch, {"cost_micros": 3000, "cost_currency": "USD"})
    state = _cost_state(llm)
    assert state["micros"] == 3000, "切到 USD 时必须丢掉 CNY 的 5000，绝不混算"
    assert state["currency"] == "USD"

    # 同币种继续累加不受影响
    _account(llm, monkeypatch, {"cost_micros": 1000, "cost_currency": "USD"})
    assert _cost_state(llm)["micros"] == 4000


def _status_text():
    import main

    return main._compose_status_token_info(
        prompt="40.0M", completion="2.1M", cached="700.4K", global_rate="71%",
        ctx_pct="3%", ctx_used="4K", ctx_max="128K",
    )


BASE_STATUS = "↑ 40.0M · ↓ 2.1M · ⚡ 700.4K (71%) · 上下文 3% (4K/128K)"


def test_status_appends_priced_cost_fragment(monkeypatch):
    """有价：末尾追加币种符号 + 4 位小数金额，原文案顺序不变。"""
    import core.llm as llm
    from core.cost_model import CURRENCY_SYMBOLS

    _reset_cost_state(llm)
    with llm.token_usage_lock:
        llm.session_token_usage["cumulative_cost_micros"] = 12345
        llm.session_token_usage["cumulative_cost_currency"] = "CNY"

    text = _status_text()
    symbol = CURRENCY_SYMBOLS["CNY"]
    assert text.startswith(BASE_STATUS), text
    assert text == f"{BASE_STATUS} · {symbol}0.0123", text
    assert "未计价" not in text


def test_status_reports_unpriced_without_fake_zero():
    """无价：只显示「未计价」，绝不显示 ¥0.0000。"""
    import core.llm as llm

    _reset_cost_state(llm)
    with llm.token_usage_lock:
        llm.session_token_usage["cumulative_cost_unpriced"] = 3

    text = _status_text()
    assert text == f"{BASE_STATUS} · 未计价", text
    assert "0.0000" not in text


def test_status_unchanged_when_no_cost_evidence():
    """既无已计价也无未计价轮次：状态栏文案与历史完全一致，不追加任何内容。"""
    import core.llm as llm

    _reset_cost_state(llm)

    assert _status_text() == BASE_STATUS
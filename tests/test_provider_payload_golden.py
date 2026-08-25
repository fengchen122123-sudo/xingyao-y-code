from copy import deepcopy
from types import SimpleNamespace
import pytest

from tests.request_payload_snapshot import ALPHA_TOOL, ZETA_TOOL, snapshot_provider_kwargs


def assert_exact_provider_kwargs(captured, expected):
    assert len(captured) == 1
    assert snapshot_provider_kwargs(captured[0]) == snapshot_provider_kwargs(expected)


def _chunk():
    delta = SimpleNamespace(
        content="answer",
        reasoning_content=None,
        tool_calls=None,
        provider_specific_fields=None,
    )
    return SimpleNamespace(
        choices=[SimpleNamespace(delta=delta, finish_reason="stop")],
        usage=None,
    )


def _capture(
    monkeypatch, *, model, api_base, protocol, model_cfg, messages,
    route_model_cfg=None, **kwargs
):
    import core.llm as llm

    captured = []
    monkeypatch.setattr(llm, "estimate_tokens", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(llm, "get_tools_schema", lambda: [ZETA_TOOL, ALPHA_TOOL])
    monkeypatch.setattr(
        "core.model_manager.get_model_protocol",
        lambda *_args, **_kwargs: protocol,
    )
    monkeypatch.setattr(
        "core.model_manager.get_model_config_by_litellm_model",
        lambda *_args, **_kwargs: model_cfg if route_model_cfg is None else route_model_cfg,
    )
    monkeypatch.setattr(
        "core.model_manager.get_model_config_by_id",
        lambda *_args, **_kwargs: model_cfg,
        raising=False,
    )
    monkeypatch.setattr(
        "core.memory_kairos.build_memory_system_section",
        lambda: "",
    )
    monkeypatch.setattr(llm, "_describe_images_with_secondary", lambda *_args, **_kwargs: None)

    def completion(**provider_kwargs):
        captured.append(deepcopy(provider_kwargs))
        return iter([_chunk()])

    if protocol == "responses":
        monkeypatch.setattr(llm, "_litellm_responses", completion)
    else:
        monkeypatch.setattr(llm, "completion", completion)
    list(llm.chat_with_model_events(model, "golden-secret", messages, api_base, persist=False,
                                    consume_background_notifications=False, **kwargs))
    return captured


def test_deepseek_non_thinking_provider_kwargs_are_byte_golden(monkeypatch):
    import core.llm as llm

    captured = _capture(
        monkeypatch, model="openai/deepseek-v4-pro",
        api_base="https://api.deepseek.com", protocol="openai",
        model_cfg={"enable_thinking": False},
        messages=[{"role": "system", "content": "stable"}, {"role": "user", "content": "inspect"}],
    )
    expected = {
        "model": "openai/deepseek-v4-pro",
        "api_key": "<redacted>",
        "messages": [
            {"role": "system", "content": "stable"},
            {"role": "user", "content": "inspect"},
        ],
        "tools": [ALPHA_TOOL, ZETA_TOOL],
        "stream": True,
        "stream_options": {"include_usage": True},
        "num_retries": 2,
        "extra_body": {"thinking": {"type": "disabled"}},
        "tool_choice": "auto",
        "max_tokens": 8192,
        "api_base": "https://api.deepseek.com",
        "timeout": llm.API_REQUEST_TIMEOUT_SECONDS,
    }
    assert_exact_provider_kwargs(captured, expected)


def test_deepseek_thinking_tool_round_provider_kwargs_are_byte_golden(monkeypatch):
    import core.llm as llm

    messages = [
        {"role": "system", "content": "stable"},
        {"role": "user", "content": "inspect"},
        {
            "role": "assistant", "content": None, "reasoning_content": "reason",
            "tool_calls": [{
                "id": "call-1", "type": "function",
                "function": {"name": "alpha", "arguments": "{}"},
            }],
        },
        {"role": "tool", "tool_call_id": "call-1", "name": "alpha", "content": "result"},
    ]
    captured = _capture(
        monkeypatch, model="openai/deepseek-v4-pro",
        api_base="https://api.deepseek.com", protocol="openai",
        model_cfg={"enable_thinking": True, "reasoning_effort": "high"},
        messages=messages,
    )
    expected = {
        "model": "openai/deepseek-v4-pro",
        "api_key": "<redacted>",
        "messages": [
            {"role": "system", "content": "stable"},
            {"role": "user", "content": "inspect"},
            {
                "role": "assistant", "content": "",
                "tool_calls": [{
                    "id": "call-1", "type": "function",
                    "function": {"name": "alpha", "arguments": "{}"},
                }],
                "reasoning_content": "reason",
            },
            {"role": "tool", "content": "result", "tool_call_id": "call-1", "name": "alpha"},
        ],
        "tools": [ALPHA_TOOL, ZETA_TOOL],
        "stream": True,
        "stream_options": {"include_usage": True},
        "num_retries": 2,
        "extra_body": {"thinking": {"type": "enabled"}},
        "reasoning_effort": "high",
        "allowed_openai_params": ["reasoning_effort"],
        "max_tokens": 8192,
        "api_base": "https://api.deepseek.com",
        "timeout": llm.API_REQUEST_TIMEOUT_SECONDS,
    }
    assert_exact_provider_kwargs(captured, expected)


def test_kimi_openai_provider_kwargs_keep_tool_order_and_cache_lane(monkeypatch):
    import core.llm as llm

    captured = _capture(
        monkeypatch, model="openai/kimi-k2.5", api_base="https://api.kimi.com/coding/v1",
        protocol="openai", model_cfg={}, cache_session_id="golden-kimi-openai",
        messages=[{"role": "system", "content": "stable"}, {"role": "user", "content": "inspect"}],
    )
    expected = {
        "model": "openai/kimi-k2.5",
        "api_key": "<redacted>",
        "messages": [
            {"role": "system", "content": "stable"},
            {"role": "user", "content": "inspect"},
        ],
        "tools": [ZETA_TOOL, ALPHA_TOOL],
        "stream": True,
        "stream_options": {"include_usage": True},
        "num_retries": 2,
        "max_tokens": 16384,
        "extra_body": {"prompt_cache_key": "ycode-75954f321a7f-38d73f3720291ffd"},
        "tool_choice": "auto",
        "api_base": "https://api.kimi.com/coding/v1",
        "timeout": llm.API_REQUEST_TIMEOUT_SECONDS,
    }
    assert_exact_provider_kwargs(captured, expected)


def test_kimi_anthropic_provider_kwargs_keep_thinking_and_metadata(monkeypatch):
    import core.llm as llm

    captured = _capture(
        monkeypatch, model="anthropic/kimi-k3", api_base="https://api.kimi.com/coding",
        protocol="anthropic", model_cfg={}, cache_session_id="golden-kimi-anthropic",
        messages=[{"role": "system", "content": "stable"}, {"role": "user", "content": "inspect"}],
    )
    expected = {
        "model": "anthropic/kimi-k3",
        "api_key": "<redacted>",
        "messages": [
            {"role": "system", "content": "stable", "cache_control": {"type": "ephemeral"}},
            {"role": "user", "content": "inspect"},
        ],
        "tools": [ZETA_TOOL, ALPHA_TOOL],
        "stream": True,
        "stream_options": {"include_usage": True},
        "num_retries": 2,
        "thinking": {"type": "enabled", "budget_tokens": 4096},
        "max_tokens": 32000,
        "allowed_openai_params": ["thinking"],
        "metadata": {"user_id": "ycode-c49d43ff17a8-642511194a2b200e"},
        "tool_choice": "auto",
        "api_base": "https://api.kimi.com/coding",
        "timeout": llm.API_REQUEST_TIMEOUT_SECONDS,
    }
    assert_exact_provider_kwargs(captured, expected)


def test_responses_provider_kwargs_are_byte_golden(monkeypatch):
    import core.llm as llm

    captured = _capture(
        monkeypatch, model="openai/gpt-5", api_base="https://api.openai.com/v1",
        protocol="responses", model_cfg={},
        messages=[{"role": "system", "content": "stable"}, {"role": "user", "content": "inspect"}],
    )
    expected = {
        "model": "openai/gpt-5",
        "api_key": "<redacted>",
        "input": [
            {"role": "system", "content": "stable"},
            {"role": "user", "content": "inspect"},
        ],
        "tools": [
            {"type": "function", "name": "zeta", "description": "zeta", "parameters": {"type": "object", "properties": {}}},
            {"type": "function", "name": "alpha", "description": "alpha", "parameters": {"type": "object", "properties": {}}},
        ],
        "stream": True,
        "timeout": llm.API_REQUEST_TIMEOUT_SECONDS,
        "api_base": "https://api.openai.com/v1",
        "max_output_tokens": 8192,
        "tool_choice": "auto",
        "num_retries": 2,
    }
    assert_exact_provider_kwargs(captured, expected)


@pytest.mark.parametrize(
    ("model", "model_cfg", "provider_tail", "tools"),
    [
        (
            "openai/deepseek-v4-flash",
            {"reasoning_choice": "high", "enable_thinking": True, "reasoning_effort": "high"},
            {
                "extra_body": {"thinking": {"type": "enabled"}},
                "reasoning_effort": "high",
                "allowed_openai_params": ["reasoning_effort"],
                "max_tokens": 8192,
            },
            [ZETA_TOOL, ALPHA_TOOL],
        ),
        (
            "openai/qwen3.7-max",
            {"reasoning_choice": "off", "enable_thinking": False},
            {
                "extra_body": {"enable_thinking": False},
                "tool_choice": "auto",
                "max_tokens": 8192,
            },
            [ZETA_TOOL, ALPHA_TOOL],
        ),
        (
            "openai/qwen3.8-max",
            {
                "reasoning_choice": "medium",
                "enable_thinking": True,
                "reasoning_effort": "medium",
            },
            {
                "extra_body": {
                    "enable_thinking": True,
                    "reasoning_effort": "medium",
                },
                "tool_choice": "auto",
                "max_tokens": 8192,
            },
            [ZETA_TOOL, ALPHA_TOOL],
        ),
        (
            "openai/LongCat-2.0",
            {"reasoning_choice": "on", "enable_thinking": True},
            {
                "extra_body": {"thinking": {"type": "enabled"}},
                "tool_choice": "auto",
                "max_tokens": 8192,
            },
            [ZETA_TOOL, ALPHA_TOOL],
        ),
        (
            "openai/kimi-k3",
            {"reasoning_choice": "high", "enable_thinking": True, "reasoning_effort": "high"},
            {
                "max_tokens": 32000,
                "extra_body": {"reasoning_effort": "high"},
                "tool_choice": "auto",
            },
            [ZETA_TOOL, ALPHA_TOOL],
        ),
    ],
)
def test_model_reasoning_choice_provider_kwargs_are_byte_golden(
    monkeypatch, model, model_cfg, provider_tail, tools
):
    """新思考策略的最终 provider 字节必须精确，不允许混入第二套开关。"""
    import core.llm as llm

    messages = [
        {"role": "system", "content": "stable"},
        {"role": "user", "content": "inspect"},
    ]
    expected_messages = deepcopy(messages)
    captured = _capture(
        monkeypatch,
        model=model,
        api_base="https://gateway.example/v1",
        protocol="openai",
        model_cfg=model_cfg,
        messages=messages,
    )
    expected = {
        "model": model,
        "api_key": "<redacted>",
        "messages": expected_messages,
        "tools": tools,
        "stream": True,
        "stream_options": {"include_usage": True},
        "num_retries": 2,
        **provider_tail,
        "api_base": "https://gateway.example/v1",
        "timeout": llm.API_REQUEST_TIMEOUT_SECONDS,
    }

    assert_exact_provider_kwargs(captured, expected)


def test_exact_model_id_reaches_final_provider_payload(monkeypatch):
    """重复 route 时，真实事件入口必须使用当前模型 ID 对应的选择。"""
    captured = _capture(
        monkeypatch,
        model="openai/deepseek-v4-flash",
        api_base="https://gateway.example/v1",
        protocol="openai",
        model_cfg={"reasoning_choice": "off", "enable_thinking": False},
        route_model_cfg={
            "reasoning_choice": "high",
            "enable_thinking": True,
            "reasoning_effort": "high",
        },
        messages=[{"role": "user", "content": "inspect"}],
        model_id="model-second",
    )

    assert captured[0]["extra_body"] == {"thinking": {"type": "disabled"}}
    assert "reasoning_effort" not in captured[0]

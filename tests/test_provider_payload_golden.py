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
        monkeypatch.setattr(llm, "_dispatch_responses", completion)
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
        "max_tokens": 384000,
        "extra_body": {"thinking": {"type": "disabled"}},
        "tool_choice": "auto",
        "api_base": "https://api.deepseek.com",
        "timeout": llm.API_REQUEST_TIMEOUT_SECONDS,
    }
    assert_exact_provider_kwargs(captured, expected)


def test_deepseek_on_maas_aggregate_domain_provider_kwargs_are_byte_golden(monkeypatch):
    """deepseek 名 × maas 聚合域：聚合域不得改写请求字节（Task 1a 的回归防线）。

    该 (model, api_base) 组合此前落在 Qwen 域名兜底里，会被追加
    x-dashscope-session-cache 头并把 content 改写成 cache_control 块；
    名称优先后应与官方 api.deepseek.com 的 deepseek 金样只差 api_base。
    """
    import core.llm as llm

    captured = _capture(
        monkeypatch, model="openai/deepseek-v4.1-flash",
        api_base="https://workspace-123.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
        protocol="openai", model_cfg={"enable_thinking": False},
        messages=[{"role": "system", "content": "stable"}, {"role": "user", "content": "inspect"}],
    )
    expected = {
        "model": "openai/deepseek-v4.1-flash",
        "api_key": "<redacted>",
        "messages": [
            {"role": "system", "content": "stable"},
            {"role": "user", "content": "inspect"},
        ],
        "tools": [ALPHA_TOOL, ZETA_TOOL],
        "stream": True,
        "stream_options": {"include_usage": True},
        "num_retries": 2,
        "max_tokens": 384000,
        "extra_body": {"thinking": {"type": "disabled"}},
        "tool_choice": "auto",
        "api_base": "https://workspace-123.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
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
        "max_tokens": 384000,
        "extra_body": {"thinking": {"type": "enabled"}},
        "reasoning_effort": "high",
        "allowed_openai_params": ["reasoning_effort"],
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
        "max_tokens": 64000,
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
        "max_tokens": 131072,
        "thinking": {"type": "enabled", "budget_tokens": 4096},
        "allowed_openai_params": ["thinking"],
        "metadata": {"user_id": "ycode-c49d43ff17a8-642511194a2b200e"},
        "tool_choice": "auto",
        "api_base": "https://api.kimi.com/coding",
        "timeout": llm.API_REQUEST_TIMEOUT_SECONDS,
    }
    assert_exact_provider_kwargs(captured, expected)


def test_kimi_anthropic_max_reasoning_provider_kwargs_are_byte_golden(monkeypatch):
    import core.llm as llm

    captured = _capture(
        monkeypatch,
        model="anthropic/k3",
        api_base="https://api.kimi.com/coding",
        protocol="anthropic",
        model_cfg={"reasoning_choice": "max"},
        cache_session_id="golden-kimi-anthropic",
        messages=[
            {"role": "system", "content": "stable"},
            {"role": "user", "content": "inspect"},
        ],
    )
    expected = {
        "model": "anthropic/k3",
        "api_key": "<redacted>",
        "messages": [
            {
                "role": "system",
                "content": "stable",
                "cache_control": {"type": "ephemeral"},
            },
            {"role": "user", "content": "inspect"},
        ],
        "tools": [ZETA_TOOL, ALPHA_TOOL],
        "stream": True,
        "stream_options": {"include_usage": True},
        "num_retries": 2,
        "max_tokens": 131072,
        "thinking": {"type": "enabled", "budget_tokens": 16384},
        "allowed_openai_params": ["thinking"],
        "metadata": {"user_id": "ycode-c49d43ff17a8-1f26e809b954b97b"},
        "tool_choice": "auto",
        "api_base": "https://api.kimi.com/coding",
        "timeout": llm.API_REQUEST_TIMEOUT_SECONDS,
    }
    assert_exact_provider_kwargs(captured, expected)


def test_stepfun_anthropic_provider_kwargs_map_effort_to_output_config(monkeypatch):
    """阶跃 Anthropic 路由：思考档位必须编码为官方 output_config.effort。"""
    import core.llm as llm

    captured = _capture(
        monkeypatch,
        model="anthropic/step-3.7-flash",
        api_base="https://api.stepfun.com/step_plan",
        protocol="anthropic",
        model_cfg={"reasoning_choice": "high", "enable_thinking": True, "reasoning_effort": "high"},
        cache_session_id="golden-stepfun-anthropic",
        messages=[{"role": "system", "content": "stable"}, {"role": "user", "content": "inspect"}],
    )
    expected = {
        "model": "anthropic/step-3.7-flash",
        "api_key": "<redacted>",
        "messages": [
            {"role": "system", "content": "stable", "cache_control": {"type": "ephemeral"}},
            {"role": "user", "content": "inspect"},
        ],
        "tools": [ZETA_TOOL, ALPHA_TOOL],
        "stream": True,
        "stream_options": {"include_usage": True},
        "num_retries": 2,
        "max_tokens": 32000,
        "output_config": {"effort": "high"},
        "allowed_openai_params": ["output_config"],
        "tool_choice": "auto",
        "api_base": "https://api.stepfun.com/step_plan",
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
        "max_output_tokens": 32000,
        "tool_choice": "auto",
        "num_retries": 2,
    }
    assert_exact_provider_kwargs(captured, expected)


@pytest.mark.parametrize(
    ("model", "model_cfg", "provider_tail", "tools", "expected_messages"),
    [
        (
            "openai/deepseek-v4-flash",
            {"reasoning_choice": "high", "enable_thinking": True, "reasoning_effort": "high"},
            {
                "max_tokens": 384000,
                "extra_body": {"thinking": {"type": "enabled"}},
                "reasoning_effort": "high",
                "allowed_openai_params": ["reasoning_effort"],
            },
            [ALPHA_TOOL, ZETA_TOOL],
            None,
        ),
        (
            "openai/qwen3.7-max",
            {"reasoning_choice": "off", "enable_thinking": False},
            {
                "max_tokens": 131000,
                "extra_headers": {"x-dashscope-session-cache": "enable"},
                "extra_body": {"enable_thinking": False},
                "tool_choice": "auto",
            },
            [ALPHA_TOOL, ZETA_TOOL],
            [
                {
                    "role": "system",
                    "content": [
                        {
                            "type": "text",
                            "text": "stable",
                            "cache_control": {"type": "ephemeral"},
                        }
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": "inspect",
                            "cache_control": {"type": "ephemeral"},
                        }
                    ],
                },
            ],
        ),
        (
            "openai/qwen3.8-max",
            {
                "reasoning_choice": "medium",
                "enable_thinking": True,
                "reasoning_effort": "medium",
            },
            {
                "max_tokens": 131000,
                "extra_headers": {"x-dashscope-session-cache": "enable"},
                "extra_body": {
                    "enable_thinking": True,
                    "reasoning_effort": "medium",
                },
                "tool_choice": "auto",
            },
            [ALPHA_TOOL, ZETA_TOOL],
            [
                {
                    "role": "system",
                    "content": [
                        {
                            "type": "text",
                            "text": "stable",
                            "cache_control": {"type": "ephemeral"},
                        }
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": "inspect",
                            "cache_control": {"type": "ephemeral"},
                        }
                    ],
                },
            ],
        ),
        (
            "openai/LongCat-2.0",
            {"reasoning_choice": "on", "enable_thinking": True},
            {
                "max_tokens": 128000,
                "extra_body": {"thinking": {"type": "enabled"}},
                "tool_choice": "auto",
            },
            [ZETA_TOOL, ALPHA_TOOL],
            None,
        ),
        (
            "openai/kimi-k3",
            {"reasoning_choice": "high", "enable_thinking": True, "reasoning_effort": "high"},
            {
                "max_tokens": 131072,
                "extra_body": {
                    "prompt_cache_key": "ycode-a47c32959586-50a50ebc13bc62a9",
                    "reasoning_effort": "high",
                },
                "tool_choice": "auto",
            },
            [ZETA_TOOL, ALPHA_TOOL],
            None,
        ),
    ],
)
def test_model_reasoning_choice_provider_kwargs_are_byte_golden(
    monkeypatch, model, model_cfg, provider_tail, tools, expected_messages
):
    """新思考策略的最终 provider 字节必须精确，不允许混入第二套开关。"""
    import core.llm as llm

    messages = [
        {"role": "system", "content": "stable"},
        {"role": "user", "content": "inspect"},
    ]
    if expected_messages is None:
        expected_messages = deepcopy(messages)
    captured = _capture(
        monkeypatch,
        model=model,
        api_base="https://gateway.example/v1",
        protocol="openai",
        model_cfg=model_cfg,
        messages=messages,
        cache_session_id="golden-reasoning-choice",
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


@pytest.fixture
def test_relay_unknown_gpt5_model_tool_choice_allowlist_is_byte_golden(
    monkeypatch,
):
    """中转站未知 gpt-5* 模型（历史 litellm 时代需放行 allowed_openai_params）：
    自研引擎无内置清单校验，tool_choice 原样透传，请求不含放行字段，
    其余字节与普通 openai 路由完全一致。"""
    import core.llm as llm

    messages = [
        {"role": "system", "content": "stable"},
        {"role": "user", "content": "inspect"},
    ]
    expected_messages = deepcopy(messages)
    captured = _capture(
        monkeypatch, model="openai/gpt-5.6-sol",
        api_base="https://relay.example/v1", protocol="openai",
        model_cfg={}, messages=messages,
    )
    expected = {
        "model": "openai/gpt-5.6-sol",
        "api_key": "<redacted>",
        "messages": expected_messages,
        "tools": [ZETA_TOOL, ALPHA_TOOL],
        "stream": True,
        "stream_options": {"include_usage": True},
        "num_retries": 2,
        "max_tokens": 32000,
        "tool_choice": "auto",
        "api_base": "https://relay.example/v1",
        "timeout": llm.API_REQUEST_TIMEOUT_SECONDS,
    }
    assert_exact_provider_kwargs(captured, expected)


def test_known_gpt5_model_request_stays_free_of_tool_choice_allowlist(
    monkeypatch,
):
    """gpt-5* 模型请求不含放行字段（自研引擎透传 tool_choice，无需白名单）。"""
    import core.llm as llm

    captured = _capture(
        monkeypatch, model="openai/gpt-5.6-sol",
        api_base="https://relay.example/v1", protocol="openai",
        model_cfg={},
        messages=[{"role": "system", "content": "stable"}, {"role": "user", "content": "inspect"}],
    )
    assert len(captured) == 1
    assert "allowed_openai_params" not in captured[0]
    assert captured[0]["tool_choice"] == "auto"



def test_native_computer_first_and_continuation_requests_are_byte_golden(
    monkeypatch, tmp_path
):
    """Task 11b：原生 computer 首请求与 continuation 请求的 provider 字节精确。

    首请求：正常会话历史 input + 尾部 computer built-in，无 previous_response_id。
    continuation：重发同一 model、同序 tools、必要 instructions/options 与上一
    response id；input 只有本批 computer_call_output，无 messages 键。
    """
    import threading

    import core.llm as llm
    from core.computer.models import ComputerOwner
    from tests.computer_fakes import NATIVE_BROWSER_CAPABILITY, FakeController

    png_path = tmp_path / "native-obs.png"
    png_path.write_bytes(b"png")
    owner = ComputerOwner(
        session_id="golden-native",
        turn_id="turn-golden",
        session_label="golden-native",
        cancel_event=threading.Event(),
    )
    controller = FakeController(
        owner=owner,
        capability=NATIVE_BROWSER_CAPABILITY,
        run_id="run-native",
        png_path=str(png_path),
    )
    captured = []

    def fake_responses(**kwargs):
        captured.append(deepcopy(kwargs))
        if len(captured) == 1:
            return iter([
                {"type": "response.created", "response": {"id": "resp_1"}},
                {"type": "response.output_item.done", "item": {
                    "type": "computer_call",
                    "id": "cc_1",
                    "call_id": "call_1",
                    "status": "completed",
                    "actions": [{"type": "click", "x": 1, "y": 1, "button": "left"}],
                }},
                {"type": "response.completed", "response": {"id": "resp_1", "output": []}},
            ])
        return iter([
            {"type": "response.output_text.delta", "delta": "完成"},
            {"type": "response.completed", "response": {"id": "resp_2", "output": []}},
        ])

    monkeypatch.setattr(llm, "estimate_tokens", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(llm, "get_tools_schema", lambda: [ZETA_TOOL, ALPHA_TOOL])
    monkeypatch.setattr(
        "core.model_manager.get_model_protocol",
        lambda *_args, **_kwargs: "responses",
    )
    monkeypatch.setattr(
        "core.model_manager.get_model_config_by_litellm_model",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(
        "core.memory_kairos.build_memory_system_section",
        lambda: "",
    )
    monkeypatch.setattr(llm, "_describe_images_with_secondary", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(llm, "get_computer_control_service", lambda: controller)
    monkeypatch.setattr(llm, "_dispatch_responses", fake_responses)
    list(llm.chat_with_model_events(
        "openai/gpt-native-golden",
        "golden-secret",
        [
            {"role": "system", "content": "stable"},
            {"role": "user", "content": "inspect"},
        ],
        "https://api.openai.com/v1",
        persist=False,
        consume_background_notifications=False,
        computer_capability=NATIVE_BROWSER_CAPABILITY,
    ))

    assert len(captured) == 2
    first_expected = {
        "model": "openai/gpt-native-golden",
        "api_key": "<redacted>",
        "input": [
            {"role": "system", "content": "stable"},
            {"role": "user", "content": "inspect"},
        ],
        "tools": [
            {"type": "function", "name": "zeta", "description": "zeta", "parameters": {"type": "object", "properties": {}}},
            {"type": "function", "name": "alpha", "description": "alpha", "parameters": {"type": "object", "properties": {}}},
            {"type": "computer"},
        ],
        "stream": True,
        "timeout": llm.API_REQUEST_TIMEOUT_SECONDS,
        "api_base": "https://api.openai.com/v1",
        "max_output_tokens": 32000,
        "tool_choice": "auto",
        "num_retries": 2,
    }
    assert_exact_provider_kwargs(captured[:1], first_expected)
    continuation_expected = {
        "model": "openai/gpt-native-golden",
        "api_key": "<redacted>",
        "input": [{
            "type": "computer_call_output",
            "call_id": "call_1",
            "output": {"type": "computer_screenshot", "image_url": "data:image/png;base64,cG5n"},
        }],
        "tools": [
            {"type": "function", "name": "zeta", "description": "zeta", "parameters": {"type": "object", "properties": {}}},
            {"type": "function", "name": "alpha", "description": "alpha", "parameters": {"type": "object", "properties": {}}},
            {"type": "computer"},
        ],
        "stream": True,
        "previous_response_id": "resp_1",
        "instructions": "stable",
        "api_base": "https://api.openai.com/v1",
        "max_output_tokens": 32000,
        "tool_choice": "auto",
        "num_retries": 2,
        "timeout": llm.API_REQUEST_TIMEOUT_SECONDS,
    }
    assert_exact_provider_kwargs(captured[1:], continuation_expected)


def test_exact_model_id_reaches_final_provider_payload(monkeypatch):
    """重复 route 时，真实事件入口必须使用当前模型 ID 对应的选择。"""
    import core.llm as llm

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
    expected = {
        "model": "openai/deepseek-v4-flash",
        "api_key": "<redacted>",
        "messages": [{"role": "user", "content": "inspect"}],
        "tools": [ALPHA_TOOL, ZETA_TOOL],
        "stream": True,
        "stream_options": {"include_usage": True},
        "num_retries": 2,
        "max_tokens": 384000,
        "extra_body": {"thinking": {"type": "disabled"}},
        "tool_choice": "auto",
        "api_base": "https://gateway.example/v1",
        "timeout": llm.API_REQUEST_TIMEOUT_SECONDS,
    }

    assert_exact_provider_kwargs(captured, expected)

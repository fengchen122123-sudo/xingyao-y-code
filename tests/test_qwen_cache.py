import time

from copy import deepcopy

from core.llm import (
    _is_official_dashscope_route,
    _is_qwen_family,
    _supports_reasoning_fields,
    _supports_cache_keepalive,
    _supports_cache_control,
    _select_request_tools_for_route,
    _apply_cache_control,
    _attach_qwen_request_extras,
    _provider_request_options,
    _provider_cache_affinity_options,
    _strip_media_and_merge,
)
from core.cache_keepalive import build_keepalive_request
from core.harness.request_envelope import _cache_lane_sha256


def test_official_dashscope_route_detection():
    assert _is_official_dashscope_route("https://dashscope.aliyuncs.com/compatible-mode/v1")
    assert _is_official_dashscope_route("https://dashscope.aliyuncs.com")
    assert _is_official_dashscope_route("https://dashscope-intl.aliyuncs.com/compatible-mode/v1")
    assert _is_official_dashscope_route("https://workspace-123.cn-beijing.maas.aliyuncs.com/compatible-mode/v1")
    assert _is_official_dashscope_route("https://dashscope.cn-hangzhou.aliyuncs.com/compatible-mode/v1")

    assert not _is_official_dashscope_route("https://gateway.example/v1")
    assert not _is_official_dashscope_route("https://api.siliconflow.cn/v1")
    assert not _is_official_dashscope_route("http://dashscope.aliyuncs.com")
    assert not _is_official_dashscope_route("https://dashscope.aliyuncs.com:8443")
    assert not _is_official_dashscope_route("https://user:pass@dashscope.aliyuncs.com")
    assert not _is_official_dashscope_route("")
    assert not _is_official_dashscope_route(None)


def test_qwen_family_detection():
    assert _is_qwen_family("openai/qwen3.8-max")
    assert _is_qwen_family("qwen-plus")
    assert _is_qwen_family("qwen-max")
    assert _is_qwen_family("qwen3.8-flash")
    assert _is_qwen_family("qwen38-27b")
    assert _is_qwen_family("dashscope/qwen-turbo")
    assert _is_qwen_family("custom-model", "https://dashscope.aliyuncs.com/compatible-mode/v1")

    assert not _is_qwen_family("openai/deepseek-v4")
    assert not _is_qwen_family("kimi-k3")
    assert not _is_qwen_family("glm-5.2")


def test_qwen_reasoning_fields_preserved_in_history():
    model = "openai/qwen3.8-max"
    api_base = "https://dashscope.aliyuncs.com/compatible-mode/v1"

    assert _supports_reasoning_fields(model, api_base)

    messages = [
        {"role": "system", "content": "stable system prompt"},
        {"role": "user", "content": "帮我看看代码"},
        {
            "role": "assistant",
            "content": "好的，我来读取文件",
            "reasoning_content": "用户想让我读取代码，我应该调用 read_file 工具...",
            "tool_calls": [{"id": "call-1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": "call-1", "name": "read_file", "content": "def hello(): pass"},
    ]

    cleaned, folded, keep = _strip_media_and_merge(messages, model, api_base)

    assistant_msg = next(m for m in cleaned if m.get("role") == "assistant")
    assert assistant_msg.get("reasoning_content") == "用户想让我读取代码，我应该调用 read_file 工具..."


def test_qwen_tools_canonicalization():
    tool_z = {"type": "function", "function": {"name": "zeta_tool", "description": "z"}}
    tool_a = {"type": "function", "function": {"name": "alpha_tool", "description": "a"}}
    tools = [tool_z, tool_a]

    sorted_tools = _select_request_tools_for_route(
        "openai/qwen3.8-max",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        tools,
    )
    assert [t["function"]["name"] for t in sorted_tools] == ["alpha_tool", "zeta_tool"]

    sorted_gateway_tools = _select_request_tools_for_route(
        "openai/qwen3.8-max",
        "https://gateway.example/v1",
        tools,
    )
    assert [t["function"]["name"] for t in sorted_gateway_tools] == ["alpha_tool", "zeta_tool"]


def test_qwen_session_cache_header_injection(monkeypatch):
    from core.model_manager import load_config
    monkeypatch.setattr("core.model_manager.load_config", lambda: {})

    official_base = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    gateway_base = "https://gateway.example/v1"

    opts = _provider_request_options(
        "openai/qwen3.8-max",
        official_base,
        cache_scope="main",
    )
    assert opts.get("extra_headers", {}).get("x-dashscope-session-cache") == "enable"

    opts_bg = _provider_request_options(
        "openai/qwen3.8-max",
        official_base,
        cache_scope="background",
    )
    assert "x-dashscope-session-cache" not in opts_bg.get("extra_headers", {})

    opts_gateway = _provider_request_options(
        "openai/qwen3.8-max",
        gateway_base,
        cache_scope="main",
    )
    assert opts_gateway.get("extra_headers", {}).get("x-dashscope-session-cache") == "enable"

    affinity_official = _provider_cache_affinity_options(
        "openai/qwen3.8-max",
        official_base,
        cache_scope="main",
    )
    assert affinity_official.get("extra_headers", {}).get("x-dashscope-session-cache") == "enable"


def test_qwen_cache_control_enabled_across_channels():
    official_base = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    gateway_base = "https://gateway.example/v1"

    assert _supports_cache_control("openai/qwen3.8-max", official_base)
    assert _supports_cache_control("openai/qwen3.8-max", gateway_base)

    messages = [
        {"role": "system", "content": "stable system prompt"},
        {"role": "user", "content": "round 1 question"},
        {"role": "assistant", "content": "round 1 answer"},
        {"role": "user", "content": "round 2 question"},
    ]

    marked = _apply_cache_control(deepcopy(messages), "openai/qwen3.8-max", official_base)
    assert isinstance(marked[0]["content"], list)
    assert marked[0]["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert marked[0]["content"][0]["text"] == "stable system prompt"

    assert isinstance(marked[2]["content"], list)
    assert marked[2]["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert marked[2]["content"][0]["text"] == "round 1 answer"

    marked_gateway = _apply_cache_control(deepcopy(messages), "openai/qwen3.8-max", gateway_base)
    assert isinstance(marked_gateway[0]["content"], list)
    assert marked_gateway[0]["content"][0]["cache_control"] == {"type": "ephemeral"}


def test_qwen_keepalive_support_and_request_building():
    official_base = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    gateway_base = "https://gateway.example/v1"

    assert _supports_cache_keepalive("openai/qwen3.8-max", official_base)
    assert _supports_cache_keepalive("openai/qwen3.8-max", gateway_base)

    original_req = {
        "model": "openai/qwen3.8-max",
        "api_key": "sk-test",
        "api_base": official_base,
        "messages": [{"role": "system", "content": "stable"}],
        "tools": [{"type": "function", "function": {"name": "read_file"}}],
        "extra_headers": {"x-dashscope-session-cache": "enable"},
        "extra_body": {"enable_thinking": True, "reasoning_effort": "medium"},
        "tool_choice": "auto",
        "stream": True,
        "max_tokens": 8192,
    }

    keepalive_req = build_keepalive_request(original_req)

    assert keepalive_req["max_tokens"] == 1
    assert keepalive_req["stream"] is False
    assert keepalive_req["extra_headers"]["x-dashscope-session-cache"] == "enable"
    assert keepalive_req["extra_body"]["enable_thinking"] is False
    assert "reasoning_effort" not in keepalive_req["extra_body"]
    assert keepalive_req["tool_choice"] == "none"


def test_qwen_keepalive_default_thinking_tier_disables_thinking():
    official_base = "https://dashscope.aliyuncs.com/compatible-mode/v1"

    no_extra_req = {
        "model": "openai/qwen3.8-flash",
        "api_key": "sk-test",
        "api_base": official_base,
        "messages": [{"role": "system", "content": "stable"}],
        "extra_headers": {"x-dashscope-session-cache": "enable"},
    }
    keepalive_req = build_keepalive_request(no_extra_req)
    assert keepalive_req["max_tokens"] == 1
    assert keepalive_req["extra_body"]["enable_thinking"] is False

    other_extra_req = {
        "model": "openai/qwen3.8-flash",
        "api_key": "sk-test",
        "api_base": official_base,
        "messages": [{"role": "system", "content": "stable"}],
        "extra_body": {"some_flag": True},
    }
    keepalive_req2 = build_keepalive_request(other_extra_req)
    assert keepalive_req2["extra_body"]["enable_thinking"] is False
    assert keepalive_req2["extra_body"]["some_flag"] is True

    disabled_req = {
        "model": "openai/qwen3.8-flash",
        "api_key": "sk-test",
        "api_base": official_base,
        "messages": [{"role": "system", "content": "stable"}],
        "extra_body": {"enable_thinking": False},
    }
    keepalive_req3 = build_keepalive_request(disabled_req)
    assert keepalive_req3["extra_body"]["enable_thinking"] is False

    non_qwen_req = {
        "model": "openai/some-model",
        "api_key": "sk-test",
        "api_base": "https://gateway.example/v1",
        "messages": [{"role": "system", "content": "stable"}],
    }
    keepalive_req4 = build_keepalive_request(non_qwen_req)
    assert "extra_body" not in keepalive_req4


def test_request_envelope_lane_sha_captures_dashscope_session_cache():
    kwargs = {
        "extra_headers": {"x-dashscope-session-cache": "enable"},
    }
    sha = _cache_lane_sha256(kwargs)
    assert len(sha) == 64
    assert _cache_lane_sha256({}) == ""


def test_request_envelope_lane_sha_distinguishes_qwen_lanes():
    common = {"extra_headers": {"x-dashscope-session-cache": "enable"}}
    sha_max = _cache_lane_sha256({
        "model": "openai/qwen3.8-max",
        "api_base": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        **common,
    })
    sha_flash = _cache_lane_sha256({
        "model": "openai/qwen3.8-flash",
        "api_base": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        **common,
    })
    sha_gateway = _cache_lane_sha256({
        "model": "openai/qwen3.8-max",
        "api_base": "https://gateway.example/v1",
        **common,
    })
    assert len(sha_max) == 64
    assert sha_max != sha_flash
    assert sha_max != sha_gateway
    assert sha_max == _cache_lane_sha256({
        "model": "openai/qwen3.8-max",
        "api_base": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        **common,
    })
    assert _cache_lane_sha256({"model": "openai/qwen3.8-max"}) == ""


def test_qwen_cache_enabled_regardless_of_config(monkeypatch):
    gateway_base = "https://tokenrhythm.studio/v1"
    model = "openai/qwen3.8-flash"

    monkeypatch.setattr("core.model_manager.load_config", lambda: {
        "providers": [{
            "name": "TokenRhythm",
            "base_url": gateway_base,
            "models": [{"litellm_model": model, "name": "qwen3.8-flash"}],
        }]
    })
    from core.llm import _qwen_cache_enabled, _supports_cache_keepalive, _supports_cache_control
    assert _qwen_cache_enabled(model, gateway_base)
    assert _supports_cache_keepalive(model, gateway_base)
    assert _supports_cache_control(model, gateway_base)
    opts = _provider_request_options(model, gateway_base, cache_scope="main")
    assert opts.get("extra_headers", {}).get("x-dashscope-session-cache") == "enable"

    monkeypatch.setattr("core.model_manager.load_config", lambda: {
        "providers": [{
            "name": "TokenRhythm",
            "base_url": gateway_base,
            "enable_prompt_cache": True,
            "models": [{"litellm_model": model, "name": "qwen3.8-flash"}],
        }]
    })
    assert _qwen_cache_enabled(model, gateway_base)
    assert _supports_cache_keepalive(model, gateway_base)
    opts = _provider_request_options(model, gateway_base, cache_scope="main")
    assert opts.get("extra_headers", {}).get("x-dashscope-session-cache") == "enable"

    monkeypatch.setattr("core.model_manager.load_config", lambda: {
        "providers": [{
            "name": "TokenRhythm",
            "base_url": gateway_base,
            "models": [{
                "litellm_model": model,
                "name": "qwen3.8-flash",
                "enable_cache_control": True,
            }],
        }]
    })
    assert _supports_cache_control(model, gateway_base)
    messages = [
        {"role": "system", "content": "stable system prompt"},
        {"role": "user", "content": "hello"},
    ]
    marked = _apply_cache_control(deepcopy(messages), model, gateway_base)
    assert isinstance(marked[0]["content"], list)
    assert marked[0]["content"][0]["cache_control"] == {"type": "ephemeral"}


def test_qwen_subagent_and_benchmark_scope_header_injection():
    official_base = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    model = "openai/qwen3.8-max"

    opts_bm = _provider_request_options(model, official_base, cache_scope="benchmark:main")
    assert opts_bm.get("extra_headers", {}).get("x-dashscope-session-cache") == "enable"

    opts_sub = _provider_request_options(model, official_base, cache_scope="subagent:worker-1")
    assert opts_sub.get("extra_headers", {}).get("x-dashscope-session-cache") == "enable"

    opts_bg = _provider_request_options(model, official_base, cache_scope="background")
    assert "x-dashscope-session-cache" not in opts_bg.get("extra_headers", {})


def test_qwen_cache_control_400_downgrade(monkeypatch):
    from core.llm import _disable_cache_control_route, _cache_control_disabled_routes
    gateway_base = "https://custom-gateway.io/v1"
    model = "openai/qwen3.8-max"

    monkeypatch.setattr("core.model_manager.load_config", lambda: {
        "providers": [{
            "name": "Custom",
            "base_url": gateway_base,
            "enable_cache_control": True,
            "models": [{"litellm_model": model}],
        }]
    })
    assert _supports_cache_control(model, gateway_base)

    route_key = _disable_cache_control_route(model, gateway_base)
    try:
        assert not _supports_cache_control(model, gateway_base)

        messages = [
            {"role": "system", "content": "prompt"},
            {"role": "user", "content": "hi"},
        ]
        result = _apply_cache_control(deepcopy(messages), model, gateway_base)
        assert result[0]["content"] == "prompt"
    finally:
        _cache_control_disabled_routes.pop(route_key, None)


def test_qwen_tools_canonicalization_includes_computer_tool():
    tools = [
        {"type": "function", "function": {"name": "zeta_tool", "description": "z"}},
        {"type": "function", "function": {"name": "computer", "description": "desktop"}},
        {"type": "function", "function": {"name": "alpha_tool", "description": "a"}},
    ]

    sorted_tools = _select_request_tools_for_route(
        "openai/qwen3.8-max",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        tools,
    )
    assert [t["function"]["name"] for t in sorted_tools] == ["alpha_tool", "computer", "zeta_tool"]


def test_qwen_tools_canonicalized_for_third_party_route(monkeypatch):
    gateway_base = "https://tokenrhythm.studio/v1"
    model = "openai/qwen3.8-flash"

    monkeypatch.setattr("core.model_manager.load_config", lambda: {
        "providers": [{
            "name": "TokenRhythm",
            "base_url": gateway_base,
            "models": [{
                "litellm_model": model,
                "name": "qwen3.8-flash",
                "enable_cache_control": True,
            }],
        }]
    })
    from core.llm import _qwen_cache_enabled, _select_request_tools_for_route

    tools = [
        {"type": "function", "function": {"name": "zeta_tool", "description": "z"}},
        {"type": "function", "function": {"name": "alpha_tool", "description": "a"}},
    ]
    sorted_tools = _select_request_tools_for_route(model, gateway_base, tools)
    assert [t["function"]["name"] for t in sorted_tools] == ["alpha_tool", "zeta_tool"]
    assert _qwen_cache_enabled(model, gateway_base)


def test_cache_benchmark_qwen_parameters():

    official_base = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    model = "openai/qwen3.8-max"

    messages = [
        {"role": "system", "content": "system benchmark"},
        {"role": "user", "content": "hi"},
    ]
    cleaned, _, _ = _strip_media_and_merge(messages, model, official_base)
    marked = _apply_cache_control(cleaned, model, official_base)
    assert isinstance(marked[0]["content"], list)
    assert marked[0]["content"][0]["cache_control"] == {"type": "ephemeral"}

    options = _provider_request_options(
        model,
        official_base,
        cache_scope="benchmark:main",
        cache_session_id="bench-sess-1",
        tool_contract="tc-1",
    )
    assert options.get("extra_headers", {}).get("x-dashscope-session-cache") == "enable"


_OFFICIAL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
_QWEN = "openai/qwen3.8-max"


def _marker_count(messages):
    total = 0
    for msg in messages:
        if isinstance(msg, dict) and "cache_control" in msg:
            total += 1
        content = msg.get("content") if isinstance(msg, dict) else None
        if isinstance(content, list):
            total += sum(
                1 for item in content if isinstance(item, dict) and "cache_control" in item
            )
    return total


def test_qwen_tail_breakpoint_marks_last_user_message():
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "q2"},
    ]
    marked = _apply_cache_control(deepcopy(messages), _QWEN, _OFFICIAL)
    assert marked[-1]["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert marked[-1]["content"][0]["text"] == "q2"
    assert _marker_count(marked) == 3


def test_qwen_tail_breakpoint_skips_tool_message():
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}
        ]},
        {"role": "tool", "tool_call_id": "c1", "name": "read_file", "content": "file body"},
    ]
    marked = _apply_cache_control(deepcopy(messages), _QWEN, _OFFICIAL)
    assert marked[-1]["content"] == "file body"
    assert _marker_count(marked) == 1


def test_qwen_tool_loop_boundary_falls_back_to_text_assistant():
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "q2"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "name": "read_file", "content": "file body"},
    ]
    marked = _apply_cache_control(deepcopy(messages), _QWEN, _OFFICIAL)
    assert _marker_count(marked) == 2
    a1 = marked[2]
    assert isinstance(a1["content"], list)
    assert a1["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert a1["content"][0]["text"] == "a1"
    assert marked[4]["content"] == ""
    assert marked[-1]["content"] == "file body"


def test_qwen_tail_breakpoint_not_duplicated_on_last_assistant():
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1"},
    ]
    marked = _apply_cache_control(deepcopy(messages), _QWEN, _OFFICIAL)
    assert _marker_count(marked) == 2
    assert sum(
        1 for item in marked[-1]["content"] if isinstance(item, dict) and "cache_control" in item
    ) == 1


def test_qwen_marker_count_stays_within_provider_limit():
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "q2"},
        {"role": "assistant", "content": "a2"},
        {"role": "user", "content": "q3"},
    ]
    assert _marker_count(_apply_cache_control(deepcopy(messages), _QWEN, _OFFICIAL)) <= 4


_GATEWAY = "https://tokenrhythm.studio/v1"
_GATEWAY_MODEL = "openai/qwen3.8-flash"


def test_keepalive_route_config_lookup_is_cached(monkeypatch):
    reads = []

    def fake_load_config():
        reads.append(1)
        return {"cache_keepalive_routes": ["tokenrhythm.studio"]}

    monkeypatch.setattr("core.model_manager.load_config", fake_load_config)
    for _ in range(6):
        assert _supports_cache_keepalive("openai/some-model", _GATEWAY)
    assert len(reads) == 1


def test_keepalive_route_config_lookup_refreshes_when_config_changes(monkeypatch):
    monkeypatch.setattr("core.model_manager.load_config", lambda: {
        "cache_keepalive_routes": []
    })
    assert not _supports_cache_keepalive("openai/some-model", _GATEWAY)

    monkeypatch.setattr("core.model_manager.load_config", lambda: {
        "cache_keepalive_routes": ["tokenrhythm.studio"]
    })
    assert _supports_cache_keepalive("openai/some-model", _GATEWAY)


class _FakeBadRequest(Exception):
    status_code = 400


class _FakeServerError(Exception):
    status_code = 500


def test_cache_control_rejection_requires_bad_request_status():
    from core.llm import _is_cache_control_rejection

    assert _is_cache_control_rejection(
        _FakeBadRequest("Error code: 400 - unknown field 'cache_control' in content")
    )
    assert not _is_cache_control_rejection(
        RuntimeError("connection reset while uploading cache_control payload (4000 bytes)")
    )
    assert not _is_cache_control_rejection(
        _FakeServerError("500 internal server error mentioning ephemeral schema")
    )
    assert not _is_cache_control_rejection(
        _FakeBadRequest("Error code: 400 - context length exceeded, invalid prompt")
    )


def test_cache_control_blacklist_expires_after_ttl():
    from core.llm import _disable_cache_control_route

    assert _supports_cache_control(_QWEN, _OFFICIAL)
    _disable_cache_control_route(_QWEN, _OFFICIAL, ttl_seconds=0.05)
    assert not _supports_cache_control(_QWEN, _OFFICIAL)
    time.sleep(0.08)
    assert _supports_cache_control(_QWEN, _OFFICIAL)

"""Tests for Qwen and DashScope prompt cache hit rate optimizations."""

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
    # 官方端点
    assert _is_official_dashscope_route("https://dashscope.aliyuncs.com/compatible-mode/v1")
    assert _is_official_dashscope_route("https://dashscope.aliyuncs.com")
    assert _is_official_dashscope_route("https://dashscope-intl.aliyuncs.com/compatible-mode/v1")
    assert _is_official_dashscope_route("https://workspace-123.cn-beijing.maas.aliyuncs.com/compatible-mode/v1")
    assert _is_official_dashscope_route("https://dashscope.cn-hangzhou.aliyuncs.com/compatible-mode/v1")

    # 非官方端点 / 第三方中转
    assert not _is_official_dashscope_route("https://gateway.example/v1")
    assert not _is_official_dashscope_route("https://api.siliconflow.cn/v1")
    assert not _is_official_dashscope_route("http://dashscope.aliyuncs.com")  # 非 https
    assert not _is_official_dashscope_route("https://dashscope.aliyuncs.com:8443")  # 非 443
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

    # 非 Qwen 模型
    assert not _is_qwen_family("openai/deepseek-v4")
    assert not _is_qwen_family("kimi-k3")
    assert not _is_qwen_family("glm-5.2")


def test_qwen_reasoning_fields_preserved_in_history():
    model = "openai/qwen3.8-max"
    api_base = "https://dashscope.aliyuncs.com/compatible-mode/v1"

    assert _supports_reasoning_fields(model, api_base)

    # 模拟多轮对话：上一轮 assistant 消息中带有思考模型的 reasoning_content
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

    # 验证第 2 条 assistant 消息保留了 reasoning_content，没有被剥离
    assistant_msg = next(m for m in cleaned if m.get("role") == "assistant")
    assert assistant_msg.get("reasoning_content") == "用户想让我读取代码，我应该调用 read_file 工具..."


def test_qwen_tools_canonicalization():
    tool_z = {"type": "function", "function": {"name": "zeta_tool", "description": "z"}}
    tool_a = {"type": "function", "function": {"name": "alpha_tool", "description": "a"}}
    tools = [tool_z, tool_a]

    # 百炼官方路由：工具按 function.name 字母序排序
    sorted_tools = _select_request_tools_for_route(
        "openai/qwen3.8-max",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        tools,
    )
    assert [t["function"]["name"] for t in sorted_tools] == ["alpha_tool", "zeta_tool"]

    # 第三方路由（任意中转）：同样排序（判定只看模型家族）
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

    # 官方主会话请求注入 extra_headers
    opts = _provider_request_options(
        "openai/qwen3.8-max",
        official_base,
        cache_scope="main",
    )
    assert opts.get("extra_headers", {}).get("x-dashscope-session-cache") == "enable"

    # 非主会话（如 background / keepalive）不盲目注入
    opts_bg = _provider_request_options(
        "openai/qwen3.8-max",
        official_base,
        cache_scope="background",
    )
    assert "x-dashscope-session-cache" not in opts_bg.get("extra_headers", {})

    # 第三方网关同样注入（判定只看模型家族）
    opts_gateway = _provider_request_options(
        "openai/qwen3.8-max",
        gateway_base,
        cache_scope="main",
    )
    assert opts_gateway.get("extra_headers", {}).get("x-dashscope-session-cache") == "enable"

    # 亲和参数提取测试
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

    # 官方百炼路由：转换为 content block 形式并携带 cache_control
    marked = _apply_cache_control(deepcopy(messages), "openai/qwen3.8-max", official_base)
    assert isinstance(marked[0]["content"], list)
    assert marked[0]["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert marked[0]["content"][0]["text"] == "stable system prompt"

    # 最后一个 assistant 消息也打上断点
    assert isinstance(marked[2]["content"], list)
    assert marked[2]["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert marked[2]["content"][0]["text"] == "round 1 answer"

    # 第三方网关同样打 cache_control 断点
    marked_gateway = _apply_cache_control(deepcopy(messages), "openai/qwen3.8-max", gateway_base)
    assert isinstance(marked_gateway[0]["content"], list)
    assert marked_gateway[0]["content"][0]["cache_control"] == {"type": "ephemeral"}


def test_qwen_keepalive_support_and_request_building():
    official_base = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    gateway_base = "https://gateway.example/v1"

    # 家族判定：官方与第三方路由都放行保活
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

    # 验证保活请求配置：输出限制 1 token、关闭思考、保留 session 缓存 header
    assert keepalive_req["max_tokens"] == 1
    assert keepalive_req["stream"] is False
    assert keepalive_req["extra_headers"]["x-dashscope-session-cache"] == "enable"
    assert keepalive_req["extra_body"]["enable_thinking"] is False
    assert "reasoning_effort" not in keepalive_req["extra_body"]
    assert keepalive_req["tool_choice"] == "none"


def test_qwen_keepalive_default_thinking_tier_disables_thinking():
    """Qwen 默认思考档（用户未显式配置思考）的保活快照必须补 enable_thinking=False。

    Qwen3.x 未显式配置时默认开启思考，保活 max_tokens=1 会因思考输出受限被
    provider 拒绝，首次保活报错即令保活服务自我停用。"""
    official_base = "https://dashscope.aliyuncs.com/compatible-mode/v1"

    # 1. extra_body 缺失（default 档未注入任何思考参数）：补 enable_thinking=False
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

    # 2. extra_body 存在但无思考键（仅携带其他参数）：同样补关思考
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

    # 3. 用户显式关闭思考（enable_thinking=False）：保持原样不覆盖
    disabled_req = {
        "model": "openai/qwen3.8-flash",
        "api_key": "sk-test",
        "api_base": official_base,
        "messages": [{"role": "system", "content": "stable"}],
        "extra_body": {"enable_thinking": False},
    }
    keepalive_req3 = build_keepalive_request(disabled_req)
    assert keepalive_req3["extra_body"]["enable_thinking"] is False

    # 4. 非 Qwen 模型且无思考配置：不注入任何键，保活快照保持既有字节
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
    """Qwen lane 指纹必须随模型/路由变化。

    只哈希 header 值（恒为 "enable"）会让所有 Qwen 请求挤进同一条缓存
    通道，journal 诊断无法区分模型或路由；并入模型与路由后同参数请求
    仍字节稳定。会话身份不随请求发送（DashScope 按前缀/密钥侧建缓存），
    故不参与哈希。"""
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
    assert sha_max != sha_flash  # 不同模型不同 lane
    assert sha_max != sha_gateway  # 不同路由不同 lane
    # 同参数稳定复现（保活请求与主请求落同一条 lane）
    assert sha_max == _cache_lane_sha256({
        "model": "openai/qwen3.8-max",
        "api_base": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        **common,
    })
    # 无 session-cache header 的请求不受影响
    assert _cache_lane_sha256({"model": "openai/qwen3.8-max"}) == ""


def test_qwen_cache_enabled_regardless_of_config(monkeypatch):
    """Qwen 家族任意渠道默认全开；配置键保留兼容，存在与否都不影响判定。"""
    gateway_base = "https://tokenrhythm.studio/v1"
    model = "openai/qwen3.8-flash"

    # 1. 未配置任何缓存键：家族判定下默认全开
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
    # session-cache header 同样注入
    opts = _provider_request_options(model, gateway_base, cache_scope="main")
    assert opts.get("extra_headers", {}).get("x-dashscope-session-cache") == "enable"

    # 2. 模拟在 provider 级别开启 enable_prompt_cache
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
    # session-cache header 也会注入
    opts = _provider_request_options(model, gateway_base, cache_scope="main")
    assert opts.get("extra_headers", {}).get("x-dashscope-session-cache") == "enable"

    # 3. 模拟在 model 级别开启 enable_cache_control
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
    """验证 subagent scope 与 benchmark:main 均能正确注入 session-cache header。"""
    official_base = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    model = "openai/qwen3.8-max"

    # benchmark scope
    opts_bm = _provider_request_options(model, official_base, cache_scope="benchmark:main")
    assert opts_bm.get("extra_headers", {}).get("x-dashscope-session-cache") == "enable"

    # subagent scope
    opts_sub = _provider_request_options(model, official_base, cache_scope="subagent:worker-1")
    assert opts_sub.get("extra_headers", {}).get("x-dashscope-session-cache") == "enable"

    # 其他未认可 scope（如 background）不注入
    opts_bg = _provider_request_options(model, official_base, cache_scope="background")
    assert "x-dashscope-session-cache" not in opts_bg.get("extra_headers", {})


def test_qwen_cache_control_400_downgrade(monkeypatch):
    """验证网关返回 400 拒绝 cache_control 时，能自动登记临时黑名单并降级。"""
    from core.llm import _disable_cache_control_route, _cache_control_disabled_routes
    gateway_base = "https://custom-gateway.io/v1"
    model = "openai/qwen3.8-max"

    # 模拟开启了 enable_cache_control
    monkeypatch.setattr("core.model_manager.load_config", lambda: {
        "providers": [{
            "name": "Custom",
            "base_url": gateway_base,
            "enable_cache_control": True,
            "models": [{"litellm_model": model}],
        }]
    })
    assert _supports_cache_control(model, gateway_base)

    # 模拟触发 400 拒绝后登记临时黑名单
    route_key = _disable_cache_control_route(model, gateway_base)
    try:
        # 黑名单中路由不再支持 cache_control
        assert not _supports_cache_control(model, gateway_base)

        messages = [
            {"role": "system", "content": "prompt"},
            {"role": "user", "content": "hi"},
        ]
        # _apply_cache_control 跳过标记
        result = _apply_cache_control(deepcopy(messages), model, gateway_base)
        assert result[0]["content"] == "prompt"
    finally:
        _cache_control_disabled_routes.pop(route_key, None)


def test_qwen_tools_canonicalization_includes_computer_tool():
    """验证 generic computer_tool 也能在 Qwen 官方路由下正确参与字母序排序。"""
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
    """第三方网关的 tools 必须排序（家族判定），配置不再是必要条件。

    tools 字母序是缓存前缀字节稳定的前提：不排序时 MCP 工具顺序一抖动，
    缓存前缀整体失效。"""
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
    # 缓存开关判定同样只看家族：配置不再是必要条件
    assert _qwen_cache_enabled(model, gateway_base)


def test_cache_benchmark_qwen_parameters():
    """验证 cache_benchmark 中 Qwen 请求参数构造能够正确生成断点与 Session 缓存。"""

    official_base = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    model = "openai/qwen3.8-max"

    messages = [
        {"role": "system", "content": "system benchmark"},
        {"role": "user", "content": "hi"},
    ]
    cleaned, _, _ = _strip_media_and_merge(messages, model, official_base)
    # 模拟 cache_benchmark.py 第 141 行传 api_base
    marked = _apply_cache_control(cleaned, model, official_base)
    assert isinstance(marked[0]["content"], list)
    assert marked[0]["content"][0]["cache_control"] == {"type": "ephemeral"}

    # 模拟 cache_benchmark.py 第 148-154 行传 cache_scope="benchmark:main"
    options = _provider_request_options(
        model,
        official_base,
        cache_scope="benchmark:main",
        cache_session_id="bench-sess-1",
        tool_contract="tc-1",
    )
    assert options.get("extra_headers", {}).get("x-dashscope-session-cache") == "enable"


# ---------------------------------------------------------------------------
# 断点策略：第 3 个断点打在尾部消息，让当轮新增内容立刻写入缓存
# ---------------------------------------------------------------------------

_OFFICIAL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
_QWEN = "openai/qwen3.8-max"


def _marker_count(messages):
    """统计 cache_control 断点总数（含 content 块内与消息顶层两种形态）。"""
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
    """尾部是 user 消息时加第 3 个断点：上一轮写入的缓存能覆盖本轮新问题之前的全部字节。"""
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
    """尾部是 tool 结果时不加断点：OpenAI 兼容协议要求 tool 消息 content 为字符串，
    改成 content 块会被上游判为非法载荷。"""
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
    assert _marker_count(marked) == 1  # 仅 system；末尾无 assistant 文本可标


def test_qwen_tool_loop_boundary_falls_back_to_text_assistant():
    """工具循环内空文本 assistant 打不上 content 块断点：回退标记循环前
    最后一条文本 assistant。

    修复前循环各 Step 只剩 system 断点，本轮新增内容（用户问题 + 全部
    工具结果）每步全额 miss；修复后边界至少推进到循环起点，且与上一轮
    请求字节（该 assistant 当时已带标记）保持一致。"""
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
    # system + 循环前最后一条文本 assistant（a1）共 2 个断点
    assert _marker_count(marked) == 2
    a1 = marked[2]
    assert isinstance(a1["content"], list)
    assert a1["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert a1["content"][0]["text"] == "a1"
    # 空文本 assistant 与 tool 尾部保持原样（协议约束不允许标记）
    assert marked[4]["content"] == ""
    assert marked[-1]["content"] == "file body"


def test_qwen_tail_breakpoint_not_duplicated_on_last_assistant():
    """尾部本来就是最后一条 assistant 时不得重复标记，避免白占断点配额。"""
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
    """任意消息形状下断点总数都不得超过 Anthropic/DashScope 的 4 个上限。"""
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "q2"},
        {"role": "assistant", "content": "a2"},
        {"role": "user", "content": "q3"},
    ]
    assert _marker_count(_apply_cache_control(deepcopy(messages), _QWEN, _OFFICIAL)) <= 4


# ---------------------------------------------------------------------------
# 配置解析：避免每轮对话重复读盘解析 config.json
# ---------------------------------------------------------------------------

_GATEWAY = "https://tokenrhythm.studio/v1"
_GATEWAY_MODEL = "openai/qwen3.8-flash"


def test_keepalive_route_config_lookup_is_cached(monkeypatch):
    """非四族模型的显式保活通道判定：同一路由一轮内多次判定只读一次 config.json。"""
    reads = []

    def fake_load_config():
        reads.append(1)
        return {"cache_keepalive_routes": ["tokenrhythm.studio"]}

    monkeypatch.setattr("core.model_manager.load_config", fake_load_config)
    for _ in range(6):
        assert _supports_cache_keepalive("openai/some-model", _GATEWAY)
    assert len(reads) == 1


def test_keepalive_route_config_lookup_refreshes_when_config_changes(monkeypatch):
    """配置被改写后显式保活通道判定必须跟上，不能永远停留在旧结论。"""
    monkeypatch.setattr("core.model_manager.load_config", lambda: {
        "cache_keepalive_routes": []
    })
    assert not _supports_cache_keepalive("openai/some-model", _GATEWAY)

    monkeypatch.setattr("core.model_manager.load_config", lambda: {
        "cache_keepalive_routes": ["tokenrhythm.studio"]
    })
    assert _supports_cache_keepalive("openai/some-model", _GATEWAY)


# ---------------------------------------------------------------------------
# 400 降级：只在确实是 bad request 时拉黑，且拉黑会在 TTL 到期后自愈
# ---------------------------------------------------------------------------

class _FakeBadRequest(Exception):
    status_code = 400


class _FakeServerError(Exception):
    status_code = 500


def test_cache_control_rejection_requires_bad_request_status():
    """只有上游明确以 400/bad request 拒绝 cache_control 才算数；网络抖动、5xx
    与文案里恰好出现关键字的错误都不得把该路由永久关掉。"""
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
    """拉黑只应临时生效，TTL 到期后自动恢复，避免一次误判永久关掉优化。"""
    from core.llm import _disable_cache_control_route

    assert _supports_cache_control(_QWEN, _OFFICIAL)
    _disable_cache_control_route(_QWEN, _OFFICIAL, ttl_seconds=0.05)
    assert not _supports_cache_control(_QWEN, _OFFICIAL)
    time.sleep(0.08)
    assert _supports_cache_control(_QWEN, _OFFICIAL)

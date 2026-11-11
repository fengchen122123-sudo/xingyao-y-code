"""测试大模型上游报错中文映射与排查建议引擎。"""

import pytest

from core.llm_error_mapper import (
    format_user_facing_error,
    map_llm_error,
    sanitize_error_text,
)


class DummyApiException(Exception):
    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


def test_auth_invalid_401():
    # 1. 明确携带 401 状态码
    exc = DummyApiException("Incorrect API key provided: sk-abcdef1234567890", status_code=401)
    info = map_llm_error(exc)
    assert info.category == "auth_invalid"
    assert "API 凭证无效" in info.title

    formatted = format_user_facing_error(exc)
    assert "【错误诊断】API 凭证无效 (401 Unauthorized)" in formatted
    assert "【排查建议】" in formatted
    assert "sk-abcdef1234567890" not in formatted  # 必须被脱敏
    assert "***" in formatted


def test_model_not_supported_under_401_is_not_blamed_on_api_key():
    """401 + ModelError（模型下线/不被上游支持）不能诊断成"API 凭证无效"。

    实测案例：OpenCode Zen 免费档对已下线模型回
    {"type":"error","error":{"type":"ModelError","message":"Model xxx is not supported"}}，
    状态码是 401。旧逻辑把它归类成凭证问题，用户按提示反复检查 API Key 也解决不了。
    """
    exc = DummyApiException(
        "API 凭证无效或未授权 (HTTP 401): "
        '{"type":"error","error":{"type":"ModelError",'
        '"message":"Model deepseek-v4-flash-free is not supported"}}',
        status_code=401,
    )

    info = map_llm_error(exc)
    assert info.category == "model_unavailable"
    assert "模型不可用" in info.title
    assert "凭证无效" not in info.title

    formatted = format_user_facing_error(exc)
    assert "重新选择" in formatted


def test_insufficient_quota_402():
    # 2. 欠费 / 额度耗尽
    exc = DummyApiException("Error code: 429 - {'error': {'message': 'You exceeded your current quota', 'type': 'insufficient_quota'}}")
    info = map_llm_error(exc)
    assert info.category == "insufficient_quota"
    assert "额度耗尽" in info.title or "余额不足" in info.title

    formatted = format_user_facing_error(exc)
    assert "充值" in formatted or "限额" in formatted


def test_rate_limit_429():
    # 3. 普通 429 速率限制
    exc = DummyApiException("Error code: 429 - Rate limit reached for requests per minute (RPM)", status_code=429)
    info = map_llm_error(exc)
    assert info.category == "rate_limit"
    assert "429" in info.title

    formatted = format_user_facing_error(exc)
    assert "速率超限" in formatted


def test_context_overflow_400():
    # 4. 上下文超限
    exc = DummyApiException("This model's maximum context length is 128000 tokens. However, your messages resulted in 135000 tokens.")
    info = map_llm_error(exc)
    assert info.category == "context_overflow"
    assert "上下文" in info.title

    formatted = format_user_facing_error(exc)
    assert "/new" in formatted or "开新会话" in formatted


def test_permission_denied_403():
    # 5. 地域或权限限制 403
    exc = DummyApiException("User location is not supported for the requested model", status_code=403)
    info = map_llm_error(exc)
    assert info.category == "permission_denied"
    assert "403" in info.title

    formatted = format_user_facing_error(exc)
    assert "地域" in formatted or "权限" in formatted


def test_model_not_found_404():
    # 6. 模型不存在 404
    exc = DummyApiException("The model `gpt-9-turbo` does not exist or you do not have access to it.", status_code=404)
    info = map_llm_error(exc)
    assert info.category == "model_not_found"
    assert "404" in info.title

    formatted = format_user_facing_error(exc)
    assert "模型不存在" in formatted


def test_service_overloaded_503():
    # 7. 服务商过载 503
    exc = DummyApiException("AnthropicError: overloaded_error: Overloaded", status_code=503)
    info = map_llm_error(exc)
    assert info.category == "service_overloaded"
    assert "过载" in info.title


def test_gateway_error_502_504():
    # 8. 网关超时 504
    exc = DummyApiException("504 Gateway Time-out: The server didn't respond in time.")
    info = map_llm_error(exc)
    assert info.category == "gateway_error"
    assert "网关" in info.title


def test_internal_server_error_500():
    # 9. 服务商内部故障 500
    exc = DummyApiException("500 Internal Server Error", status_code=500)
    info = map_llm_error(exc)
    assert info.category == "internal_server_error"
    assert "内部错误" in info.title


def test_network_connection_error():
    # 10. 网络连接错误 / 超时
    class ConnectTimeout(Exception):
        pass

    exc = ConnectTimeout("Failed to connect: [Errno 111] Connection refused to api.deepseek.com")
    info = map_llm_error(exc)
    assert info.category == "network_error"
    assert "网络连接失败" in info.title

    formatted = format_user_facing_error(exc)
    assert "代理" in formatted or "Base URL" in formatted


def test_policy_refusal():
    # 11. 安全审查拦截
    exc = DummyApiException("Your request was rejected due to safety policy violation: content_policy")
    info = map_llm_error(exc)
    assert info.category == "policy_refusal"
    assert "合规" in info.title or "安全" in info.title


def test_sensitive_key_redaction():
    # 12. 敏感信息脱敏
    raw = "Failed with api_key=sk-1234567890abcdef1234567890 and Bearer ghp_99998888777766665555"
    sanitized = sanitize_error_text(raw)
    assert "sk-1234567890abcdef1234567890" not in sanitized
    assert "ghp_99998888777766665555" not in sanitized
    assert "api_key=***" in sanitized
    assert "Bearer ***" in sanitized


def test_already_chinese_message_passthrough():
    # 13. 已有中文消息原样透传
    msg = "已被用户取消，当前会话已停止。"
    assert format_user_facing_error(msg) == msg

    msg2 = "等待模型响应已达 180 秒（约 3 分钟上限）仍未收到首字节，本轮已自动结束"
    assert format_user_facing_error(msg2) == msg2


def test_none_or_empty_error():
    # 14. 空异常鲁棒性
    assert format_user_facing_error(None) == "未知错误"
    assert format_user_facing_error("") == "未知错误"

"""大模型上游报错中文映射与排查建议引擎。

将 LiteLLM、各模型云厂商 (OpenAI/Anthropic/DeepSeek/Kimi/GLM/通义千问等) 返回的
HTTP 状态码、英文异常及 JSON 报错结构，智能解析为对用户友好的中文诊断与操作建议，
同时对敏感 API Key 等进行不可逆脱敏，保留必要技术简讯以供开发者排查。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class LLMErrorInfo:
    """结构化大模型报错信息。"""

    category: str  # 错误简短分类标识 (如 auth, quota, rate_limit, context_overflow 等)
    title: str  # 友好的中文诊断标题 (如 "API 凭证无效 (401)")
    reason: str  # 通俗易懂的原因说明
    suggestion: str  # 明确可行的操作排查指引
    raw_message: str  # 脱敏后的原始报错简讯 (截断在安全长度内)


# 敏感信息脱敏正则
_RE_URL_USERINFO = re.compile(
    r"([a-z][a-z0-9+.-]*://[^\s/:@]+:)[^\s/@]+(@)", re.IGNORECASE
)
_RE_KEY_VALUE_SECRETS = re.compile(
    r"(?i)(\b(?:api[_-]?key|access[_-]?token|token|secret|password|passwd|authorization)\s*[=:]\s*)[^\s,;&\"']+"
)
_RE_BEARER = re.compile(r"(?i)\bBearer\s+[^\s\"',;]+")
_RE_KNOWN_KEY_PREFIX = re.compile(
    r"\b(?:sk|ghp|gho|ghu|ghs|ghr|glpat|hf|xoxb|xoxp|xoxa|AKIA|ASIA|AIza)[-_][A-Za-z0-9_.-]{6,}\b"
)


def sanitize_error_text(text: Any) -> str:
    """对异常文本中的密钥和 URL 凭据进行不可逆脱敏。"""
    s = str(text or "")
    s = _RE_URL_USERINFO.sub(r"\1***\2", s)
    s = _RE_KEY_VALUE_SECRETS.sub(r"\1***", s)
    s = _RE_BEARER.sub("Bearer ***", s)
    s = _RE_KNOWN_KEY_PREFIX.sub("***", s)
    return s


def _extract_status_code(exc: Any, text: str) -> int | None:
    """尝试从异常对象或错误文本中提取 HTTP 状态码。"""
    # 1. 直接读取异常属性
    code = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    if isinstance(code, int) and 100 <= code <= 599:
        return code
    if isinstance(code, str) and code.isdigit() and 100 <= int(code) <= 599:
        return int(code)

    # 2. 从 response 对象读取
    resp = getattr(exc, "response", None)
    if resp is not None:
        resp_code = getattr(resp, "status_code", None)
        if isinstance(resp_code, int) and 100 <= resp_code <= 599:
            return resp_code

    # 3. 正则提取常见模式
    m = re.search(
        r"(?:error[ _-]?code|status[ _-]?code|http(?: status)?|code)\s*[:=]?\s*([45]\d{2})\b",
        text,
        re.IGNORECASE,
    )
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            pass

    m2 = re.search(r"\b([45]\d{2})\b", text)
    if m2:
        val = int(m2.group(1))
        # 常见状态码白名单，避免误匹配普通数字
        if val in {400, 401, 402, 403, 404, 408, 409, 413, 429, 499, 500, 502, 503, 504, 520, 521, 522, 524, 529}:
            return val

    return None


def _is_mostly_chinese(text: str) -> bool:
    """检查文本是否已主要是中文（例如业务已处理过的友好提示），避免重复包裹。"""
    if not text:
        return False
    clean = re.sub(r"\s+", "", text)
    if not clean:
        return False
    chinese_chars = len(re.findall(r"[\u4e00-\u9fff]", clean))
    return (chinese_chars / len(clean)) >= 0.45


def map_llm_error(exc: Any) -> LLMErrorInfo:
    """将大模型报错或异常分类映射为结构化的中文错误信息。"""
    raw_str = sanitize_error_text(str(exc or ""))
    exc_type = type(exc).__name__.lower() if isinstance(exc, Exception) else ""
    text_lower = f"{exc_type} {raw_str}".lower()
    status_code = _extract_status_code(exc, raw_str)

    # 1. 余额欠费 / 额度耗尽 (必须先于普通的 429 速率限制匹配)
    if status_code == 402 or any(
        kw in text_lower
        for kw in (
            "insufficient_quota",
            "insufficient_credits",
            "billing_hard_limit",
            "credit balance",
            "exceeded your current quota",
            "quota_exceeded",
            "quota exceeded",
            "balance not enough",
            "out of credits",
            "账户额度不足",
            "余额不足",
            "欠费",
        )
    ):
        return LLMErrorInfo(
            category="insufficient_quota",
            title="账户额度耗尽 / 余额不足",
            reason="当前大模型服务商账户的 API 调用额度已耗尽，或已触达账户设置的消费硬上限。",
            suggestion="请登录对应的模型服务商官网（如 OpenAI / DeepSeek / Moonshot 等）检查账户余额并充值，或更新为有可用额度的 API Key。",
            raw_message=raw_str,
        )

    # 2. 鉴权失败 / API Key 无效 (401)
    if status_code == 401 or "authenticationerror" in exc_type or any(
        kw in text_lower
        for kw in (
            "invalid_api_key",
            "incorrect api key",
            "invalid api key",
            "unauthorized",
            "authentication failed",
            "api_key_invalid",
            "bearer token is invalid",
            "api key 错误",
            "未授权",
        )
    ):
        return LLMErrorInfo(
            category="auth_invalid",
            title="API 凭证无效 (401 Unauthorized)",
            reason="当前配置的 API Key 错误、已过期、已被服务商撤销，或无权调用当前指定的模型。",
            suggestion="请在「设置 → 模型管理」或 config.json 中核对并重新填写该厂商正确的 API Key，注意检查首尾是否有空格。",
            raw_message=raw_str,
        )

    # 3. 访问权限受限 / 地域不支持 (403)
    if status_code == 403 or "permissiondeniederror" in exc_type or any(
        kw in text_lower
        for kw in (
            "permission_denied",
            "not_available_in_region",
            "unsupported_country",
            "country, region, or territory",
            "access denied",
            "forbidden",
            "无权限",
            "地域受限",
        )
    ):
        return LLMErrorInfo(
            category="permission_denied",
            title="访问权限受限 / 地域不支持 (403 Forbidden)",
            reason="当前账号未开通该模型的访问权限，或当前出口 IP 地域被服务商列入了访问限制名单。",
            suggestion="请检查是否开通了对应的模型权限；如使用境外模型（OpenAI/Claude），请检查科学上网代理的节点配置。",
            raw_message=raw_str,
        )

    # 4. 模型不存在 / 未找到 (404)
    if status_code == 404 or "notfounderror" in exc_type or any(
        kw in text_lower
        for kw in (
            "model_not_found",
            "model not found",
            "not_found_error",
            "no endpoints found",
            "does not exist",
            "模型不存在",
            "未找到模型",
        )
    ):
        return LLMErrorInfo(
            category="model_not_found",
            title="请求的模型不存在 (404 Not Found)",
            reason="所请求的模型名称未被服务商识别，可能是拼写错误、已下线、或该 API 渠道不支持此模型。",
            suggestion="请在设置中核对模型名称是否与服务商官方文档完全一致（如区分大小写及版本后缀）。如使用本地 Ollama，请确认模型已下载（ollama pull）。",
            raw_message=raw_str,
        )

    # 5. 上下文窗口超限 (400 Context Overflow)
    if "contextwindowexceedederror" in exc_type or any(
        kw in text_lower
        for kw in (
            "context_length_exceeded",
            "context window",
            "maximum context",
            "prompt is too long",
            "input is too long",
            "too many tokens",
            "exceeds the context window",
            "超出模型上下文",
            "上下文超限",
        )
    ):
        return LLMErrorInfo(
            category="context_overflow",
            title="上下文窗口超限 (Context Overflow)",
            reason="当前会话历史、系统提示词或携带的文件附件累计 Token 数量超出了该模型的上下文窗口上限。",
            suggestion="建议输入 /new 开启新会话，或在输入时移除过大的附件、长文本文件；也可换用支持更大上下文窗口的模型（如 1M 上下文模型）。",
            raw_message=raw_str,
        )

    # 6. 请求速率超限 / 429 频繁限制
    if status_code == 429 or "ratelimiterror" in exc_type or any(
        kw in text_lower
        for kw in (
            "rate_limit",
            "ratelimit",
            "too many requests",
            "requests per minute",
            "tokens per minute",
            "rpm limit",
            "tpm limit",
            "请求过快",
            "频率超限",
        )
    ):
        return LLMErrorInfo(
            category="rate_limit",
            title="请求速率超限 (429 Too Many Requests)",
            reason="请求发送频率或每分钟消耗的 Token 超过了服务商为您分配的并发限额 (RPM/TPM)。",
            suggestion="请稍等十几秒后再次尝试；若频繁出现，可前往服务商控制台申请提升账号的速率等级或绑定更多备用模型。",
            raw_message=raw_str,
        )

    # 7. 服务商过载或临时维护 (503 / 529)
    if status_code in (503, 529) or any(
        kw in text_lower
        for kw in (
            "overloaded_error",
            "server_overloaded",
            "service unavailable",
            "server is busy",
            "capacity overloaded",
            "服务器繁忙",
            "过载",
            "系统繁忙",
        )
    ):
        return LLMErrorInfo(
            category="service_overloaded",
            title="模型服务商服务器过载 (503 Overloaded)",
            reason="模型服务商云端集群当前处于高并发负载高峰，算力资源暂时饱和排队中。",
            suggestion="服务商临时拥堵，通常会在 1~2 分钟内自行缓解，请稍后重试；若任务紧急，建议临时切换至备用模型提供商。",
            raw_message=raw_str,
        )

    # 8. 网关超时与网关错误 (502 / 504 / 520~524)
    if status_code in (502, 504, 520, 521, 522, 523, 524) or any(
        kw in text_lower
        for kw in (
            "bad gateway",
            "gateway timeout",
            "502 bad gateway",
            "504 gateway time-out",
            "网关错误",
            "网关超时",
        )
    ):
        return LLMErrorInfo(
            category="gateway_error",
            title="上游网关超时 / 故障 (502/504 Gateway Error)",
            reason="位于模型服务器前置的代理网关或 CDN (如 Cloudflare) 出现连接超时或代理中断，模型未在规定时间内产出首字。",
            suggestion="通常是由于大上下文或深度思考推理较慢导致网络超时断开，请稍后重试，或检查 API 基础地址 (Base URL) 是否稳定可用。",
            raw_message=raw_str,
        )

    # 9. 服务商内部故障 (500)
    if status_code == 500 or "internalservererror" in exc_type or any(
        kw in text_lower
        for kw in (
            "internal server error",
            "internal_error",
            "api_error",
            "服务端异常",
            "服务器内部错误",
        )
    ):
        return LLMErrorInfo(
            category="internal_server_error",
            title="模型服务商内部错误 (500 Internal Error)",
            reason="模型服务商内部服务组件发生未处理异常崩溃，非本地代码原因。",
            suggestion="建议等待数分钟后重试，或前往对应服务商的 Status 状态页（如 status.openai.com）查看官方系统运行状况。",
            raw_message=raw_str,
        )

    # 10. 网络连接失败 / 本地超时
    if any(
        kw in exc_type
        for kw in (
            "apiconnectionerror",
            "connecttimeout",
            "readtimeout",
            "timeouterror",
            "connectionerror",
            "networkerror",
        )
    ) or any(
        kw in text_lower
        for kw in (
            "connection refused",
            "connection error",
            "failed to connect",
            "connection reset",
            "remote disconnected",
            "name resolution failed",
            "nodename nor servname provided",
            "getaddrinfo failed",
            "网络连接失败",
            "连接超时",
        )
    ):
        return LLMErrorInfo(
            category="network_error",
            title="网络连接失败 / 请求超时",
            reason="未能成功与大模型 API 服务器建立网络连接，可能是本地网络未连通、DNS 解析失败或代理工具异常。",
            suggestion="1) 检查本地网络与梯子/代理设置；2) 确认配置的 Base URL (接口地址) 是否可以正常访问；3) 检查防火墙是否拦截了 Python 进程。",
            raw_message=raw_str,
        )

    # 11. 内容安全合规拦截
    if any(
        kw in text_lower
        for kw in (
            "content_policy",
            "content policy",
            "safety policy",
            "moderation",
            "policy violation",
            "blocked by policy",
            "policy_refusal",
            "触发安全策略",
            "敏感词",
            "合规拦截",
        )
    ):
        return LLMErrorInfo(
            category="policy_refusal",
            title="内容安全合规拦截 (Policy Refusal)",
            reason="输入的问题或模型拟输出的内容触发了服务商的内容安全审查策略。",
            suggestion="请尝试调整提问表述，避免包含可能引起模型安全对齐机制误伤的敏感词汇或话题。",
            raw_message=raw_str,
        )

    # 12. 请求参数非法 / 400 格式错误
    if status_code == 400 or "badrequesterror" in exc_type or any(
        kw in text_lower
        for kw in (
            "bad_request",
            "invalid_request",
            "schema_validation",
            "unsupported parameter",
            "extra inputs are not permitted",
            "参数错误",
        )
    ):
        return LLMErrorInfo(
            category="bad_request",
            title="请求参数不受支持 (400 Bad Request)",
            reason="发往模型的请求参数（如工具定义、思考强度、格式约束等）未能通过上游服务端的格式校验。",
            suggestion="若近期修改过自定义模型参数，请核对其是否符合该模型规范；也可尝试重启会话重试。",
            raw_message=raw_str,
        )

    # 13. 等待超时（星瑶内部硬上限）
    if "requestwaitgiveuperror" in exc_type or "仍未收到首字节" in raw_str:
        return LLMErrorInfo(
            category="wait_give_up",
            title="等待模型首字响应超时",
            reason="在规定等待上限内仍未收到模型服务商推送的首个字符（超大载荷或深度思考推理较长）。",
            suggestion="可重新发起提问，或尝试缩小发送的代码/上下文范围；若持续超时，请检查服务商 API 是否卡顿。",
            raw_message=raw_str,
        )

    # 兜底：未知异常
    return LLMErrorInfo(
        category="unknown_error",
        title="模型请求异常",
        reason="调用大模型接口时遇到了未知异常，可能是服务商未定义的临时报错。",
        suggestion="请检查网络和 API 配置，若问题持续存在，可复制下方【技术明细】进行排查或反馈。",
        raw_message=raw_str,
    )


def format_user_facing_error(exc: Any) -> str:
    """将异常格式化为用户终端/界面可读的高优先级中文诊断卡片。

    设计保障：
    1. 如果输入已经是星瑶内部的中文友好提示（如“已被用户取消”、“无进展熔断”），保持原样，绝不生硬套用模版。
    2. 提取出核心错误分类、可能原因与排查建议，帮助用户迅速定位是 Key 错误、欠费、限流还是超窗。
    3. 保留脱敏后的技术原讯（截断至安全长度），兼顾小白友好度与资深开发者排查需求。
    """
    if exc is None:
        return "未知错误"

    raw_str = sanitize_error_text(str(exc)).strip()
    if not raw_str:
        return "未知错误"

    # 防御：已包含完整中文提示或已经是短中文业务通知，原样透传
    if _is_mostly_chinese(raw_str) and not any(
        p in raw_str for p in ("litellm", "OpenAIException", "AnthropicError", "Error code:", "status_code")
    ):
        return raw_str

    info = map_llm_error(exc)

    # 截取精简的原始错误摘要（去除换行、多余空格），限长 300 字符
    concise_raw = " ".join(info.raw_message.split())
    if len(concise_raw) > 300:
        concise_raw = concise_raw[:200] + " ... " + concise_raw[-80:]

    lines = [
        f"【错误诊断】{info.title}",
        f"【可能原因】{info.reason}",
        f"【排查建议】{info.suggestion}",
    ]
    if concise_raw:
        lines.append(f"【技术明细】{concise_raw}")

    return "\n".join(lines)

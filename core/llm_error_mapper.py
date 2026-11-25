from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class LLMErrorInfo:
    """结构化大模型报错信息。"""

    category: str
    title: str
    reason: str
    suggestion: str
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
    code = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    if isinstance(code, int) and 100 <= code <= 599:
        return code
    if isinstance(code, str) and code.isdigit() and 100 <= int(code) <= 599:
        return int(code)

    resp = getattr(exc, "response", None)
    if resp is not None:
        resp_code = getattr(resp, "status_code", None)
        if isinstance(resp_code, int) and 100 <= resp_code <= 599:
            return resp_code

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


def _looks_like_socket_would_block(text: str) -> bool:
    """Windows WSAEWOULDBLOCK：套接字这一下没有数据，不是连接被拒绝。"""
    lowered = (text or "").lower()
    return (
        "10035" in (text or "")
        or "非阻止性套接字" in (text or "")
        or "非阻塞套接字" in (text or "")
        or "non-blocking socket" in lowered
        or "ewouldblock" in lowered
        or "wsaewouldblock" in lowered
    )


def _is_mostly_chinese(text: str) -> bool:
    if not text:
        return False
    clean = re.sub(r"\s+", "", text)
    if not clean:
        return False
    chinese_chars = len(re.findall(r"[\u4e00-\u9fff]", clean))
    return (chinese_chars / len(clean)) >= 0.45


# 上游会把"模型不存在/不被支持"塞进 401 的错误结构（例如 OpenCode Zen 免费档对
# 已下线模型回 {"type":"ModelError","message":"Model xxx is not supported"}）。
# 这类报文与凭证无关，必须先于鉴权分类识别，否则会把用户带向"去查 API Key"的
# 错误方向（实测案例：模型下线后用户按提示反复检查 Key，问题依旧）。
_RE_MODEL_UNSUPPORTED = re.compile(
    r"(?:modelerror"
    r"|model_not_supported"
    r"|unsupported_model"
    r"|model_not_available"
    r"|model[^,;{}\n]{0,80}?\bnot\s+supported"
    r"|模型不支持|不支持的模型|模型已下线)",
    re.IGNORECASE,
)


def _looks_like_unsupported_model(text: str) -> bool:
    return bool(_RE_MODEL_UNSUPPORTED.search(text or ""))


# 中转网关（new-api / one-api 系）的内容过滤拦截。网关在把请求转发给上游之前
# 检查请求文本，命中后台配置的关键词规则就直接回 HTTP 500，报文形如
#   {"error":{"message":"user sensitive words detected: <词>",
#             "type":"new_api_error","code":"sensitive_words_detected"}}
# 新版请求过滤插件则回 {"code":"request_filtered","ruleId":"..."}。
# 这类 500 与“服务商内部崩溃”无关，且上下文不变就必然复现，必须单独分类，
# 否则会被引向“等几分钟重试 / 看 status.openai.com”的死路。
_GATEWAY_FILTER_MARKERS = (
    "sensitive_words_detected",
    "sensitive words detected",
    "request_filtered",
    # 网关汉化版提示：同样属于确定性拒绝，不能按“临时故障”放行。
    "命中敏感词",
    "敏感词拦截",
)

_RE_GATEWAY_FILTER_HIT = re.compile(
    r"(?:sensitive words detected|敏感词)[:：]\s*([^\s\"',;)\]}\n]+)",
    re.IGNORECASE,
)


def looks_like_gateway_content_filter(text: Any) -> bool:
    """报文是否来自中转网关的内容过滤拦截（而不是上游模型故障）。"""
    lowered = str(text or "").lower()
    return any(marker in lowered for marker in _GATEWAY_FILTER_MARKERS)


def gateway_filter_hit_words(text: Any) -> list[str]:
    """网关回显的命中词，按出现顺序去重。"""
    words: list[str] = []
    for match in _RE_GATEWAY_FILTER_HIT.finditer(str(text or "")):
        word = match.group(1).strip()
        if word and word not in words:
            words.append(word)
    return words


def mask_gateway_filter_words(text: Any, words: list[str]) -> str:
    """把命中词在原报文里打码。

    错误卡片本身会被写回会话历史，若原样保留触发词，下一轮请求又会命中同一条
    规则（自锁：越诊断越报错，整个会话废掉）。保留首尾各两个字符，既能认出是
    哪个词，又不再构成完整匹配。
    """
    masked = str(text or "")
    for word in words:
        if not word:
            continue
        keep = 2 if len(word) >= 6 else 1
        token = f"{word[:keep]}***{word[-keep:]}" if len(word) > keep * 2 else "***"
        masked = re.sub(re.escape(word), token, masked, flags=re.IGNORECASE)
    return masked


def map_llm_error(exc: Any) -> LLMErrorInfo:
    """将大模型报错或异常分类映射为结构化的中文错误信息。"""
    raw_str = sanitize_error_text(str(exc or ""))
    exc_type = type(exc).__name__.lower() if isinstance(exc, Exception) else ""
    text_lower = f"{exc_type} {raw_str}".lower()
    status_code = _extract_status_code(exc, raw_str)

    # 网关内容过滤必须先于 500 判定：它是确定性的拒绝，不是服务商临时故障。
    if looks_like_gateway_content_filter(raw_str):
        hit_words = gateway_filter_hit_words(raw_str)
        hit_hint = ""
        if hit_words:
            hit_hint = "命中词：" + "、".join(
                mask_gateway_filter_words(word, [word]) for word in hit_words
            ) + "。"
        return LLMErrorInfo(
            category="gateway_content_filtered",
            title="请求被中转网关内容过滤拦截",
            reason=(
                "中转网关（new-api / one-api 系）在把请求转发给模型之前检查了请求文本，"
                "命中后台配置的关键词规则后直接拒绝并回了 HTTP 500；上游模型从未收到这次请求。"
                + hit_hint
            ),
            suggestion=(
                "这不是服务商故障，等待重试不会恢复：报错回显的触发词已经留在会话上下文里，"
                "每轮请求都会重新带上它，越问越拦。请清空当前会话或新开会话重新提问，"
                "不要把这整段报错原文再贴回输入框；若网关是自建的，到后台「请求过滤 / 敏感词」"
                "里删掉对应词条即可根治。"
            ),
            raw_message=mask_gateway_filter_words(raw_str, hit_words),
        )

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
            # 额度耗尽的 429 与 RPM 限流的 429 骨架相同，但前者是确定性失败：
            # 只有归到本分类才会 fail-fast 并触发多 Key 故障转移，被误判成
            # rate_limit 的结果是在同一个空 Key 上退避重试到失败。
            # Kimi for Coding 订阅额度用尽：{"type":"exceeded_current_quota_error",
            # "message":"Your organization's Extra Usage balance has run out..."}；
            # "Insufficient Balance" 是 DeepSeek 等厂商的措辞。
            "exceeded_current_quota",
            "extra usage balance",
            "balance has run out",
            "insufficient balance",
            "账户额度不足",
            "余额不足",
            "额度已耗尽",
            "余额已耗尽",
            "欠费",
        )
    ):
        return LLMErrorInfo(
            category="insufficient_quota",
            title="账户额度耗尽 / 余额不足",
            reason="当前大模型服务商账户的 API 调用额度已耗尽，或已触达账户设置的消费硬上限。",
            suggestion=(
                "请登录对应的模型服务商官网（如 OpenAI / DeepSeek / Moonshot 等）检查账户余额并充值，"
                "或更新为有可用额度的 API Key；若该厂商已配置多个 Key，请确认有额度的 Key 也在列表里，"
                "故障转移会自动切到它。"
            ),
            raw_message=raw_str,
        )

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
        if _looks_like_unsupported_model(text_lower):
            return LLMErrorInfo(
                category="model_unavailable",
                title="模型不可用 (上游不支持该模型)",
                reason=(
                    "该厂商/网关不认识当前请求的模型名称：它可能已下线、已改名，或不属于当前渠道"
                    "（例如免费通道轮换了可用模型）。这与 API Key 是否正确无关。"
                ),
                suggestion=(
                    "请在「设置 → 模型管理」中重新选择该厂商当前可用的模型；若使用会轮换模型的"
                    "免费通道，请以模型列表里的最新名称为准。"
                ),
                raw_message=raw_str,
            )
        return LLMErrorInfo(
            category="auth_invalid",
            title="API 凭证无效 (401 Unauthorized)",
            reason="当前配置的 API Key 错误、已过期、已被服务商撤销，或无权调用当前指定的模型。",
            suggestion="请在「设置 → 模型管理」或 config.json 中核对并重新填写该厂商正确的 API Key，注意检查首尾是否有空格。",
            raw_message=raw_str,
        )

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

    if _looks_like_socket_would_block(text_lower) or _looks_like_socket_would_block(raw_str):
        return LLMErrorInfo(
            category="socket_would_block",
            title="模型流暂时没有数据",
            reason="读取模型流式输出时，连接这一下还没有新数据。Windows 把这次等待报成了套接字错误，模型并没有拒绝请求。",
            suggestion="点「继续任务」即可接着做。若同一会话仍反复中断，检查到模型接口的网络是否稳定。",
            raw_message=raw_str,
        )

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

    if (
        _is_mostly_chinese(raw_str)
        and not _looks_like_socket_would_block(raw_str)
        # 网关过滤提示必须进 map 打码，否则中文报文会原样透出触发词。
        and not looks_like_gateway_content_filter(raw_str)
        and not any(
            p in raw_str for p in ("litellm", "OpenAIException", "AnthropicError", "Error code:", "status_code")
        )
    ):
        return raw_str

    info = map_llm_error(exc)

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

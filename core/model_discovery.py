"""厂商可用模型列表拉取（CLI /models 与 GUI 模型设置共用）。

与对话请求链路完全独立：一次性短超时 httpx 请求，不触碰共享连接池。
只做读取，返回归一化后的模型条目，不写任何配置。
"""

from __future__ import annotations

import re
from typing import Any

# 拉列表的超时（秒）：/models 是轻量元数据接口，不需要对话链路那样的长超时。
MODEL_LIST_TIMEOUT = 15.0

# 版本段：v1 / v4 / v1beta / v2.0 …（OpenAI 惯例把版本写进 Base URL 末尾）
_VERSION_SEGMENT = re.compile(r"^v\d+[0-9a-z.]*$", re.IGNORECASE)

# 上下文 / 最大输出 / 多模态元数据字段名因厂商而异，逐个候选键提取：
# - 上下文：阶跃 max_input_tokens、OpenRouter/Together context_length、
#   Groq context_window、vLLM max_model_len
# - 最大输出：OpenRouter max_completion_tokens、通用 max_output_tokens
# - 多模态：阶跃 enable_vision_input、通用 supports_vision、
#   OpenRouter architecture.input_modalities 含 "image"
_CONTEXT_KEYS = (
    "max_input_tokens", "context_length", "context_window",
    "max_model_len", "max_context_length",
)
_OUTPUT_KEYS = ("max_completion_tokens", "max_output_tokens")
_VISION_BOOL_KEYS = ("enable_vision_input", "supports_vision", "supports_image_input")

# 明确标注为语音/图像/embedding 的条目在列表里排到末尾，对话模型靠前
_NON_CHAT_KEYWORDS = (
    "语音", "图像", "音频", "文生图", "embedding", "rerank", "tts", "asr",
)


def models_endpoint(base_url: str) -> str:
    """把厂商 Base URL 转成模型列表端点。

    Base URL 可能已含版本段（OpenAI 惯例 …/v1、智谱 …/paas/v4、阶跃 …/step_plan/v1），
    也可能只是裸端点（如 …/step_plan）：前者直接拼 /models，后者补 /v1/models，
    绝不重复拼版本段（曾出现 /v1/v1/models 直接 404）。
    """
    base = str(base_url or "").strip().rstrip("/")
    if not base:
        raise ValueError("Base URL 为空")
    if _VERSION_SEGMENT.match(base.rsplit("/", 1)[-1]):
        return base + "/models"
    return base + "/v1/models"


def _alternate_endpoint(base_url: str) -> str:
    """同一 Base URL 的另一种端点拼法，用于首个地址 404 时兜底重试。"""
    base = str(base_url or "").strip().rstrip("/")
    if not base:
        return ""
    if _VERSION_SEGMENT.match(base.rsplit("/", 1)[-1]):
        # 已含版本段 → 备用拼法是「裸端点 + /v1/models」
        parent = base.rsplit("/", 1)[0]
        return parent + "/v1/models" if parent else base + "/v1/models"
    # 裸端点 → 备用拼法是「裸端点 + /models」
    return base + "/models"


def _normalize_keys(api_keys) -> list[str]:
    """把 api_keys 归一化为「去空 + 保序去重」的尝试序列；无 Key 时返回 [""]。

    字符串按厂商多 Key 口径解析（换行 / 逗号 / 分号分隔）；
    空结果保留 [""]，维持"不带鉴权头单次请求"的行为。
    """
    keys: list[str] = []
    if isinstance(api_keys, str):
        from core.provider_keys import split_provider_keys

        keys = split_provider_keys(api_keys)
    elif api_keys is not None:
        try:
            keys = [str(item).strip() for item in api_keys if item]
        except TypeError:
            keys = []
    deduped = list(dict.fromkeys(key for key in keys if key))
    return deduped or [""]


def _extract_meta(entry: dict) -> tuple[Any, Any, Any]:
    """从单条 /models 响应条目里提取 (上下文, 最大输出, 是否多模态)。"""
    context_window = None
    for key in _CONTEXT_KEYS:
        try:
            value = int(entry.get(key))
        except (TypeError, ValueError):
            continue
        if value > 0:
            context_window = value
            break
    max_output = None
    for key in _OUTPUT_KEYS:
        try:
            value = int(entry.get(key))
        except (TypeError, ValueError):
            continue
        if value > 0:
            max_output = value
            break
    supports_vision = None
    for key in _VISION_BOOL_KEYS:
        if isinstance(entry.get(key), bool):
            supports_vision = entry[key]
            break
    if supports_vision is None:
        architecture = entry.get("architecture")
        if isinstance(architecture, dict):
            modalities = architecture.get("input_modalities")
            if isinstance(modalities, list) and modalities:
                supports_vision = "image" in [str(m).lower() for m in modalities]
    return context_window, max_output, supports_vision


def _sort_key(entry: dict) -> tuple:
    model_type = str(entry.get("model_type") or "").lower()
    is_non_chat = any(keyword in model_type for keyword in _NON_CHAT_KEYWORDS)
    return (1 if is_non_chat else 0, str(entry.get("id") or "").lower())


def normalize_models(items) -> list[dict]:
    """把 /models 响应的 data 数组归一化为模型条目列表（元数据缺失回填本地规格表）。"""
    try:
        from core.model_spec_table import lookup_model_spec
    except Exception:  # pragma: no cover - 规格表异常不影响列表可用性
        def lookup_model_spec(_model_id):
            return (None, None)

    models: list[dict] = []
    for item in items or []:
        if isinstance(item, dict):
            model_id = str(item.get("id") or "").strip()
            model_type = str(item.get("model_type") or "")
            context_window, max_output, supports_vision = _extract_meta(item)
        elif isinstance(item, str):
            model_id, model_type = item.strip(), ""
            context_window, max_output, supports_vision = None, None, None
        else:
            continue
        if not model_id:
            continue
        spec_context, spec_output = lookup_model_spec(model_id)
        if context_window is None:
            context_window = spec_context
        if max_output is None:
            max_output = spec_output
        models.append({
            "id": model_id,
            "model_type": model_type,
            "context_window": context_window,
            "supports_vision": supports_vision,
            "max_tokens": max_output,
        })
    models.sort(key=_sort_key)
    return models


def _parse_response(response, base_url: str) -> dict:
    try:
        data = response.json()
    except ValueError:
        return {"ok": False, "msg": "响应不是有效 JSON（该端点可能不支持 /v1/models）"}
    items = data.get("data") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return {"ok": False, "msg": "响应格式无法解析（非 OpenAI /v1/models 形状）"}
    models = normalize_models(items)
    return {
        "ok": True,
        "models": models,
        "count": len(models),
        "base_url": base_url,
    }


def fetch_model_list(base_url: str, api_keys=None, *, timeout: float = MODEL_LIST_TIMEOUT) -> dict:
    """按厂商端点拉取可用模型列表（GET {base_url}/models 或 /v1/models）。

    多 Key：逐 Key 尝试，任一通过鉴权即采用其结果；绝不把多行串整体塞进
    Authorization / x-api-key。鉴权头同时携带 Bearer 与 x-api-key——OpenAI 网关
    认 Bearer，Anthropic 兼容端点认 x-api-key（实测阶跃 /v1/models 单发 x-api-key
    会 401）。首个地址 404 时换另一种版本段拼法重试一次。

    返回 {"ok": True, "models": [...], "count": n, "base_url": ...}
    或 {"ok": False, "msg": "..."}；任何异常都转成 ok=False，不向上抛。
    """
    try:
        base = str(base_url or "").strip().rstrip("/")
        primary = models_endpoint(base)
    except ValueError as exc:
        return {"ok": False, "msg": str(exc)}

    keys = _normalize_keys(api_keys)
    fallback = _alternate_endpoint(base)
    candidates = [primary] + ([fallback] if fallback and fallback != primary else [])

    try:
        import httpx
        from core.llm_policy import tokenrhythm_agent_headers
    except Exception as exc:  # pragma: no cover - 依赖缺失时给出可读错误
        return {"ok": False, "msg": f"无法发起请求: {exc}"}

    last_msg = ""
    for api_key in keys:
        for url in candidates:
            headers = {"accept": "application/json"}
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"
                headers["x-api-key"] = api_key
            try:
                headers.update(tokenrhythm_agent_headers(url))
            except Exception:
                pass
            try:
                response = httpx.get(url, headers=headers, timeout=timeout)
            except Exception as exc:
                # 网络层不可达：换 Key / 换端点都无意义，直接返回
                return {"ok": False, "msg": f"无法连接 {base}: {exc}"}
            if response.status_code == 200:
                return _parse_response(response, base_url)
            body = (getattr(response, "text", "") or "").strip()[:200]
            last_msg = f"获取失败 (HTTP {response.status_code}): {body}"
            if response.status_code != 404:
                # 鉴权/限流等错误换拼法也不会好，直接换下一个 Key
                break
    if len(keys) > 1:
        return {"ok": False, "msg": f"{len(keys)} 个 Key 均获取失败，最后错误：{last_msg}"}
    return {"ok": False, "msg": last_msg or "获取失败"}

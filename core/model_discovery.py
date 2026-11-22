from __future__ import annotations

import re
from typing import Any

MODEL_LIST_TIMEOUT = 15.0

_VERSION_SEGMENT = re.compile(r"^v\d+[0-9a-z.]*$", re.IGNORECASE)

_CONTEXT_KEYS = (
    "max_input_tokens", "context_length", "context_window",
    "max_model_len", "max_context_length",
)
_OUTPUT_KEYS = ("max_completion_tokens", "max_output_tokens")
_VISION_BOOL_KEYS = ("enable_vision_input", "supports_vision", "supports_image_input")

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
        parent = base.rsplit("/", 1)[0]
        return parent + "/v1/models" if parent else base + "/v1/models"
    return base + "/models"


def _normalize_keys(api_keys) -> list[str]:
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
                break
    if len(keys) > 1:
        return {"ok": False, "msg": f"{len(keys)} 个 Key 均获取失败，最后错误：{last_msg}"}
    return {"ok": False, "msg": last_msg or "获取失败"}

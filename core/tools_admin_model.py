"""core/tools_admin_model.py
Agent 工具 model_manage：模型与提供商管理。

覆盖能力：提供商增删改查、预置提供商添加、模型增删改查、激活模型切换、
思考档位设置、连通性测试（真实最小请求）、拉取提供商模型列表。

设计约束：
- 配置读写一律走 core.model_manager 的公开函数，不直接改 config.json；
- 绝不 import gui_bridge / main / pywebview；
- 输出统一为 JSON 字符串（ensure_ascii=False）：成功 {"ok": true, ...}，
  失败 {"ok": false, "error": "中文原因"}；API Key 永不落进任何输出（仅报是否已配置）；
- 连通性测试与 fetch_models 都发真实网络请求，失败返回真实错误，不伪造成功。
"""
from __future__ import annotations

import json
import time
from typing import Any

from core.tool_protocol import ToolSpec
import core.model_manager as model_manager

# 工具参数白名单：调度层可能注入内部键（cancel_event 等），转发前先过滤，
# 避免把未知键传给固定签名的 model_manage 造成 TypeError。
_KNOWN_ARGUMENTS = frozenset({
    "action", "provider", "model_id", "name", "base_url", "api_key", "protocol",
    "model_name", "preset", "reasoning", "context_window",
    "supports_vision", "supports_function_calling", "computer_protocol",
})

_ACTIONS = (
    "list_providers",
    "list_models",
    "add_provider",
    "add_preset_provider",
    "update_provider",
    "delete_provider",
    "add_model",
    "update_model",
    "delete_model",
    "set_active",
    "set_reasoning",
    "test",
    "fetch_models",
)

_PROTOCOLS = ("openai", "anthropic", "responses")
_COMPUTER_PROTOCOLS = ("none", "generic", "native_openai")

# 连通性测试与模型列表拉取的超时（秒）
_NETWORK_TIMEOUT_SECONDS = 20.0


# ---------------------------------------------------------------------------
# 统一 JSON 输出
# ---------------------------------------------------------------------------
def _ok(**payload: Any) -> str:
    result = {"ok": True}
    result.update(payload)
    return json.dumps(result, ensure_ascii=False)


def _fail(error: str, **payload: Any) -> str:
    result = {"ok": False, "error": str(error)}
    result.update(payload)
    return json.dumps(result, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 配置查找与脱敏辅助
# ---------------------------------------------------------------------------
def _api_key_configured(provider: dict) -> bool:
    """只报「是否已配置」，绝不返回 Key 内容（含多 Key 原文）。"""
    return bool(str(provider.get("api_key") or "").strip())


def _resolve_provider(config: dict, identifier: str) -> dict | None:
    """按 provider id 精确匹配，其次按名称精确匹配。"""
    target = str(identifier or "").strip()
    if not target:
        return None
    providers = config.get("providers") or []
    for provider in providers:
        if str(provider.get("id") or "") == target:
            return provider
    for provider in providers:
        if str(provider.get("name") or "").strip() == target:
            return provider
    return None


def _find_model(config: dict, model_id: str) -> tuple[dict | None, dict | None]:
    """返回 (provider, model)；未找到返回 (None, None)。"""
    target = str(model_id or "").strip()
    if not target:
        return None, None
    for provider in config.get("providers") or []:
        for model in provider.get("models") or []:
            if str(model.get("id") or "") == target:
                return provider, model
    return None, None


def _provider_summary(provider: dict) -> dict:
    """提供商摘要（脱敏）：不含 api_key 原文。"""
    models = []
    for model in provider.get("models") or []:
        models.append({
            "model_id": model.get("id"),
            "name": model.get("name"),
            "route": model.get("litellm_model"),
            "context_window": model.get("context_window"),
            "supports_vision": model.get("supports_vision"),
            "supports_function_calling": model.get("supports_function_calling"),
        })
    return {
        "provider": provider.get("id"),
        "name": provider.get("name"),
        "base_url": provider.get("base_url"),
        "protocol": provider.get("protocol", "openai"),
        "preset_id": provider.get("preset_id"),
        "api_key_configured": _api_key_configured(provider),
        "model_count": len(models),
        "models": models,
    }


def _reasoning_choice_or_none(model_id: str):
    try:
        return model_manager.get_model_reasoning_choice(model_id)
    except Exception:
        return None


def _build_models_url(base_url: str) -> str:
    """按厂商惯例拼 /models：已带 /v1 版本段则直拼，裸端点补 /v1/models。

    绝不重复拼出版本段（否则会出现 /v1/v1/models 404）。
    """
    base = str(base_url or "").strip().rstrip("/")
    if not base:
        return ""
    if base.endswith("/v1") or base.endswith("/v4") or base.endswith("/v3"):
        return base + "/models"
    return base + "/v1/models"


# ---------------------------------------------------------------------------
# 各 action 实现
# ---------------------------------------------------------------------------
def _action_list_providers() -> str:
    config = model_manager.load_config()
    providers = [_provider_summary(p) for p in (config.get("providers") or [])]
    return _ok(
        count=len(providers),
        active_model_id=config.get("active_model_id"),
        providers=providers,
        note="API Key 仅显示是否已配置，不回传内容。",
    )


def _action_list_models() -> str:
    config = model_manager.load_config()
    active_id = str(config.get("active_model_id") or "")
    models = []
    for provider in config.get("providers") or []:
        for model in provider.get("models") or []:
            model_id = str(model.get("id") or "")
            models.append({
                "model_id": model_id,
                "name": model.get("name"),
                "route": model.get("litellm_model"),
                "provider_id": provider.get("id"),
                "provider_name": provider.get("name"),
                "protocol": provider.get("protocol", "openai"),
                "context_window": model.get("context_window"),
                "supports_vision": model.get("supports_vision"),
                "supports_function_calling": model.get("supports_function_calling"),
                "computer_protocol": model.get("computer_protocol"),
                "reasoning_choice": _reasoning_choice_or_none(model_id),
                "active": model_id == active_id,
                "api_key_configured": _api_key_configured(provider),
            })
    active = next((item for item in models if item["active"]), None)
    return _ok(count=len(models), active_model_id=active_id or None, active_model=active, models=models)


def _action_add_provider(name: str, base_url: str, api_key: str, protocol: str) -> str:
    name = str(name or "").strip()
    base_url = str(base_url or "").strip()
    if not name:
        return _fail("add_provider 需要 name（厂商名称）")
    if not base_url:
        return _fail("add_provider 需要 base_url（API 地址）")
    protocol = str(protocol or "openai").strip() or "openai"
    provider_id = model_manager.add_provider(name, base_url, str(api_key or ""), protocol)
    return _ok(provider_id=provider_id, message=f"已添加提供商 {name}", name=name,
               base_url=base_url, protocol=protocol,
               api_key_configured=bool(str(api_key or "").strip()))


def _action_add_preset_provider(preset: str, api_key: str) -> str:
    preset = str(preset or "").strip()
    if not preset:
        return _fail("add_preset_provider 需要 preset（预置厂商 id，如 deepseek/moonshot）")
    provider_id = model_manager.add_preset_provider(preset, str(api_key or ""))
    config = model_manager.load_config()
    provider = _resolve_provider(config, provider_id) or {}
    return _ok(
        provider_id=provider_id,
        preset_id=preset,
        name=provider.get("name"),
        base_url=provider.get("base_url"),
        protocol=provider.get("protocol"),
        message=f"已按预置 {preset} 添加提供商（模型列表可用 fetch_models 拉取）",
    )


def _action_update_provider(provider: str, name: str, base_url: str,
                            api_key: str, protocol: str) -> str:
    if not str(provider or "").strip():
        return _fail("update_provider 需要 provider（厂商 id 或名称）")
    config = model_manager.load_config()
    target = _resolve_provider(config, provider)
    if target is None:
        return _fail(f"未找到提供商: {provider}")
    new_name = str(name or "").strip() or str(target.get("name") or "")
    new_base_url = str(base_url or "").strip() or str(target.get("base_url") or "")
    new_protocol = str(protocol or "").strip() or str(target.get("protocol") or "openai")
    model_manager.update_provider(
        str(target.get("id")),
        name=new_name,
        base_url=new_base_url,
        api_key=str(api_key or ""),  # 空串 = 保留原 Key
        protocol=new_protocol,
    )
    return _ok(
        provider_id=target.get("id"),
        name=new_name,
        base_url=new_base_url,
        protocol=new_protocol,
        api_key_updated=bool(str(api_key or "").strip()),
        message="提供商已更新" + ("" if str(api_key or "").strip() else "（API Key 保持原值）"),
    )


def _action_delete_provider(provider: str) -> str:
    if not str(provider or "").strip():
        return _fail("delete_provider 需要 provider（厂商 id 或名称）")
    config = model_manager.load_config()
    target = _resolve_provider(config, provider)
    if target is None:
        return _fail(f"未找到提供商: {provider}")
    provider_id = str(target.get("id"))
    model_count = len(target.get("models") or [])
    model_manager.delete_provider(provider_id)
    return _ok(provider_id=provider_id, name=target.get("name"),
               deleted_model_count=model_count,
               message=f"已删除提供商 {target.get('name') or provider_id} 及其 {model_count} 个模型")


def _action_add_model(provider: str, model_name: str, name: str,
                      context_window: int, supports_vision: bool | None,
                      supports_function_calling: bool | None,
                      computer_protocol: str) -> str:
    if not str(provider or "").strip():
        return _fail("add_model 需要 provider（厂商 id 或名称）")
    resolved_name = str(model_name or "").strip() or str(name or "").strip()
    if not resolved_name:
        return _fail("add_model 需要 model_name（模型名称）")
    config = model_manager.load_config()
    target = _resolve_provider(config, provider)
    if target is None:
        return _fail(f"未找到提供商: {provider}")
    window = int(context_window or 0)
    if window <= 0:
        window = 128000  # 未指定时按通用默认，避免 model_manager 直接拒绝
    proto = str(computer_protocol or "").strip() or None
    model_id = model_manager.add_model_to_provider(
        str(target.get("id")),
        resolved_name,
        window,
        supports_vision=supports_vision,
        supports_function_calling=supports_function_calling,
        computer_protocol=proto,
    )
    return _ok(model_id=model_id, provider_id=target.get("id"),
               name=resolved_name, context_window=window,
               message=f"已添加模型 {resolved_name}")


def _action_update_model(model_id: str, name: str, model_name: str,
                         context_window: int, supports_vision: bool | None,
                         supports_function_calling: bool | None,
                         computer_protocol: str) -> str:
    if not str(model_id or "").strip():
        return _fail("update_model 需要 model_id")
    config = model_manager.load_config()
    provider, model = _find_model(config, model_id)
    if provider is None or model is None:
        return _fail(f"未找到模型: {model_id}")
    new_name = (str(name or "").strip() or str(model_name or "").strip()
                or str(model.get("name") or ""))
    window = int(context_window or 0)
    if window <= 0:
        window = int(model.get("context_window") or 128000)
    vision = supports_vision
    if vision is None:
        vision = model.get("supports_vision")
    if vision is None:
        vision = False
    proto = str(computer_protocol or "").strip() or None
    model_manager.update_model(
        str(provider.get("id")),
        str(model.get("id")),
        name=new_name,
        context_window=window,
        supports_vision=bool(vision),
        supports_function_calling=supports_function_calling,
        computer_protocol=proto,
    )
    return _ok(model_id=model.get("id"), provider_id=provider.get("id"),
               name=new_name, context_window=window, supports_vision=bool(vision),
               message=f"模型 {new_name} 已更新")


def _action_delete_model(model_id: str) -> str:
    if not str(model_id or "").strip():
        return _fail("delete_model 需要 model_id")
    config = model_manager.load_config()
    provider, model = _find_model(config, model_id)
    if provider is None or model is None:
        return _fail(f"未找到模型: {model_id}")
    model_manager.delete_model(str(model.get("id")))
    return _ok(model_id=model.get("id"), name=model.get("name"),
               provider_id=provider.get("id"),
               message=f"已删除模型 {model.get('name') or model_id}")


def _action_set_active(model_id: str) -> str:
    if not str(model_id or "").strip():
        return _fail("set_active 需要 model_id")
    config = model_manager.load_config()
    _provider, model = _find_model(config, model_id)
    if model is None:
        return _fail(f"未找到模型: {model_id}")
    model_manager.set_active_model(str(model.get("id")))
    active = model_manager.get_active_model()
    return _ok(
        active_model_id=model.get("id"),
        name=model.get("name"),
        route=model.get("litellm_model"),
        # 只回传非敏感字段，get_active_model 的 api_key 一律不输出
        provider_name=(active or {}).get("provider_name"),
        protocol=(active or {}).get("protocol"),
        context_window=(active or {}).get("context_window"),
        message=f"已激活模型 {model.get('name') or model_id}",
    )


def _action_set_reasoning(model_id: str, reasoning: str) -> str:
    if not str(model_id or "").strip():
        return _fail("set_reasoning 需要 model_id")
    choice = str(reasoning or "").strip()
    if not choice:
        return _fail("set_reasoning 需要 reasoning（思考档位，如 default/off/on/low/medium/high）")
    ok, message = model_manager.set_model_reasoning_choice(str(model_id), choice)
    if not ok:
        return _fail(message)
    return _ok(model_id=model_id, reasoning=model_manager.get_model_reasoning_choice(str(model_id)),
               message=message)


def _action_test(model_id: str) -> str:
    """发一次最小真实 chat 请求验证连通性；失败回传真实错误，不伪装成功。"""
    if not str(model_id or "").strip():
        return _fail("test 需要 model_id")
    config = model_manager.load_config()
    provider, model = _find_model(config, model_id)
    if provider is None or model is None:
        return _fail(f"未找到模型: {model_id}")
    route = str(model.get("litellm_model") or "").strip()
    if not route:
        return _fail(f"模型 {model_id} 缺少路由名（litellm_model）")
    base_url = str(provider.get("base_url") or "").strip()
    if not base_url:
        return _fail(f"提供商 {provider.get('name')} 未配置 Base URL")
    api_key = model_manager.effective_provider_api_key(provider)
    if not str(api_key or "").strip():
        return _fail(f"提供商 {provider.get('name')} 未配置 API Key，无法测试连通性")

    # 复用项目原生 HTTP 传输（core.llm_native_transport）：非流式、最小载荷、20 秒超时。
    from core.llm_native_transport import native_completion

    started = time.perf_counter()
    try:
        response = native_completion(
            model=route,
            api_key=api_key,
            api_base=base_url,
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=8,
            stream=False,
            timeout=_NETWORK_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        status = getattr(exc, "status_code", None)
        detail = f"HTTP {status}: {exc}" if status else str(exc)
        return _fail(f"连通性测试失败（{provider.get('name')}/{model.get('name')}）：{detail}")
    elapsed_ms = int((time.perf_counter() - started) * 1000)

    reply = ""
    try:
        if response.choices:
            reply = str(response.choices[0].message.content or "")
    except Exception:
        reply = ""
    usage = {}
    try:
        raw_usage = getattr(response, "usage", None)
        if isinstance(raw_usage, dict):
            usage = {k: v for k, v in raw_usage.items()
                     if k in ("prompt_tokens", "completion_tokens", "total_tokens")}
    except Exception:
        usage = {}

    return _ok(
        model_id=model.get("id"),
        name=model.get("name"),
        route=route,
        provider_name=provider.get("name"),
        latency_ms=elapsed_ms,
        response_model=getattr(response, "model", None),
        reply_preview=reply[:80],
        usage=usage,
        message="连通性测试通过",
    )


def _action_fetch_models(provider: str) -> str:
    """GET {base_url}/models（或 /v1/models）拉取可用模型 id 列表。"""
    if not str(provider or "").strip():
        return _fail("fetch_models 需要 provider（厂商 id 或名称）")
    config = model_manager.load_config()
    target = _resolve_provider(config, provider)
    if target is None:
        return _fail(f"未找到提供商: {provider}")
    base_url = str(target.get("base_url") or "").strip()
    if not base_url:
        return _fail(f"提供商 {target.get('name')} 未配置 Base URL")
    url = _build_models_url(base_url)
    api_key = str(model_manager.effective_provider_api_key(target) or "").strip()
    headers = {"accept": "application/json"}
    if api_key:
        # 同时带 Bearer 与 x-api-key：OpenAI 网关认前者，Anthropic 兼容端点认后者
        headers["Authorization"] = f"Bearer {api_key}"
        headers["x-api-key"] = api_key

    import httpx

    try:
        response = httpx.get(url, headers=headers, timeout=_NETWORK_TIMEOUT_SECONDS)
    except Exception as exc:
        return _fail(f"拉取模型列表失败（无法连接 {url}）：{exc}")
    if response.status_code != 200:
        body = (response.text or "").strip()[:300]
        return _fail(f"拉取模型列表失败（HTTP {response.status_code}）：{body}")
    try:
        data = response.json()
    except ValueError:
        return _fail("拉取模型列表失败：响应不是有效 JSON（该端点可能不支持 /models）")

    items = data.get("data") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return _fail("拉取模型列表失败：响应格式非 OpenAI /models 形状")

    model_ids: list[str] = []
    for item in items:
        if isinstance(item, str):
            model_ids.append(item)
        elif isinstance(item, dict):
            identifier = item.get("id") or item.get("name") or item.get("model")
            if identifier:
                model_ids.append(str(identifier))
    # 去重保序
    seen: set[str] = set()
    unique_ids = [mid for mid in model_ids if not (mid in seen or seen.add(mid))]
    return _ok(
        provider_id=target.get("id"),
        provider_name=target.get("name"),
        url=url,
        count=len(unique_ids),
        models=unique_ids,
    )


# ---------------------------------------------------------------------------
# 对外工具函数
# ---------------------------------------------------------------------------
def model_manage(action: str, provider: str = "", model_id: str = "", name: str = "",
                 base_url: str = "", api_key: str = "", protocol: str = "",
                 model_name: str = "", preset: str = "", reasoning: str = "",
                 context_window: int = 0, supports_vision: bool | None = None,
                 supports_function_calling: bool | None = None,
                 computer_protocol: str = "") -> str:
    """模型与提供商管理统一入口，返回 JSON 字符串。"""
    try:
        action = str(action or "").strip()
        if not action:
            return _fail("action 不能为空，可选：" + "/".join(_ACTIONS))
        if action == "list_providers":
            return _action_list_providers()
        if action == "list_models":
            return _action_list_models()
        if action == "add_provider":
            return _action_add_provider(name, base_url, api_key, protocol)
        if action == "add_preset_provider":
            return _action_add_preset_provider(preset, api_key)
        if action == "update_provider":
            return _action_update_provider(provider, name, base_url, api_key, protocol)
        if action == "delete_provider":
            return _action_delete_provider(provider)
        if action == "add_model":
            return _action_add_model(provider, model_name, name, context_window,
                                     supports_vision, supports_function_calling,
                                     computer_protocol)
        if action == "update_model":
            return _action_update_model(model_id, name, model_name, context_window,
                                        supports_vision, supports_function_calling,
                                        computer_protocol)
        if action == "delete_model":
            return _action_delete_model(model_id)
        if action == "set_active":
            return _action_set_active(model_id)
        if action == "set_reasoning":
            return _action_set_reasoning(model_id, reasoning)
        if action == "test":
            return _action_test(model_id)
        if action == "fetch_models":
            return _action_fetch_models(provider)
        return _fail(f"不支持的 action: {action}，可选：" + "/".join(_ACTIONS))
    except ValueError as exc:
        return _fail(str(exc) or "参数校验失败")
    except KeyError as exc:
        return _fail(f"配置中不存在对应条目: {exc}")
    except Exception as exc:  # 统一兜底：绝不把异常抛给调度层
        return _fail(f"model_manage 执行失败: {exc}")


def _execute(**arguments: Any) -> str:
    """ToolSpec 执行入口：过滤调度层注入的内部键后转调 model_manage。"""
    clean = {key: value for key, value in arguments.items() if key in _KNOWN_ARGUMENTS}
    return model_manage(**clean)


_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": list(_ACTIONS),
            "description": (
                "操作类型：list_providers 列出提供商与模型；list_models 列出全部模型及当前激活模型；"
                "add_provider 新增提供商；add_preset_provider 按预置新增提供商；"
                "update_provider 修改提供商；delete_provider 删除提供商；"
                "add_model 新增模型；update_model 修改模型；delete_model 删除模型；"
                "set_active 设为激活模型；set_reasoning 设置思考档位；"
                "test 发真实最小请求测试连通性；fetch_models 拉取提供商模型列表"
            ),
        },
        "provider": {"type": "string", "description": "提供商标识（provider id 或名称），update/delete/fetch_models/add_model 必填"},
        "model_id": {"type": "string", "description": "模型 id（update_model/delete_model/set_active/set_reasoning/test 必填）"},
        "name": {"type": "string", "description": "名称：add_provider 时为厂商名称，add_model/update_model 时可作为模型名称"},
        "base_url": {"type": "string", "description": "API 地址（add_provider 必填，update_provider 可选）"},
        "api_key": {"type": "string", "description": "API Key（add_provider/add_preset_provider 使用；update_provider 留空表示保留原 Key）"},
        "protocol": {"type": "string", "enum": list(_PROTOCOLS), "description": "API 协议，默认 openai"},
        "model_name": {"type": "string", "description": "模型名称（add_model 必填，update_model 时可改名）"},
        "preset": {"type": "string", "description": "预置厂商 id（如 deepseek、moonshot、kimi-coding-openai）"},
        "reasoning": {"type": "string", "description": "思考档位，可选值随模型能力而定（default/off/on/low/medium/high/xhigh/max）"},
        "context_window": {"type": "integer", "minimum": 1, "description": "上下文窗口（token）；不传或 0 表示沿用默认/原值"},
        "supports_vision": {"type": "boolean", "description": "是否支持视觉输入"},
        "supports_function_calling": {"type": "boolean", "description": "是否支持函数调用（工具）"},
        "computer_protocol": {"type": "string", "enum": list(_COMPUTER_PROTOCOLS), "description": "电脑操控协议；generic 要求声明支持视觉且未关闭函数调用"},
    },
    "required": ["action"],
}


def build_specs() -> list[ToolSpec]:
    """返回 model_manage 的工具规格（单条）。"""
    return [
        ToolSpec(
            name="model_manage",
            description=(
                "管理与查询大模型提供商与模型配置（读写本地 config.json）。"
                "支持：列出提供商/模型、新增或修改、删除提供商、按预置新增提供商、"
                "新增/修改/删除模型、切换激活模型、设置思考档位、"
                "用真实最小请求测试某个模型连通性、拉取某提供商的可用模型列表。"
                "API Key 只报是否已配置，绝不回显内容。"
            ),
            input_schema=_INPUT_SCHEMA,
            execute=_execute,
            read_only=False,
            requires_permission=True,
            side_effect_scope="system",
            activity_description="管理模型配置",
        )
    ]

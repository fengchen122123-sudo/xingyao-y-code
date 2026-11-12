"""
core/model_manager.py
多厂商模型管理器：读写 config.json，提供 get/set 激活模型的接口。
"""
import json
import os
import uuid

import config
import core.app_context as app_context
from core.model_catalog import (
    get_preset_provider,
    provider_route_name,
    validate_protocol,
)
from core.reasoning_capabilities import get_reasoning_capability
_CONFIG_FILE = config._CONFIG_FILE

# 电脑操控能力元数据（设计 9.1）：只接受严格 bool 与固定协议集合；
# generic 组合缺视觉或工具能力时保存即拒绝，不允许落盘半有效配置。
COMPUTER_PROTOCOLS = ("none", "generic", "native_openai")


def _normalize_capability_bool(name: str, value):
    """可选布尔字段：None 表示调用方未指定；其余必须 type(value) is bool。"""
    if value is None:
        return None
    if type(value) is not bool:
        raise ValueError(f"{name} 必须是布尔值")
    return value


def _normalize_computer_protocol(value):
    """None = 调用方不触碰（保留既有值）；"" = 自动（缺省协议，由能力元数据判定）。"""
    if value is None or value == "":
        return value
    if type(value) is not str or value not in COMPUTER_PROTOCOLS:
        raise ValueError(
            f"computer_protocol 只支持 {'/'.join(COMPUTER_PROTOCOLS)}"
        )
    return value


def _validate_computer_combination(supports_vision, supports_function_calling, computer_protocol):
    """generic 要求视觉显式声明且工具调用未被显式关闭（同日修订 2026-09-05：
    工具调用未声明视为支持——Agent 里的模型本就靠 function calling 驱动；
    False 是用户主动排除的逃生门），否则保存即拒绝。"""
    if computer_protocol == "generic" and not (supports_vision and supports_function_calling is not False):
        raise ValueError(
            "computer_protocol=generic 要求声明 supports_vision，且 supports_function_calling 未被显式关闭"
        )


def load_config() -> dict:
    """加载完整配置，如果是旧格式则自动迁移。主文件损坏时优先从备份恢复。

    读取失败（主文件损坏且无可用备份）时只在内存里给出默认结构，绝不写盘：
    以前这里会顺手把默认配置落盘，导致一次瞬时读取失败就把用户的全部厂商/模型
    配置覆盖成单个默认厂商（重启后「模型配置全丢」的事故根因）。
    """
    raw = config._read_json_with_backup(_CONFIG_FILE, default=None)
    # 首次运行（文件确实不存在）允许把默认结构落盘；文件存在却读不出来属于故障，
    # 此时只在内存里用默认结构顶住，绝不回写磁盘
    read_failed = not isinstance(raw, dict) and os.path.exists(_CONFIG_FILE)
    data = raw if isinstance(raw, dict) else {}

    # 检测旧格式（只有 model/api_key/base_url 的扁平结构），自动迁移
    if "model" in data and "providers" not in data:
        try:
            old_model = data.get("model", "openai/agnes-3.0-flash")
            if isinstance(old_model, dict):
                old_model = old_model.get("litellm_model") or old_model.get("name") or "openai/agnes-3.0-flash"
            elif not isinstance(old_model, str):
                old_model = str(old_model) if old_model else "openai/agnes-3.0-flash"
            
            model_name = old_model.split("/")[-1]
            mid = str(uuid.uuid4())[:8]
            pid = str(uuid.uuid4())[:8]
            # 保留旧格式中的其他字段（theme / permission_mode 等），只替换模型相关字段
            base_url = data.pop("base_url", "")
            api_key = data.pop("api_key", "")
            data.pop("model", None)
            migrated = {
                "active_model_id": mid,
                "providers": [
                    {
                        "id": pid,
                        "name": "默认厂商",
                        "base_url": base_url,
                        "api_key": api_key,
                        "protocol": "openai",
                        "models": [
                            {
                                "id": mid,
                                "name": model_name,
                                "litellm_model": old_model,
                                "context_window": 128000,
                            }
                        ],
                    }
                ],
            }
            data.update(migrated)
            _write_config(data)
        except Exception:
            data = {}

    # 无 providers 键时的处理：
    # - 完全空配置：写入 DeepSeek 默认结构作为新手引导；
    # - 已有自定义配置（如只含 active_model_id）：补充空 providers 列表并保留
    #   用户原有字段，避免静默覆盖用户手动维护的配置。
    if "providers" not in data:
        if not data:
            data = {
                "active_model_id": "ds-v4-pro",
                "providers": [
                    {
                        "id": "deepseek-provider",
                        "name": "深度求索",
                        "base_url": "https://api.deepseek.com",
                        "api_key": "",
                        "protocol": "openai",
                        "preset_id": "deepseek",
                        "models": [
                            {
                                "id": "ds-v4-pro",
                                "name": "deepseek-v4-pro",
                                "litellm_model": "openai/deepseek-v4-pro",
                                "context_window": 1000000,
                                "supports_vision": False,
                                "enable_thinking": True,
                                "reasoning_effort": "max"
                            },
                            {
                                "id": "ds-flash",
                                "name": "deepseek-flash",
                                "litellm_model": "openai/deepseek-flash",
                                "context_window": 1000000,
                                "supports_vision": True,
                                "enable_thinking": True,
                                "reasoning_effort": "max"
                            }
                        ]
                    }
                ]
            }
            if read_failed:
                config._log_warning(
                    "config.json 读取失败且没有可用备份，本次按空配置启动；"
                    "为避免把用户模型配置覆盖成默认值，本次不会写回磁盘"
                )
            else:
                # 仅「首次运行（配置文件确实不存在）」才落盘默认结构
                _write_config(data)
        else:
            data["providers"] = []

    return data


def _write_config(data: dict):
    """原子写配置：临时文件 + os.replace + 进程内锁，防止写一半崩溃损坏配置。"""
    with config._CONFIG_LOCK:
        config._atomic_write_json(_CONFIG_FILE, data)


def effective_provider_api_key(provider: dict) -> str:
    """请求读取路径使用的单 Key：多 Key provider 返回 Key 池「当前该用」的 Key。

    设计见 docs/superpowers/specs/2026-10-12-provider-multi-key-failover-design.md
    第 4 节：所有「取 Key 去发请求 / 返回给调用方」的路径都必须与 Key 池一致，
    绝不把多行 Key 串整体当作一个 Key 使用。

    - 多 Key（换行/逗号/分号分隔）→ Key 池 initial_key()（粘性 + 冷却后轮换）；
    - 单 Key → 原样返回 provider["api_key"]；
    - Key 池不可用 / 任何异常 → 原值回退，行为与现状一致。

    注意：add/update/delete 等写入路径不得调用本函数——多行原文必须原样保存。
    """
    raw = (provider or {}).get("api_key")
    try:
        from core.provider_keys import get_key_pool

        pool = get_key_pool(provider)
        if pool is not None:
            key = pool.initial_key()
            if key:
                return key
    except Exception:
        pass
    return raw


def get_active_model(session_name: str | None = None) -> dict | None:
    """
    返回当前激活模型的完整信息字典，包含：
      litellm_model, api_key, base_url, context_window, name
    如果没有配置任何模型，返回 None。

    当传入 session_name 时，优先取该会话在 sessions 表中绑定的 active_model_id；
    未绑定则回退到全局 active_model_id。CLI/无会话调用方传 None，始终读全局配置。
    """
    active_id = None
    if session_name:
        try:
            manager = app_context.get_manager()
            if manager is not None:
                active_id = manager.get_session_model_id(session_name)
        except Exception:
            pass
    data = load_config()
    global_id = data.get("active_model_id")
    lookup = {
        model.get("id"): (provider, model)
        for provider in data.get("providers", [])
        for model in provider.get("models", [])
    }
    if active_id in lookup:
        provider, model = lookup[active_id]
    elif global_id in lookup:
        # 会话未绑定，或绑定的模型已被用户删除：回退全局默认，
        # 不能让会话被死绑定卡死（表现为"未激活任何模型"）
        provider, model = lookup[global_id]
    else:
        return None
    return {
        "litellm_model":   model["litellm_model"],
        # 多 Key provider 只返回「当前该用的单 Key」（Key 池选择结果），
        # 多行原文仍完整保存在配置里，由请求层按需切 Key
        "api_key":         effective_provider_api_key(provider),
        "base_url":         provider["base_url"],
        "context_window":  model.get("context_window", 128000),
        "supports_vision": model.get("supports_vision"),
        "supports_function_calling": model.get("supports_function_calling"),
        "computer_protocol": model.get("computer_protocol"),
        "system_prompt_extra": model.get("system_prompt_extra"),
        "name":            model["name"],
        "protocol":        provider.get("protocol", "openai"),
        "provider_name":   provider["name"],
        "id":              model["id"],
        # 成本账本：模型级可选价格块（缺省 None → 回退内置价格表 → 未计价）
        "price":           model.get("price"),
    }


def set_active_model(model_id: str):
    """将 model_id 设为激活模型并持久化。"""
    # 读-改-写整体在锁内完成，避免与 config.save_config_value 并发时互相覆盖
    def _mutate(data):
        data["active_model_id"] = model_id
    config.update_config(_mutate)


def _reasoning_model_name(model: dict) -> str:
    return str(model.get("litellm_model") or model.get("name") or "")


def _find_config_model(data: dict, model_id: str) -> dict | None:
    for provider in data.get("providers", []):
        for model in provider.get("models", []):
            if str(model.get("id")) == str(model_id):
                return model
    return None


def _reasoning_choice_from_model(model: dict) -> str:
    capability = get_reasoning_capability(_reasoning_model_name(model))
    stored = str(model.get("reasoning_choice") or "").strip().lower()
    if stored:
        if stored not in capability.options:
            raise ValueError(
                f"模型 {model.get('name') or model.get('id')} 保存了不支持的思考选项: {stored}"
            )
        return stored
    if model.get("enable_thinking") is False and "off" in capability.options:
        return "off"
    effort = str(model.get("reasoning_effort") or "").strip().lower()
    if effort:
        if effort not in capability.options:
            raise ValueError(
                f"模型 {model.get('name') or model.get('id')} 保存了不支持的思考强度: {effort}"
            )
        return effort
    if model.get("enable_thinking") is True and "on" in capability.options:
        return "on"
    return "default"


def get_model_reasoning_choice(model_id: str) -> str:
    """返回模型的通用思考选择；无覆盖时返回 default。"""
    if not model_id:
        return "default"
    model = _find_config_model(load_config(), model_id)
    return _reasoning_choice_from_model(model) if model else "default"


def set_model_reasoning_choice(model_id: str, choice: str | None) -> tuple[bool, str]:
    """严格校验并原子持久化模型思考开关与强度。"""
    normalized = str(choice or "default").strip().lower() or "default"
    current = _find_config_model(load_config(), model_id)
    if current is None:
        return False, f"未找到模型: {model_id}"

    capability = get_reasoning_capability(_reasoning_model_name(current))
    if capability.kind == "fixed":
        return False, capability.disabled_reason
    if capability.kind == "hidden":
        return False, f"模型 {current.get('name') or model_id} 未声明可调思考能力"
    if normalized not in capability.options:
        allowed = "/".join(capability.options)
        return False, f"模型 {current.get('name') or model_id} 不支持思考选项 {normalized}（可选 {allowed}）"

    changed = []
    validation_error = []

    def _mutate(data):
        model = _find_config_model(data, model_id)
        if model is None:
            return
        current_capability = get_reasoning_capability(_reasoning_model_name(model))
        if current_capability.kind == "fixed":
            validation_error.append(current_capability.disabled_reason)
            return
        if current_capability.kind == "hidden":
            validation_error.append(
                f"模型 {model.get('name') or model_id} 未声明可调思考能力"
            )
            return
        if normalized not in current_capability.options:
            allowed = "/".join(current_capability.options)
            validation_error.append(
                f"模型 {model.get('name') or model_id} 不支持思考选项 "
                f"{normalized}（可选 {allowed}）"
            )
            return
        changed.append(model.get("name") or model_id)
        # thinking_budget 属于旧通用 thinking 协议。模型专属选择启用后必须由
        # request_strategy 独占编码，否则会同时发送两套相互冲突的开关。
        model.pop("thinking_budget", None)
        if normalized == "default":
            model.pop("reasoning_choice", None)
            model.pop("enable_thinking", None)
            model.pop("reasoning_effort", None)
        elif normalized == "off":
            model["reasoning_choice"] = "off"
            model["enable_thinking"] = False
            model.pop("reasoning_effort", None)
        elif normalized == "on":
            model["reasoning_choice"] = "on"
            model["enable_thinking"] = True
            model.pop("reasoning_effort", None)
        else:
            model["reasoning_choice"] = normalized
            model["enable_thinking"] = True
            model["reasoning_effort"] = normalized

    config.update_config(_mutate)
    if validation_error:
        return False, validation_error[0]
    if not changed:
        return False, f"未找到模型: {model_id}"
    if normalized == "default":
        return True, f"模型 {changed[0]} 思考设置已恢复默认"
    return True, f"模型 {changed[0]} 思考设置已设为 {normalized}"


def get_model_reasoning_effort(model_id: str) -> str | None:
    """兼容 CLI 旧调用：仅在当前选择是具体强度时返回值。"""
    choice = get_model_reasoning_choice(model_id)
    return choice if choice in {"low", "medium", "high", "xhigh", "max"} else None


def set_model_reasoning_effort(model_id: str, level: str | None) -> tuple[bool, str]:
    """兼容 CLI 旧调用，委托给通用选择接口。"""
    return set_model_reasoning_choice(model_id, level)


def get_model_config_by_litellm_model(litellm_model: str, base_url: str | None = None) -> dict | None:
    """根据 litellm_model 字符串（以及可选的 base_url）查找对应的模型配置条目。

    返回的 dict 是 provider 下 model 条目的引用（浅拷贝），调用方不应修改。
    """
    data = load_config()
    litellm_model = str(litellm_model or "").strip()
    base_url = str(base_url or "").rstrip("/").lower()
    for provider in data.get("providers", []):
        p_base = str(provider.get("base_url") or "").rstrip("/").lower()
        for model in provider.get("models", []):
            if model.get("litellm_model") == litellm_model:
                if not base_url or not p_base or p_base == base_url:
                    return model
    return None


def get_model_config_by_id(model_id: str) -> dict | None:
    """按唯一模型 ID 返回配置浅拷贝，避免同一路由重名模型互相串设置。"""
    model = _find_config_model(load_config(), str(model_id or ""))
    return dict(model) if model is not None else None


def get_model_protocol(litellm_model: str, base_url: str | None = None) -> str | None:
    """按模型路由和 endpoint 返回显式 provider 协议。"""
    data = load_config()
    target_model = str(litellm_model or "").strip()
    target_base = str(base_url or "").rstrip("/").lower()
    for provider in data.get("providers", []):
        provider_base = str(provider.get("base_url") or "").rstrip("/").lower()
        for model in provider.get("models", []):
            if model.get("litellm_model") != target_model:
                continue
            if target_base and provider_base and provider_base != target_base:
                continue
            return validate_protocol(provider.get("protocol", "openai"))
    return None


def get_secondary_model() -> dict | None:
    """返回当前配置的 secondary（便宜）模型信息，用于子 Agent / 摘要 / 压缩等后台任务。"""
    data = load_config()
    secondary_id = data.get("secondary_model_id")
    if not secondary_id:
        return None
    for provider in data.get("providers", []):
        for model in provider.get("models", []):
            if model.get("id") == secondary_id:
                return {
                    "litellm_model":   model["litellm_model"],
                    "api_key":         effective_provider_api_key(provider),
                    "base_url":         provider["base_url"],
                    "context_window":  model.get("context_window", 128000),
                    "supports_vision": model.get("supports_vision"),
                    "supports_function_calling": model.get("supports_function_calling"),
                    "computer_protocol": model.get("computer_protocol"),
                    "system_prompt_extra": model.get("system_prompt_extra"),
                    "name":            model["name"],
                    "protocol":        provider.get("protocol", "openai"),
                    "provider_name":   provider["name"],
                    "id":              model["id"],
                }
    return None


def set_secondary_model(model_id: str | None):
    """设置/清除 secondary（便宜）模型 ID，用于子 Agent / 摘要 / 压缩等后台任务。"""
    def _mutate(data):
        if model_id:
            data["secondary_model_id"] = model_id
        else:
            data.pop("secondary_model_id", None)
    config.update_config(_mutate)


def get_secondary_roles() -> dict:
    """返回副模型用途开关：vision / compression / subagent，默认全部开启。"""
    data = load_config()
    roles = data.get("secondary_model_roles") or {}
    return {
        "vision": bool(roles.get("vision", True)),
        "compression": bool(roles.get("compression", True)),
        "subagent": bool(roles.get("subagent", True)),
    }


def set_secondary_roles(roles: dict):
    """更新副模型用途开关，仅覆盖传入的 key。"""
    def _mutate(data):
        current = data.get("secondary_model_roles") or {}
        for key in ("vision", "compression", "subagent"):
            if key in roles:
                current[key] = bool(roles[key])
        data["secondary_model_roles"] = current
    config.update_config(_mutate)


def add_provider(name: str, base_url: str, api_key: str, protocol: str) -> str:
    """添加一个新厂商，返回新生成的 provider_id。"""
    name = str(name or "").strip()
    if not name:
        raise ValueError("厂商名称不能为空")
    validate_protocol(protocol)
    pid = str(uuid.uuid4())[:8]

    def _mutate(data):
        data.setdefault("providers", []).append({
            "id": pid,
            "name": name,
            "base_url": base_url,
            "api_key": api_key,
            "protocol": protocol,
            "models": [],
        })
    config.update_config(_mutate)
    return pid


def add_model_to_provider(
    provider_id: str,
    name: str,
    context_window: int,
    supports_vision: bool | None = None,
    supports_function_calling: bool | None = None,
    computer_protocol: str | None = None,
    max_tokens: int | None = None,
    system_prompt_extra: str | None = None,
) -> str:
    """
    给指定厂商追加一个模型，litellm_model 格式为 {protocol}/{name}。
    返回新模型的 model_id。若当前无激活模型，自动激活该模型。

    system_prompt_extra：模型级系统提示扩展（可选）。None=不落键；""=清除该键；
    非空=写入，该模型请求时会自动追加这段指令，其他模型完全不受影响。
    """
    name = str(name or "").strip()
    context_window = int(context_window)
    if not name:
        raise ValueError("模型名称不能为空")
    if context_window <= 0:
        raise ValueError("上下文窗口必须大于 0")
    supports_function_calling = _normalize_capability_bool(
        "supports_function_calling", supports_function_calling
    )
    computer_protocol = _normalize_computer_protocol(computer_protocol)
    # 模型级系统提示扩展：None=保持现状，""=清除键，非空=写入（沿用既有字段风格）
    normalized_prompt_extra = (
        None if system_prompt_extra is None else str(system_prompt_extra).strip()
    )
    _validate_computer_combination(
        supports_vision if type(supports_vision) is bool else False,
        supports_function_calling,
        computer_protocol,
    )
    mid = str(uuid.uuid4())[:8]
    found = False

    def _mutate(data):
        nonlocal found
        for provider in data.get("providers", []):
            if provider["id"] == provider_id:
                found = True
                protocol = provider.get("protocol", "openai")
                model_entry = {
                    "id": mid,
                    "name": name,
                    "litellm_model": provider_route_name(protocol, name),
                    "context_window": context_window,
                }
                if max_tokens is not None:
                    model_entry["max_tokens"] = int(max_tokens)
                if supports_vision is not None:
                    model_entry["supports_vision"] = bool(supports_vision)
                if supports_function_calling is not None:
                    model_entry["supports_function_calling"] = supports_function_calling
                # ""（自动）不落键：缺省协议由 capability 按视觉+工具调用元数据判定
                if computer_protocol:
                    model_entry["computer_protocol"] = computer_protocol
                if normalized_prompt_extra:
                    model_entry["system_prompt_extra"] = normalized_prompt_extra
                elif normalized_prompt_extra == "":
                    model_entry.pop("system_prompt_extra", None)
                provider["models"].append(model_entry)
                break
        # 若当前尚未选择激活任何模型，自动激活新添加的模型
        if not data.get("active_model_id"):
            data["active_model_id"] = mid
    config.update_config(_mutate)
    if not found:
        raise ValueError(f"厂商不存在: {provider_id}")
    return mid


def delete_model(model_id: str):
    """删除一个模型；空厂商继续保留，厂商删除必须走显式操作。"""
    def _mutate(data):
        for provider in data.get("providers", []):
            for model in provider["models"][:]:
                if model["id"] == model_id:
                    provider["models"].remove(model)
        # 如果删掉的是 secondary 模型，一并清除
        if data.get("secondary_model_id") == model_id:
            data.pop("secondary_model_id", None)
        # 如果删掉的是激活模型，自动将激活模型切到第一个剩余模型
        if data.get("active_model_id") == model_id:
            first_remaining = None
            for p in data.get("providers", []):
                if p.get("models"):
                    first_remaining = p["models"][0]["id"]
                    break
            data["active_model_id"] = first_remaining
    config.update_config(_mutate)


def delete_provider(provider_id: str):
    """显式删除厂商及其模型，并修复活动/副模型选择。

    删除最后一个厂商同样属于用户明确意图，因此显式传 allow_provider_wipe=True
    放行写入保护（其余写入路径仍会拒绝把 providers 写空）。
    """
    def _mutate(data):
        providers = data.get("providers", [])
        target = next((provider for provider in providers if provider.get("id") == provider_id), None)
        if target is None:
            raise ValueError(f"厂商不存在: {provider_id}")
        removed_ids = {model.get("id") for model in target.get("models", [])}
        providers.remove(target)
        if data.get("secondary_model_id") in removed_ids:
            data.pop("secondary_model_id", None)
        if data.get("active_model_id") in removed_ids:
            data["active_model_id"] = next(
                (model["id"] for provider in providers for model in provider.get("models", [])),
                None,
            )

    config.update_config(_mutate, allow_provider_wipe=True)


def update_provider(
    provider_id: str,
    *,
    name: str,
    base_url: str,
    api_key: str,
    protocol: str,
):
    """更新厂商；空 API Key 表示保留原密钥。"""
    name = str(name or "").strip()
    if not name:
        raise ValueError("厂商名称不能为空")
    validate_protocol(protocol)

    def _mutate(data):
        provider = next(
            (item for item in data.get("providers", []) if item.get("id") == provider_id),
            None,
        )
        if provider is None:
            raise ValueError(f"厂商不存在: {provider_id}")
        provider["name"] = name
        provider["base_url"] = base_url
        provider["protocol"] = protocol
        if api_key:
            provider["api_key"] = api_key
        for model in provider.get("models", []):
            model["litellm_model"] = provider_route_name(protocol, model["name"])

    config.update_config(_mutate)


def update_model(
    provider_id: str,
    model_id: str,
    *,
    name: str,
    context_window: int,
    supports_vision: bool,
    supports_function_calling: bool | None = None,
    computer_protocol: str | None = None,
    system_prompt_extra: str | None = None,
    max_tokens: int | None = None,
):
    """更新单个模型的公开配置字段；新能力字段传 None 表示保留既有值。

    system_prompt_extra：None=保留既有值，""=清除该键，非空=写入。
    max_tokens（最大输出）：None=保留既有值，0=清除该键（回到内置默认），正整数=写入。
    """
    name = str(name or "").strip()
    context_window = int(context_window)
    if not name:
        raise ValueError("模型名称不能为空")
    if context_window <= 0:
        raise ValueError("上下文窗口必须大于 0")
    supports_function_calling = _normalize_capability_bool(
        "supports_function_calling", supports_function_calling
    )
    computer_protocol = _normalize_computer_protocol(computer_protocol)
    normalized_prompt_extra = (
        None if system_prompt_extra is None else str(system_prompt_extra).strip()
    )
    clear_max_tokens = False
    normalized_max_tokens: int | None = None
    if max_tokens is not None:
        try:
            mt = int(max_tokens)
        except (TypeError, ValueError):
            raise ValueError("最大输出必须为正整数")
        if mt < 0:
            raise ValueError("最大输出必须为正整数")
        if mt == 0:
            clear_max_tokens = True
        else:
            normalized_max_tokens = mt

    def _mutate(data):
        provider = next(
            (item for item in data.get("providers", []) if item.get("id") == provider_id),
            None,
        )
        if provider is None:
            raise ValueError(f"厂商不存在: {provider_id}")
        model = next(
            (item for item in provider.get("models", []) if item.get("id") == model_id),
            None,
        )
        if model is None:
            raise ValueError(f"模型不存在: {model_id}")
        # 组合校验针对生效后的最终值，拒绝降级出半有效 generic 配置
        effective_fc = (
            supports_function_calling
            if supports_function_calling is not None
            else model.get("supports_function_calling")
        )
        effective_protocol = (
            computer_protocol if computer_protocol is not None else model.get("computer_protocol")
        )
        _validate_computer_combination(bool(supports_vision), effective_fc, effective_protocol)
        model["name"] = name
        model["litellm_model"] = provider_route_name(provider.get("protocol", "openai"), name)
        model["context_window"] = context_window
        model["supports_vision"] = bool(supports_vision)
        if supports_function_calling is not None:
            model["supports_function_calling"] = supports_function_calling
        if computer_protocol == "":
            # ""（自动）：删除已存显式协议，回到缺省判定（视觉+工具调用）
            model.pop("computer_protocol", None)
        elif computer_protocol is not None:
            model["computer_protocol"] = computer_protocol
        # 模型级系统提示扩展：None=保留既有值，""=清除键，非空=写入
        if normalized_prompt_extra:
            model["system_prompt_extra"] = normalized_prompt_extra
        elif normalized_prompt_extra == "":
            model.pop("system_prompt_extra", None)
        # 最大输出：None=保留既有值，0=清除键（回到内置默认），正整数=写入
        if normalized_max_tokens is not None:
            model["max_tokens"] = normalized_max_tokens
        elif clear_max_tokens:
            model.pop("max_tokens", None)

    config.update_config(_mutate)


def update_provider_and_model(provider_id: str, model_id: str, base_url: str, api_key: str, model_name: str, context_window: int, supports_vision: bool | None = None, supports_function_calling: bool | None = None, computer_protocol: str | None = None):
    """更新厂商的 Base URL、API Key 以及旗下特定模型的名字、上下文窗口和视觉支持标志。"""
    supports_function_calling = _normalize_capability_bool(
        "supports_function_calling", supports_function_calling
    )
    computer_protocol = _normalize_computer_protocol(computer_protocol)

    def _mutate(data):
        for provider in data.get("providers", []):
            if provider["id"] == provider_id:
                provider["base_url"] = base_url
                provider["api_key"] = api_key
                for model in provider.get("models", []):
                    if model["id"] == model_id:
                        model["name"] = model_name
                        proto = provider.get("protocol", "openai")
                        model["litellm_model"] = provider_route_name(proto, model_name)
                        model["context_window"] = context_window
                        if supports_vision is not None:
                            model["supports_vision"] = bool(supports_vision)
                        effective_fc = (
                            supports_function_calling
                            if supports_function_calling is not None
                            else model.get("supports_function_calling")
                        )
                        effective_protocol = (
                            computer_protocol
                            if computer_protocol is not None
                            else model.get("computer_protocol")
                        )
                        _validate_computer_combination(
                            model.get("supports_vision"), effective_fc, effective_protocol
                        )
                        if supports_function_calling is not None:
                            model["supports_function_calling"] = supports_function_calling
                        if computer_protocol is not None:
                            model["computer_protocol"] = computer_protocol
                        break
                break
    config.update_config(_mutate)


def add_preset_provider_with_models(
    name: str,
    base_url: str,
    api_key: str,
    protocol: str,
    models: list[dict],
    preset_id: str | None = None,
) -> str:
    """批量添加预设厂商及其下的所有预置模型，默认激活厂商的第一个大模型，返回 provider_id。"""
    validate_protocol(protocol)
    pid = str(uuid.uuid4())[:8]

    def _mutate(data):
        provider_models = []
        for m in models:
            mid = str(uuid.uuid4())[:8]
            mname = m["name"]
            model_entry = {
                "id": mid,
                "name": mname,
                "litellm_model": provider_route_name(protocol, mname),
                "context_window": m.get("context_window", 128000),
            }
            if "max_tokens" in m:
                model_entry["max_tokens"] = int(m["max_tokens"])
            if "supports_vision" in m:
                model_entry["supports_vision"] = bool(m["supports_vision"])
            if "supports_function_calling" in m:
                model_entry["supports_function_calling"] = _normalize_capability_bool(
                    "supports_function_calling", m["supports_function_calling"]
                )
            if "computer_protocol" in m:
                model_entry["computer_protocol"] = _normalize_computer_protocol(
                    m["computer_protocol"]
                )
            if "system_prompt_extra" in m:
                _preset_prompt_extra = str(m["system_prompt_extra"]).strip()
                if _preset_prompt_extra:
                    model_entry["system_prompt_extra"] = _preset_prompt_extra
            _validate_computer_combination(
                model_entry.get("supports_vision"),
                model_entry.get("supports_function_calling"),
                model_entry.get("computer_protocol"),
            )
            provider_models.append(model_entry)

        provider_entry = {
            "id": pid,
            "name": name,
            "base_url": base_url,
            "api_key": api_key,
            "protocol": protocol,
            "models": provider_models,
        }
        if preset_id:
            provider_entry["preset_id"] = preset_id
        data.setdefault("providers", []).append(provider_entry)
        # 仅在当前无激活模型时才激活新厂商的首个模型，
        # 避免抢走用户正在使用的模型，也避免绕过 token 超限安全检查
        if provider_models and not data.get("active_model_id"):
            data["active_model_id"] = provider_models[0]["id"]
    config.update_config(_mutate)
    return pid


def add_preset_provider(preset_id: str, api_key: str) -> str:
    """仅接收预置 ID 和 API Key，其余字段从共享目录读取。

    预置目录只含厂商信息（名称/Base URL/协议），不携带模型——用户添加后
    通过「获取模型列表」按需拉取并自动填充。
    """
    preset = get_preset_provider(preset_id)
    return add_preset_provider_with_models(
        preset["name"],
        preset["base_url"],
        api_key,
        preset["protocol"],
        preset.get("models", []),
        preset_id=preset["id"],
    )


def _apply_preset_capability_fields(entry: dict, catalog_model: dict) -> None:
    """把目录条目的可选能力字段落进新建模型条目。"""
    if "max_tokens" in catalog_model:
        entry["max_tokens"] = int(catalog_model["max_tokens"])
    if "supports_vision" in catalog_model:
        entry["supports_vision"] = bool(catalog_model["supports_vision"])
    if "supports_function_calling" in catalog_model:
        entry["supports_function_calling"] = _normalize_capability_bool(
            "supports_function_calling", catalog_model["supports_function_calling"]
        )
    if "computer_protocol" in catalog_model:
        entry["computer_protocol"] = _normalize_computer_protocol(
            catalog_model["computer_protocol"]
        )
    # 模型级系统提示扩展（预置护栏）：目录声明了才落键，未声明的模型不受影响。
    if "system_prompt_extra" in catalog_model:
        prompt_extra = str(catalog_model["system_prompt_extra"]).strip()
        if prompt_extra:
            entry["system_prompt_extra"] = prompt_extra



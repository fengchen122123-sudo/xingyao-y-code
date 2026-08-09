"""
core/model_manager.py
多厂商模型管理器：读写 config.json，提供 get/set 激活模型的接口。
"""
import json
import os
import uuid

import config
_CONFIG_FILE = config._CONFIG_FILE


def load_config() -> dict:
    """加载完整配置，如果是旧格式则自动迁移。主文件损坏时优先从 .bak 备份恢复。"""
    data = config._read_json_with_backup(_CONFIG_FILE, default=None)
    if not isinstance(data, dict):
        data = {}

    # 检测旧格式（只有 model/api_key/base_url 的扁平结构），自动迁移
    if "model" in data and "providers" not in data:
        try:
            old_model = data.get("model", "openai/agnes-2.0-flash")
            if isinstance(old_model, dict):
                old_model = old_model.get("litellm_model") or old_model.get("name") or "openai/agnes-2.0-flash"
            elif not isinstance(old_model, str):
                old_model = str(old_model) if old_model else "openai/agnes-2.0-flash"
            
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
                        "models": [
                            {
                                "id": "ds-v4-pro",
                                "name": "deepseek-v4-pro",
                                "litellm_model": "openai/deepseek-v4-pro",
                                "context_window": 1000000,
                                "enable_thinking": True,
                                "reasoning_effort": "max"
                            },
                            {
                                "id": "ds-v4-flash",
                                "name": "deepseek-v4-flash",
                                "litellm_model": "openai/deepseek-v4-flash",
                                "context_window": 1000000,
                                "enable_thinking": True,
                                "reasoning_effort": "max"
                            }
                        ]
                    }
                ]
            }
            _write_config(data)
        else:
            data["providers"] = []

    return data


def _write_config(data: dict):
    """原子写配置：临时文件 + os.replace + 进程内锁，防止写一半崩溃损坏配置。"""
    with config._CONFIG_LOCK:
        config._atomic_write_json(_CONFIG_FILE, data)


def get_active_model() -> dict | None:
    """
    返回当前激活模型的完整信息字典，包含：
      litellm_model, api_key, base_url, context_window, name
    如果没有配置任何模型，返回 None。
    """
    data = load_config()
    active_id = data.get("active_model_id")
    for provider in data.get("providers", []):
        for model in provider.get("models", []):
            if model["id"] == active_id:
                return {
                    "litellm_model":   model["litellm_model"],
                    "api_key":         provider["api_key"],
                    "base_url":         provider["base_url"],
                    "context_window":  model.get("context_window", 128000),
                    "supports_vision": model.get("supports_vision"),
                    "name":            model["name"],
                    "protocol":        provider.get("protocol", "openai"),
                    "provider_name":   provider["name"],
                }
    return None


def set_active_model(model_id: str):
    """将 model_id 设为激活模型并持久化。"""
    # 读-改-写整体在锁内完成，避免与 config.save_config_value 并发时互相覆盖
    def _mutate(data):
        data["active_model_id"] = model_id
    config.update_config(_mutate)


REASONING_EFFORT_LEVELS = ("low", "high", "max")


def get_model_reasoning_effort(model_id: str) -> str | None:
    """返回模型条目上显式配置的 reasoning_effort；未配置返回 None（请求层用内置默认）。"""
    if not model_id:
        return None
    data = load_config()
    for provider in data.get("providers", []):
        for model in provider.get("models", []):
            if model.get("id") == model_id:
                level = model.get("reasoning_effort")
                return str(level).lower() if level else None
    return None


def set_model_reasoning_effort(model_id: str, level: str | None) -> tuple[bool, str]:
    """按模型持久化推理强度档位；level 为 None/\"default\" 时删除字段恢复内置默认。"""
    normalized = str(level or "").strip().lower()
    if normalized in ("", "default"):
        normalized = ""
    elif normalized not in REASONING_EFFORT_LEVELS:
        return False, f"无效档位: {level}（可选 low/high/max/default）"

    found = []

    def _mutate(data):
        for provider in data.get("providers", []):
            for model in provider.get("models", []):
                if model.get("id") == model_id:
                    found.append(model.get("name") or model_id)
                    if normalized:
                        model["reasoning_effort"] = normalized
                    else:
                        model.pop("reasoning_effort", None)
                    return
    config.update_config(_mutate)
    if not found:
        return False, f"未找到模型: {model_id}"
    if normalized:
        return True, f"模型 {found[0]} 推理强度已设为 {normalized}"
    return True, f"模型 {found[0]} 推理强度已恢复默认"


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
                    "api_key":         provider["api_key"],
                    "base_url":         provider["base_url"],
                    "context_window":  model.get("context_window", 128000),
                    "supports_vision": model.get("supports_vision"),
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


def add_model_to_provider(provider_id: str, name: str, context_window: int, supports_vision: bool | None = None) -> str:
    """
    给指定厂商追加一个模型，litellm_model 格式为 {protocol}/{name}。
    返回新模型的 model_id。若当前无激活模型，自动激活该模型。
    """
    mid = str(uuid.uuid4())[:8]

    def _mutate(data):
        for provider in data.get("providers", []):
            if provider["id"] == provider_id:
                protocol = provider.get("protocol", "openai")
                # 用户可能直接输入带前缀的模型名（如 openai/gpt-4o）：
                # 已含 / 前缀时不再重复添加，避免出现 openai/openai/gpt-4o
                litellm_model = name if "/" in name else f"{protocol}/{name}"
                model_entry = {
                    "id": mid,
                    "name": name,
                    "litellm_model": litellm_model,
                    "context_window": context_window,
                }
                if supports_vision is not None:
                    model_entry["supports_vision"] = bool(supports_vision)
                provider["models"].append(model_entry)
                break
        # 若当前尚未选择激活任何模型，自动激活新添加的模型
        if not data.get("active_model_id"):
            data["active_model_id"] = mid
    config.update_config(_mutate)
    return mid


def delete_model(model_id: str):
    """删除一个模型；如果厂商下没有模型了，厂商也一并删除。若删除了激活模型，自动切到第一个剩余模型。"""
    def _mutate(data):
        for provider in data.get("providers", [])[:]:
            for model in provider["models"][:]:
                if model["id"] == model_id:
                    provider["models"].remove(model)
            if not provider["models"]:
                data["providers"].remove(provider)
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


def update_provider_and_model(provider_id: str, model_id: str, base_url: str, api_key: str, model_name: str, context_window: int, supports_vision: bool | None = None):
    """更新厂商的 Base URL、API Key 以及旗下特定模型的名字、上下文窗口和视觉支持标志。"""
    def _mutate(data):
        for provider in data.get("providers", []):
            if provider["id"] == provider_id:
                provider["base_url"] = base_url
                provider["api_key"] = api_key
                for model in provider.get("models", []):
                    if model["id"] == model_id:
                        model["name"] = model_name
                        proto = provider.get("protocol", "openai")
                        # 与 add_model_to_provider 一致：已含 / 前缀不重复添加
                        model["litellm_model"] = model_name if "/" in model_name else f"{proto}/{model_name}"
                        model["context_window"] = context_window
                        if supports_vision is not None:
                            model["supports_vision"] = bool(supports_vision)
                        break
                break
    config.update_config(_mutate)


def add_preset_provider_with_models(name: str, base_url: str, api_key: str, protocol: str, models: list[dict]) -> str:
    """批量添加预设厂商及其下的所有预置模型，默认激活厂商的第一个大模型，返回 provider_id。"""
    pid = str(uuid.uuid4())[:8]

    def _mutate(data):
        provider_models = []
        for m in models:
            mid = str(uuid.uuid4())[:8]
            mname = m["name"]
            litellm_model = mname if "/" in mname else f"{protocol}/{mname}"
            model_entry = {
                "id": mid,
                "name": mname,
                "litellm_model": litellm_model,
                "context_window": m.get("context_window", 128000),
            }
            if "supports_vision" in m:
                model_entry["supports_vision"] = bool(m["supports_vision"])
            provider_models.append(model_entry)

        data.setdefault("providers", []).append({
            "id": pid,
            "name": name,
            "base_url": base_url,
            "api_key": api_key,
            "protocol": protocol,
            "models": provider_models,
        })
        # 仅在当前无激活模型时才激活新厂商的首个模型，
        # 避免抢走用户正在使用的模型，也避免绕过 token 超限安全检查
        if provider_models and not data.get("active_model_id"):
            data["active_model_id"] = provider_models[0]["id"]
    config.update_config(_mutate)
    return pid

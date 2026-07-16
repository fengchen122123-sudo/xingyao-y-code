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
    """加载完整配置，如果是旧格式则自动迁移。"""
    try:
        with open(_CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, FileNotFoundError):
        data = {}

    # 检测旧格式（只有 model/api_key/base_url 的扁平结构），自动迁移
    if "model" in data and "providers" not in data:
        old_model = data.get("model", "openai/agnes-2.0-flash")
        model_name = old_model.split("/")[-1]
        mid = str(uuid.uuid4())[:8]
        pid = str(uuid.uuid4())[:8]
        data = {
            "active_model_id": mid,
            "providers": [
                {
                    "id": pid,
                    "name": "默认厂商",
                    "base_url": data.get("base_url", ""),
                    "api_key": data.get("api_key", ""),
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
        _write_config(data)

    # 如果完全空，初始化为 DeepSeek 默认结构
    if "providers" not in data or not data.get("providers"):
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
                            "litellm_model": "deepseek/deepseek-chat",
                            "context_window": 1000000
                        },
                        {
                            "id": "ds-v4-flash",
                            "name": "deepseek-v4-flash",
                            "litellm_model": "deepseek/deepseek-coder",
                            "context_window": 1000000
                        }
                    ]
                }
            ]
        }
        _write_config(data)

    return data


def _write_config(data: dict):
    with open(_CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


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
                    "litellm_model":  model["litellm_model"],
                    "api_key":        provider["api_key"],
                    "base_url":        provider["base_url"],
                    "context_window": model.get("context_window", 128000),
                    "name":            model["name"],
                    "protocol":        provider.get("protocol", "openai"),
                    "provider_name":   provider["name"],
                }
    return None


def set_active_model(model_id: str):
    """将 model_id 设为激活模型并持久化。"""
    data = load_config()
    data["active_model_id"] = model_id
    _write_config(data)


def add_provider(name: str, base_url: str, api_key: str, protocol: str) -> str:
    """添加一个新厂商，返回新生成的 provider_id。"""
    data = load_config()
    pid = str(uuid.uuid4())[:8]
    data["providers"].append({
        "id": pid,
        "name": name,
        "base_url": base_url,
        "api_key": api_key,
        "protocol": protocol,
        "models": [],
    })
    _write_config(data)
    return pid


def add_model_to_provider(provider_id: str, name: str, context_window: int) -> str:
    """
    给指定厂商追加一个模型，litellm_model 格式为 {protocol}/{name}。
    返回新模型的 model_id。
    """
    data = load_config()
    mid = str(uuid.uuid4())[:8]
    for provider in data["providers"]:
        if provider["id"] == provider_id:
            protocol = provider.get("protocol", "openai")
            litellm_model = f"{protocol}/{name}"
            provider["models"].append({
                "id": mid,
                "name": name,
                "litellm_model": litellm_model,
                "context_window": context_window,
            })
            break
    _write_config(data)
    return mid


def delete_model(model_id: str):
    """删除一个模型；如果厂商下没有模型了，厂商也一并删除。"""
    data = load_config()
    for provider in data["providers"][:]:
        for model in provider["models"][:]:
            if model["id"] == model_id:
                provider["models"].remove(model)
        if not provider["models"]:
            data["providers"].remove(provider)
    # 如果删掉的是激活模型，清空激活状态
    if data.get("active_model_id") == model_id:
        data["active_model_id"] = None
    _write_config(data)


def update_provider_and_model(provider_id: str, model_id: str, base_url: str, api_key: str, model_name: str, context_window: int):
    """更新厂商的 Base URL、API Key 以及旗下特定模型的名字和上下文窗口。"""
    data = load_config()
    for provider in data.get("providers", []):
        if provider["id"] == provider_id:
            provider["base_url"] = base_url
            provider["api_key"] = api_key
            for model in provider.get("models", []):
                if model["id"] == model_id:
                    model["name"] = model_name
                    proto = provider.get("protocol", "openai")
                    model["litellm_model"] = f"{proto}/{model_name}"
                    model["context_window"] = context_window
                    break
            break
    _write_config(data)

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
        except Exception:
            data = {}

    # 仅在全新启动/无 providers 键时初始化为 DeepSeek 默认结构
    if "providers" not in data:
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
    # 读-改-写整体在锁内完成，避免与 config.save_config_value 并发时互相覆盖
    def _mutate(data):
        data["active_model_id"] = model_id
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


def add_model_to_provider(provider_id: str, name: str, context_window: int) -> str:
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
                provider["models"].append({
                    "id": mid,
                    "name": name,
                    "litellm_model": litellm_model,
                    "context_window": context_window,
                })
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
        # 如果删掉的是激活模型，自动将激活模型切到第一个剩余模型
        if data.get("active_model_id") == model_id:
            first_remaining = None
            for p in data.get("providers", []):
                if p.get("models"):
                    first_remaining = p["models"][0]["id"]
                    break
            data["active_model_id"] = first_remaining
    config.update_config(_mutate)


def update_provider_and_model(provider_id: str, model_id: str, base_url: str, api_key: str, model_name: str, context_window: int):
    """更新厂商的 Base URL、API Key 以及旗下特定模型的名字和上下文窗口。"""
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
            provider_models.append({
                "id": mid,
                "name": mname,
                "litellm_model": litellm_model,
                "context_window": m.get("context_window", 128000),
            })

        data.setdefault("providers", []).append({
            "id": pid,
            "name": name,
            "base_url": base_url,
            "api_key": api_key,
            "protocol": protocol,
            "models": provider_models,
        })
        # 默认自动激活新添加预设厂商的第一个大模型
        if provider_models:
            data["active_model_id"] = provider_models[0]["id"]
    config.update_config(_mutate)
    return pid

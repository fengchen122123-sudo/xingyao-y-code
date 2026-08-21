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
    litellm_model_name,
    validate_protocol,
)
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
                        "protocol": "responses",
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
                                "id": "ds-v4-flash",
                                "name": "deepseek-v4-flash",
                                "litellm_model": "openai/deepseek-v4-flash",
                                "context_window": 1000000,
                                "supports_vision": False,
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
        "api_key":         provider["api_key"],
        "base_url":         provider["base_url"],
        "context_window":  model.get("context_window", 128000),
        "supports_vision": model.get("supports_vision"),
        "name":            model["name"],
        "protocol":        provider.get("protocol", "openai"),
        "provider_name":   provider["name"],
        "id":              model["id"],
    }


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


def add_model_to_provider(provider_id: str, name: str, context_window: int, supports_vision: bool | None = None) -> str:
    """
    给指定厂商追加一个模型，litellm_model 格式为 {protocol}/{name}。
    返回新模型的 model_id。若当前无激活模型，自动激活该模型。
    """
    name = str(name or "").strip()
    context_window = int(context_window)
    if not name:
        raise ValueError("模型名称不能为空")
    if context_window <= 0:
        raise ValueError("上下文窗口必须大于 0")
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
                    "litellm_model": litellm_model_name(protocol, name),
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
                    # 预置厂商模型记录删除痕迹：sync_preset_provider_models
                    # 只追加"用户没见过的新模型"，被手动删掉的不会因同步复活
                    # （如需找回，删除厂商后重新添加即可）
                    preset_id = provider.get("preset_id")
                    if preset_id:
                        try:
                            preset = get_preset_provider(preset_id)
                        except KeyError:
                            preset = None
                        if preset and any(
                            m.get("name") == model.get("name") for m in preset.get("models", [])
                        ):
                            removed = provider.setdefault("removed_preset_models", [])
                            if model.get("name") not in removed:
                                removed.append(model.get("name"))
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
    """显式删除厂商及其模型，并修复活动/副模型选择。"""
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

    config.update_config(_mutate)


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
            model["litellm_model"] = litellm_model_name(protocol, model["name"])

    config.update_config(_mutate)


def update_model(
    provider_id: str,
    model_id: str,
    *,
    name: str,
    context_window: int,
    supports_vision: bool,
):
    """更新单个模型的公开配置字段。"""
    name = str(name or "").strip()
    context_window = int(context_window)
    if not name:
        raise ValueError("模型名称不能为空")
    if context_window <= 0:
        raise ValueError("上下文窗口必须大于 0")

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
        model["name"] = name
        model["litellm_model"] = litellm_model_name(provider.get("protocol", "openai"), name)
        model["context_window"] = context_window
        model["supports_vision"] = bool(supports_vision)

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
                        model["litellm_model"] = litellm_model_name(proto, model_name)
                        model["context_window"] = context_window
                        if supports_vision is not None:
                            model["supports_vision"] = bool(supports_vision)
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
                "litellm_model": litellm_model_name(protocol, mname),
                "context_window": m.get("context_window", 128000),
            }
            if "supports_vision" in m:
                model_entry["supports_vision"] = bool(m["supports_vision"])
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
    """仅接收预置 ID 和 API Key，其余字段从共享目录读取。"""
    preset = get_preset_provider(preset_id)
    return add_preset_provider_with_models(
        preset["name"],
        preset["base_url"],
        api_key,
        preset["protocol"],
        preset["models"],
        preset_id=preset["id"],
    )


def sync_preset_provider_models(preset_id: str) -> int:
    """把预置目录中该厂商新增的模型增量合并进用户配置，返回本次新增数量。

    预置目录（model_catalog.py）更新不会自动同步到已添加过该厂商的旧
    config.json（添加时是快照拷贝）。按 preset_id 定位厂商，只追加用户
    配置中缺少的模型（按模型 name 比对），绝不删除：用户手动删掉的
    预置模型（delete_model 时记录于 removed_preset_models）与自定义
    模型都保持原样。厂商不存在（未添加过或已删除）时静默返回 0。
    """
    preset = get_preset_provider(preset_id)
    added_count = 0

    def _mutate(data):
        nonlocal added_count
        provider = next(
            (p for p in data.get("providers", []) if p.get("preset_id") == preset_id),
            None,
        )
        if provider is None:
            return
        existing_names = {m.get("name") for m in provider.get("models", [])}
        skipped_names = set(provider.get("removed_preset_models", []))
        for m in preset["models"]:
            if m["name"] in existing_names or m["name"] in skipped_names:
                continue
            entry = {
                "id": str(uuid.uuid4())[:8],
                "name": m["name"],
                "litellm_model": litellm_model_name(
                    provider.get("protocol", "openai"), m["name"]
                ),
                "context_window": m.get("context_window", 128000),
            }
            if "supports_vision" in m:
                entry["supports_vision"] = bool(m["supports_vision"])
            provider.setdefault("models", []).append(entry)
            added_count += 1

    config.update_config(_mutate)
    return added_count

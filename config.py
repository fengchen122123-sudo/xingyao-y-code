import json
import os

_CONFIG_FILE = os.path.join(os.path.dirname(__file__), "config.json")

def _load():
    try:
        with open(_CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data
    except (json.JSONDecodeError, FileNotFoundError):
        return {}

_cfg = _load()

# 1. 尝试从多厂商配置中提取激活模型
active_model_id = _cfg.get("active_model_id")
providers = _cfg.get("providers", [])

resolved_model = None
resolved_key = None
resolved_url = None
resolved_ctx = 128000

if active_model_id and providers:
    for provider in providers:
        for model in provider.get("models", []):
            if model.get("id") == active_model_id:
                resolved_model = model.get("litellm_model")
                resolved_key = provider.get("api_key")
                resolved_url = provider.get("base_url")
                resolved_ctx = model.get("context_window", 128000)
                break
        if resolved_model:
            break

# 2. 如果未找到多厂商配置，则回退到扁平旧配置或系统默认值
MY_MODEL = resolved_model or _cfg.get("model", "openai/agnes-2.0-flash")
MY_KEY = resolved_key or _cfg.get("api_key", "")
MY_BASE_URL = resolved_url or _cfg.get("base_url", "https://apihub.agnes-ai.com/v1")
MY_THEME = _cfg.get("theme", "cyan")
MY_PERMISSION_MODE = _cfg.get("permission_mode", "confirm")
MY_CONTEXT_WINDOW = resolved_ctx

def save_config_value(key, value):
    """保存配置项并持久化写入 config.json"""
    try:
        data = _load()
        data[key] = value
        with open(_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        return True
    except Exception:
        return False

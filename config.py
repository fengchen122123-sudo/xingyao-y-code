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

# 从 config.json 读取；若不存在则使用下方默认值（可手动修改 config.json 来覆盖）
MY_MODEL    = _cfg.get("model",    "openai/agnes-2.0-flash")
MY_KEY      = _cfg.get("api_key",  "")
MY_BASE_URL = _cfg.get("base_url", "https://apihub.agnes-ai.com/v1")
MY_THEME    = _cfg.get("theme",    "cyan")
MY_PERMISSION_MODE = _cfg.get("permission_mode", "confirm")

def save_config_value(key, value):
    """保存配置项并持久化写入 config.json"""
    try:
        data = _load()
        data[key] = value
        with open(_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
        return True
    except Exception:
        return False

# 安全提示：不要把真实 API 密钥硬编码到代码里！
# 请把 config.example.json 复制为 config.json 并填入你的真实密钥。

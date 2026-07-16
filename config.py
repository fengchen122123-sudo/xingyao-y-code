import json
import os
import shutil

# 获取用户目录下的 .ycode 文件夹
YCODE_DIR = os.path.join(os.path.expanduser("~"), ".ycode")
os.makedirs(YCODE_DIR, exist_ok=True)

# 集中定义所有持久化文件路径
_CONFIG_FILE = os.path.join(YCODE_DIR, "config.json")
DB_FILE = os.path.join(YCODE_DIR, "ycode.db")
ACTIVE_SKILLS_FILE = os.path.join(YCODE_DIR, "active_skills.json")
MCP_FILE = os.path.join(YCODE_DIR, "mcp.json")
SYSTEM_PROMPT_FILE = os.path.join(YCODE_DIR, "system_prompt.md")

# 自动从当前源码目录初始化默认配置文件
if not os.path.exists(_CONFIG_FILE):
    default_config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
    if os.path.exists(default_config_path):
        try:
            shutil.copy(default_config_path, _CONFIG_FILE)
        except Exception:
            pass
    if not os.path.exists(_CONFIG_FILE):
        try:
            with open(_CONFIG_FILE, "w", encoding="utf-8") as f:
                default_data = {
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
                json.dump(default_data, f, indent=2, ensure_ascii=False)
        except Exception:
            pass

if not os.path.exists(SYSTEM_PROMPT_FILE):
    default_prompt_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "system_prompt.md")
    if os.path.exists(default_prompt_path):
        try:
            shutil.copy(default_prompt_path, SYSTEM_PROMPT_FILE)
        except Exception:
            pass
    if not os.path.exists(SYSTEM_PROMPT_FILE):
        try:
            with open(SYSTEM_PROMPT_FILE, "w", encoding="utf-8") as f:
                f.write("你叫星瑶，是一个顶尖的女性黑客与代码编程高手。")
        except Exception:
            pass

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
resolved_ctx = 1000000

if active_model_id and providers:
    for provider in providers:
        for model in provider.get("models", []):
            if model.get("id") == active_model_id:
                resolved_model = model.get("litellm_model")
                resolved_key = provider.get("api_key")
                resolved_url = provider.get("base_url")
                resolved_ctx = model.get("context_window", 1000000)
                break
        if resolved_model:
            break

# 2. 如果未找到多厂商配置，则回退到扁平旧配置或系统默认值
MY_MODEL = resolved_model or _cfg.get("model", "deepseek/deepseek-chat")
MY_KEY = resolved_key or _cfg.get("api_key", "")
MY_BASE_URL = resolved_url or _cfg.get("base_url", "https://api.deepseek.com")
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

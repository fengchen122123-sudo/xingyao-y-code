import json
import logging
import os
import shutil
import threading

# 进程内配置写锁：防止并发写 config.json 互相截断
_CONFIG_LOCK = threading.Lock()


def _backup_if_valid(path):
    """仅在当前文件是合法 JSON 时刷新 .bak 备份，保证备份始终是最近一份可用配置。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            json.load(f)
        shutil.copy(path, path + ".bak")
    except Exception:
        pass


def _log_warning(message, *args):
    """配置写失败等异常写入 runtime warnings 日志，而不是静默吞掉。"""
    try:
        logging.getLogger("py.warnings").warning(message, *args)
    except Exception:
        pass


def _atomic_write_json(path, data, indent=2):
    """原子写 JSON：临时文件 + os.replace，写前刷新 .bak，避免写一半崩溃损坏配置。"""
    tmp_path = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=indent, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        _backup_if_valid(path)
        os.replace(tmp_path, path)
    finally:
        # os.replace 在 Windows 上目标文件被占用时会 PermissionError，
        # 异常路径必须清理临时文件，避免 *.tmp 泄漏堆积
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass


def _read_json_with_backup(path, default=None):
    """读取 JSON；主文件损坏时先尝试从 .bak 备份恢复，而不是直接丢弃用户配置。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except (json.JSONDecodeError, OSError):
        backup = path + ".bak"
        try:
            with open(backup, "r", encoding="utf-8") as f:
                data = json.load(f)
            # 用备份回写主文件完成自愈
            _atomic_write_json(path, data)
            return data
        except Exception:
            return default

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
    data = _read_json_with_backup(_CONFIG_FILE, default=None)
    return data if isinstance(data, dict) else {}


# 动态配置常量：MY_MODEL / MY_KEY / MY_BASE_URL / MY_CONTEXT_WINDOW / MY_THEME / MY_PERMISSION_MODE
# 通过模块级 __getattr__ 实时从 core.model_manager.get_active_model() 解析，
# 保证 set_active_model / save_config_value 之后读取到的永远是最新值（常量名保持向后兼容）。
_DYNAMIC_CONSTANTS = (
    "MY_MODEL", "MY_KEY", "MY_BASE_URL",
    "MY_CONTEXT_WINDOW", "MY_THEME", "MY_PERMISSION_MODE",
)


def _resolve_constant(name):
    info = None
    try:
        from core.model_manager import get_active_model
        info = get_active_model()
    except Exception:
        info = None
    data = _load()

    if name == "MY_MODEL":
        return (info or {}).get("litellm_model") or data.get("model", "deepseek/deepseek-chat")
    if name == "MY_KEY":
        return (info or {}).get("api_key") or data.get("api_key", "")
    if name == "MY_BASE_URL":
        return (info or {}).get("base_url") or data.get("base_url", "https://api.deepseek.com")
    if name == "MY_CONTEXT_WINDOW":
        return (info or {}).get("context_window", 1000000)
    if name == "MY_THEME":
        return data.get("theme", "cyan")
    if name == "MY_PERMISSION_MODE":
        return data.get("permission_mode", "confirm")
    raise AttributeError(name)


def __getattr__(name):
    if name in _DYNAMIC_CONSTANTS:
        return _resolve_constant(name)
    raise AttributeError(f"module 'config' has no attribute {name!r}")


def update_config(mutator):
    """在进程内锁中执行 load→mutate→原子写，消除读-改-写竞态（TOCTOU 丢失更新）。

    mutator 接收当前配置 dict：原地修改后返回 None，或返回一个全新的 dict 作为写入内容。
    返回值为实际写入的配置 dict。
    """
    with _CONFIG_LOCK:
        data = _load()
        result = mutator(data)
        if result is not None:
            data = result
        _atomic_write_json(_CONFIG_FILE, data)
    return data


def save_config_value(key, value):
    """保存配置项并持久化写入 config.json（原子写 + 进程内锁）"""
    try:
        with _CONFIG_LOCK:
            data = _load()
            data[key] = value
            _atomic_write_json(_CONFIG_FILE, data)
        return True
    except Exception as e:
        # 调用方普遍不检查返回值，失败必须留下诊断痕迹而不是静默丢配置
        _log_warning("save_config_value(%r) 写入 config.json 失败: %s", key, e)
        return False

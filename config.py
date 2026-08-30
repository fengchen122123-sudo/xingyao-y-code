import json
import logging
import os
import shutil
import threading
from copy import deepcopy

from core.credentials import DPAPI_PREFIX, KEYRING_PREFIX, protect_secret, unprotect_secret

try:
    import tomllib
except ImportError:  # Python < 3.11 时优雅降级为忽略 TOML
    tomllib = None

# 进程内配置写锁：防止并发写 config.json 互相截断
_CONFIG_LOCK = threading.RLock()


def _transform_api_keys(data, transform):
    result = deepcopy(data)
    if not isinstance(result, dict):
        return result
    providers = result.get("providers")
    if not isinstance(providers, list):
        return result
    for provider in providers:
        if isinstance(provider, dict) and isinstance(provider.get("api_key"), str):
            provider["api_key"] = transform(provider["api_key"])
    return result


def _prepare_for_storage(path, data):
    if os.path.abspath(path) != os.path.abspath(_CONFIG_FILE):
        return data
    return _transform_api_keys(data, protect_secret)


def _unprotect_api_key_safely(value):
    """单个 provider 的 api_key 解密失败（DPAPI blob 损坏/跨账户）时置空该 key 并告警，
    绝不让单点解密失败被误归类为整份配置损坏而触发 .bak 恢复甚至静默降级为默认值。"""
    try:
        return unprotect_secret(value)
    except Exception as error:
        _log_warning("config.json 中某个 provider 的 api_key 解密失败（%s），已置空该 key，其余配置保留", error)
        return ""


def _restore_after_read(path, data):
    if os.path.abspath(path) != os.path.abspath(_CONFIG_FILE):
        return data
    return _transform_api_keys(data, _unprotect_api_key_safely)


def _backup_if_valid(path):
    """仅在当前文件是合法 JSON 时刷新 .bak 备份，保证备份始终是最近一份可用配置。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            existing = json.load(f)
        with open(path + ".bak", "w", encoding="utf-8") as backup:
            json.dump(_prepare_for_storage(path, _restore_after_read(path, existing)), backup, indent=2, ensure_ascii=False)
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
        stored_data = _prepare_for_storage(path, data)
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(stored_data, f, indent=indent, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        _backup_if_valid(path)
        os.replace(tmp_path, path)
        _CONFIG_CACHE.pop(path, None)
    finally:
        # os.replace 在 Windows 上目标文件被占用时会 PermissionError，
        # 异常路径必须清理临时文件，避免 *.tmp 泄漏堆积
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass


_CONFIG_CACHE: dict[str, dict] = {}


def _copy_cached(data):
    """缓存命中时返回浅拷贝：调用方原地修改（如 update_config 的 mutator）
    不应污染缓存，否则后续读取会看到未持久化的脏数据。"""
    if isinstance(data, (dict, list)):
        return data.copy()
    return data


def _read_json_with_backup(path, default=None):
    """读取 JSON；使用 mtime 缓存，仅在磁盘文件真正改变时才重新读取磁盘，极大提升多窗口/高频刷新性能。"""
    try:
        current_mtime = os.path.getmtime(path)
        # 缓存探测/提交必须在 _CONFIG_LOCK 内完成：mtime 与 data 两个字段的
        # 写入不是原子的，无锁并发下检查-写入交错会让读方拿到 mtime 与
        # 内容不配套的缓存条目
        with _CONFIG_LOCK:
            cache_entry = _CONFIG_CACHE.setdefault(path, {"mtime": 0, "data": None})
            if cache_entry["data"] is not None and cache_entry["mtime"] == current_mtime:
                return _copy_cached(cache_entry["data"])
        with open(path, "r", encoding="utf-8") as f:
            raw_data = json.load(f)
            data = _restore_after_read(path, raw_data)
            if (
                os.path.abspath(path) == os.path.abspath(_CONFIG_FILE)
                and any(
                    isinstance(provider, dict)
                    and provider.get("api_key")
                    and not str(provider["api_key"]).startswith((DPAPI_PREFIX, KEYRING_PREFIX))
                    for provider in raw_data.get("providers", [])
                )
            ):
                with _CONFIG_LOCK:
                    _atomic_write_json(path, data)
            with _CONFIG_LOCK:
                cache_entry["mtime"] = current_mtime
                cache_entry["data"] = data
            return _copy_cached(data)
    except FileNotFoundError:
        return default
    except (json.JSONDecodeError, OSError):
        backup = path + ".bak"
        try:
            with open(backup, "r", encoding="utf-8") as f:
                data = _restore_after_read(path, json.load(f))
            # 用备份回写主文件完成自愈；对 config.json 加锁，避免与正常写路径竞态覆盖新配置
            if path == _CONFIG_FILE:
                with _CONFIG_LOCK:
                    _atomic_write_json(path, data)
            else:
                _atomic_write_json(path, data)
            return data
        except Exception:
            return default

# 获取用户目录下的 .ycode 文件夹。测试/隔离运行可在进程启动前显式覆盖，
# 默认生产路径保持不变。
YCODE_DIR = os.environ.get("YCODE_DATA_DIR") or os.path.join(
    os.path.expanduser("~"), ".ycode"
)
os.makedirs(YCODE_DIR, exist_ok=True)

# 集中定义所有持久化文件路径
_CONFIG_FILE = os.path.join(YCODE_DIR, "config.json")
DB_FILE = os.path.join(YCODE_DIR, "ycode.db")
# GUI 使用独立的会话库：GUI 与 CLI 的对话记录互不可见、互不影响
GUI_DB_FILE = os.path.join(YCODE_DIR, "ycode_gui.db")
ACTIVE_SKILLS_FILE = os.path.join(YCODE_DIR, "active_skills.json")
MCP_FILE = os.path.join(YCODE_DIR, "mcp.json")
# 只读 TOML 兼容层：config.toml 仅作为可选读取源覆盖 config.json，写回永远落 JSON
_TOML_CONFIG_FILE = os.path.join(YCODE_DIR, "config.toml")
_TOML_CACHE: dict[str, dict] = {}

# 局域网手机远程控制的配对 token 过期时间（秒）
REMOTE_TOKEN_TTL_KEY = "remote_token_ttl"
REMOTE_TOKEN_TTL_DEFAULT = 86400
# 配对 token 与绝对过期时间戳（秒）的持久化键：应用重启后 token 保持固定
REMOTE_PAIR_TOKEN_KEY = "remote_pair_token"
REMOTE_PAIR_TOKEN_EXPIRES_KEY = "remote_pair_token_expires_at"

# 自动从当前源码目录初始化默认配置文件
if not os.path.exists(_CONFIG_FILE):
    default_config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
    if os.path.exists(default_config_path):
        try:
            shutil.copy(default_config_path, _CONFIG_FILE)
            # 源码目录遗留的 config.json 会被复制为用户配置，必须留下日志而非静默搬运
            _log_warning("已从源码目录 %s 复制初始配置到 %s", default_config_path, _CONFIG_FILE)
        except Exception:
            pass
    if not os.path.exists(_CONFIG_FILE):
        try:
            with open(_CONFIG_FILE, "w", encoding="utf-8") as f:
                default_data = {
                    "active_model_id": "ds-v4-pro",
                    "command_shell": "auto",
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
                                    "supports_vision": False
                                },
                                {
                                    "id": "ds-v4-flash",
                                    "name": "deepseek-v4-flash",
                                    "litellm_model": "openai/deepseek-v4-flash",
                                    "context_window": 1000000,
                                    "supports_vision": False
                                }
                            ]
                        }
                    ]
                }
                json.dump(default_data, f, indent=2, ensure_ascii=False)
        except Exception:
            pass

def _deep_merge(base, overlay):
    """深合并：overlay 优先。dict 递归合并，list/标量整体替换；返回新对象，不污染入参。"""
    if isinstance(base, dict) and isinstance(overlay, dict):
        merged = dict(base)
        for key, value in overlay.items():
            merged[key] = _deep_merge(merged[key], value) if key in merged else value
        return merged
    return overlay


def _read_toml_overlay(path):
    """只读 TOML overlay；文件不存在或解析失败一律返回 None，绝不影响主流程。"""
    if tomllib is None:
        return None
    try:
        current_mtime = os.path.getmtime(path)
    except OSError:
        with _CONFIG_LOCK:
            _TOML_CACHE.pop(path, None)
        return None
    # 缓存探测/提交必须持锁：mtime 与 data 两字段分两次写入，无锁并发下
    # 检查-写入交错会让读方拿到 mtime 与内容不配套的条目
    with _CONFIG_LOCK:
        cache_entry = _TOML_CACHE.setdefault(path, {"mtime": 0, "data": None})
        if cache_entry["data"] is not None and cache_entry["mtime"] == current_mtime:
            return _copy_cached(cache_entry["data"])
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except (tomllib.TOMLDecodeError, OSError) as error:
        _log_warning("config.toml 读取/解析失败（%s），本次忽略 TOML 配置，回退纯 JSON", error)
        with _CONFIG_LOCK:
            _TOML_CACHE.pop(path, None)
        return None
    if not isinstance(data, dict):
        _log_warning("config.toml 顶层不是 TOML 表，忽略该文件")
        with _CONFIG_LOCK:
            _TOML_CACHE.pop(path, None)
        return None
    with _CONFIG_LOCK:
        cache_entry["mtime"] = current_mtime
        cache_entry["data"] = data
    return _copy_cached(data)


def _warn_toml_overridden_keys(keys):
    """config.toml 是只读覆盖层且读取时后合并（overlay 优先）：JSON 写入成功后，
    若被改键同时也在 TOML 中定义，实际生效值仍是 TOML——保存"成功却不生效"
    必须告警留痕，绝不静默。"""
    try:
        candidates = list(keys)
        if not candidates:
            return
        overlay = _read_toml_overlay(_TOML_CONFIG_FILE)
        if not overlay:
            return
        overridden = sorted(str(key) for key in candidates if key in overlay)
        if overridden:
            _log_warning(
                "config.toml 中定义了键 [%s]，刚写入 config.json 的同名值在下次读取时仍会被 "
                "TOML 覆盖而不生效；如需修改生效值请编辑或删除 config.toml 中的对应键",
                ", ".join(overridden),
            )
    except Exception:
        # 告警只是提示性诊断，绝不能反过来影响保存主流程
        pass


def _load():
    data = _read_json_with_backup(_CONFIG_FILE, default=None)
    result = data if isinstance(data, dict) else {}
    overlay = _read_toml_overlay(_TOML_CONFIG_FILE)
    if overlay:
        result = _deep_merge(result, overlay)
    return result


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
        if info:
            return info.get("litellm_model", "")
        if "providers" not in data:
            return data.get("model", "openai/deepseek-v4-flash")
        return ""
    if name == "MY_KEY":
        if info:
            return info.get("api_key", "")
        if "providers" not in data:
            return data.get("api_key", "")
        return ""
    if name == "MY_BASE_URL":
        if info:
            return info.get("base_url", "")
        if "providers" not in data:
            return data.get("base_url", "https://api.deepseek.com")
        return ""
    if name == "MY_CONTEXT_WINDOW":
        if info:
            return info.get("context_window", 128000)
        return 128000
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
    返回值为实际写入的配置 dict。mutator 改动了同时被 config.toml 覆盖的键时，
    写入运行时告警（TOML 读取时后合并，实际生效值仍以 TOML 为准）。
    """
    with _CONFIG_LOCK:
        data = _load()
        before = deepcopy(data)
        result = mutator(data)
        if result is not None:
            data = result
        _atomic_write_json(_CONFIG_FILE, data)
    changed_keys = {
        key
        for key in set(before) | set(data)
        if key not in before or key not in data or before[key] != data[key]
    }
    _warn_toml_overridden_keys(changed_keys)
    return data


def save_config_value(key, value):
    """保存配置项并持久化写入 config.json（原子写 + 进程内锁）。

    该键同时被 config.toml 覆盖时写入运行时告警：TOML 读取时后合并，
    保存"成功"但实际生效值仍以 TOML 为准。
    """
    try:
        with _CONFIG_LOCK:
            data = _load()
            data[key] = value
            _atomic_write_json(_CONFIG_FILE, data)
        _warn_toml_overridden_keys([key])
        return True
    except Exception as e:
        # 调用方普遍不检查返回值，失败必须留下诊断痕迹而不是静默丢配置
        _log_warning("save_config_value(%r) 写入 config.json 失败: %s", key, e)
        return False

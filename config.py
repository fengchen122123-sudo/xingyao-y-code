import difflib
import glob
import json
import logging
import os
import shutil
import threading
import time
from copy import deepcopy

from core.credentials import DPAPI_PREFIX, KEYRING_PREFIX, protect_secret, unprotect_secret
from core.managed_policy import (
    apply_managed_policy,
    enforce_managed_writes,
    load_managed_policy,
)

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


def _config_read_failed(path):
    """主文件存在且有内容，但解析不出 JSON：属于「读失败」，不是首次运行。

    写路径用它做最后一道闸：读不出来时宁可拒绝保存，也不能拿空配置覆盖用户数据
    （历史事故：读失败 → load_config 写默认 → 17 个厂商配置被 1 个默认厂商替换）。
    """
    try:
        if not os.path.exists(path) or os.path.getsize(path) == 0:
            return False
        with open(path, "r", encoding="utf-8") as f:
            json.load(f)
        return False
    except (OSError, json.JSONDecodeError):
        return True


def _quarantine_corrupt_config(path):
    """把无法解析的配置留一份现场副本（只复制、不改动原文件），便于事后人工恢复。"""
    target = f"{path}.corrupt-{time.strftime('%Y%m%d%H%M%S')}"
    try:
        shutil.copy2(path, target)
        _log_warning("%s 读取失败，已保留现场副本 %s", os.path.basename(path), target)
    except OSError as error:
        _log_warning("%s 读取失败，现场副本保存失败（%s）", os.path.basename(path), error)


# 备份恢复的顶层类型契约：不同用途的 JSON 文件顶层结构不同，必须按文件逐一定义，
# 不能一刀切放宽成「dict 或 list 都算可用」。
#   - config.json 等配置型文件是对象：主文件被误写成 list 时，若把 list 备份当配置
#     恢复，就会用非法结构覆盖用户配置，所以仍要求非空 dict；
#   - active_skills.json / builtin_skills_released.json 本身以 list 存储，沿用
#     「非空 dict」判据会把唯一可用的备份判为无效，导致激活技能/释放记录静默清空。
_LIST_TYPED_JSON_FILES = {"active_skills.json", "builtin_skills_released.json"}


def _expected_json_root_type(path):
    """该文件备份恢复时要求的 JSON 顶层类型（dict / list）。

    按文件名判定而非按调用方传参，是为了让所有既有读取点（含测试里 monkeypatch
    出来的临时路径）无需改动就拿到正确契约；未知文件默认按配置型（dict）对待，
    保持本次修复前的严格行为。
    """
    return list if os.path.basename(path) in _LIST_TYPED_JSON_FILES else dict


def _backup_data_usable(data, expected_type):
    """备份内容是否可用：顶层类型符合该文件用途，且非空。

    空对象/空列表视作不可用（与修复前「非空 dict」口径一致），继续向更早的备份回溯，
    避免用一个空备份覆盖掉仍有内容的现场。
    """
    return isinstance(data, expected_type) and bool(data)


def _recover_from_history_backups(path):
    """按 .bak、config.json.bak.*（按修改时间倒序）找第一份可用配置。

    单一 .bak 会在每次写入时被刷新，一旦主文件被错误覆盖，.bak 也可能是坏数据；
    历史命名备份（*.bak.2026xxxx-xxx）是最后一道兜底。
    """
    expected_type = _expected_json_root_type(path)
    candidates = [path + ".bak"]
    try:
        candidates.extend(
            sorted(
                glob.glob(path + ".bak.*"),
                key=lambda candidate: os.path.getmtime(candidate),
                reverse=True,
            )
        )
    except OSError:
        pass
    for candidate in candidates:
        try:
            with open(candidate, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        if _backup_data_usable(data, expected_type):
            _log_warning("%s 读取失败，已从备份 %s 恢复", os.path.basename(path), candidate)
            return _restore_after_read(path, data)
    return None


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
        # 主文件坏了：先留现场副本，再从 .bak / 历史备份里找一份可用的回写自愈。
        # 找不到可用备份时保持原文件不动并返回 default——绝不在这里写默认配置，
        # 否则一次瞬时读取失败就会把用户的厂商/模型配置覆盖成新手引导结构。
        data = _recover_from_history_backups(path)
        # 判据与备份筛选一致：只有「类型符合该文件用途的非空数据」才回写自愈，
        # list 型文件（active_skills.json 等）恢复到的 list 同样要走回写，不能被
        # 固定写成 isinstance(data, dict) 挡在门外。
        if _backup_data_usable(data, _expected_json_root_type(path)):
            _quarantine_corrupt_config(path)
            try:
                # 对配置写路径加锁，避免与正常写路径竞态覆盖新配置
                with _CONFIG_LOCK:
                    _atomic_write_json(path, data)
            except Exception as error:
                _log_warning("%s 从备份恢复后回写失败（%s），本次仅在内存中使用备份内容", os.path.basename(path), error)
            return data
        _log_warning("%s 读取失败且没有可用备份，本次不覆盖原文件", os.path.basename(path))
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
# 配对端口持久化键：重启后复用同一端口，二维码 URL 不变、无需重新扫码
REMOTE_PAIR_PORT_KEY = "remote_pair_port"
# 局域网 MCP 服务（供手机端 MCP 客户端如阶跃 Amoo 调用）：默认开启，与远程控制共用配对 token
REMOTE_MCP_ENABLED_KEY = "remote_mcp_enabled"
REMOTE_MCP_ENABLED_DEFAULT = True
REMOTE_MCP_PORT_KEY = "remote_mcp_port"
REMOTE_MCP_PORT_DEFAULT = 8765

# 飞书通道（在飞书里与星瑶对话）：默认关闭，未配置时不影响任何现有功能。
# app_secret 写盘前经 core.credentials 加密（dpapi:/keyring: 前缀），读取时解密。
FEISHU_ENABLED_KEY = "feishu_enabled"
FEISHU_ENABLED_DEFAULT = False
FEISHU_APP_ID_KEY = "feishu_app_id"
FEISHU_APP_ID_DEFAULT = ""
FEISHU_APP_SECRET_KEY = "feishu_app_secret"
FEISHU_APP_SECRET_DEFAULT = ""
# 审批白名单（open_id 列表）；为空时审批卡不渲染按钮
FEISHU_APPROVERS_KEY = "feishu_approvers"
FEISHU_APPROVERS_DEFAULT: tuple = ()
# 固定推送落点（群/单聊 chat_id，oc_ 开头）；为空时推给最后说话的会话
FEISHU_NOTIFY_CHAT_ID_KEY = "feishu_notify_chat_id"
FEISHU_NOTIFY_CHAT_ID_DEFAULT = ""

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
                            "protocol": "openai",
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
                                    "id": "ds-flash",
                                    "name": "deepseek-flash",
                                    "litellm_model": "openai/deepseek-flash",
                                    "context_window": 1000000,
                                    "supports_vision": True
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


def _load(managed=True):
    """读取有效配置（JSON + TOML overlay 合并）；``managed=False`` 供写路径读用户原值。

    受管策略（企业/团队管理员基线）在返回前套用：``permission_mode`` 超出上限时
    降档并留警告。受管文件缺失时结果与改动前逐字节一致。
    """
    data = _read_json_with_backup(_CONFIG_FILE, default=None)
    result = data if isinstance(data, dict) else {}
    overlay = _read_toml_overlay(_TOML_CONFIG_FILE)
    if overlay:
        result = _deep_merge(result, overlay)
    if managed:
        try:
            result = apply_managed_policy(result, load_managed_policy())
        except Exception as error:
            # 受管策略是管理员基线，读取/套用异常不能反过来影响主流程（按无策略继续）
            _log_warning("受管策略读取/套用失败（%s），本次按无策略继续", error)
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
    受管策略锁定的键（permission_mode 超上限）变更被拒绝（回退写入前值）并提示。
    """
    with _CONFIG_LOCK:
        data = _load(managed=False)
        if _config_read_failed(_CONFIG_FILE):
            _log_warning(
                "config.json 当前无法解析且无可用备份，已拒绝本次写入，避免覆盖用户配置"
            )
            return data
        before = deepcopy(data)
        result = mutator(data)
        if result is not None:
            data = result
        data, _rejected = enforce_managed_writes(data, before)
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
    受管策略锁定的键（permission_mode 超出管理员上限）拒绝写入并提示"由管理员锁定"。
    """
    try:
        with _CONFIG_LOCK:
            data = _load(managed=False)
            if _config_read_failed(_CONFIG_FILE):
                _log_warning(
                    "config.json 当前无法解析且无可用备份，已拒绝写入 %r，避免覆盖用户配置", key
                )
                return False
            before = dict(data) if isinstance(data, dict) else {}
            data[key] = value
            data, rejected = enforce_managed_writes(data, before)
            _atomic_write_json(_CONFIG_FILE, data)
        _warn_toml_overridden_keys([key])
        return key not in rejected
    except Exception as e:
        # 调用方普遍不检查返回值，失败必须留下诊断痕迹而不是静默丢配置
        _log_warning("save_config_value(%r) 写入 config.json 失败: %s", key, e)
        return False


def save_nested_config_value(dotted_key, value):
    """保存两层点分路径配置项（如 "memory_compression.watermark_base_tokens"）。

    读-改-写整份 config.json：只改动目标子键，其他所有键（含 providers / theme）
    原样保留；父级不存在或不是 dict 时创建空 dict。
    与 save_config_value 同风格：复用 _CONFIG_LOCK 与 _atomic_write_json，
    异常时记运行告警并返回 False，绝不把异常抛给调用方。
    """
    try:
        parts = [part for part in str(dotted_key).split(".") if part]
        if len(parts) != 2:
            raise ValueError(f"只支持两层点分路径，收到 {dotted_key!r}")
        parent_key, child_key = parts
        with _CONFIG_LOCK:
            data = _load(managed=False)
            if _config_read_failed(_CONFIG_FILE):
                _log_warning(
                    "config.json 当前无法解析且无可用备份，已拒绝写入 %r，避免覆盖用户配置",
                    dotted_key,
                )
                return False
            if not isinstance(data, dict):
                data = {}
            before = dict(data)
            parent = data.get(parent_key)
            if not isinstance(parent, dict):
                parent = {}
                data[parent_key] = parent
            parent[child_key] = value
            data, rejected = enforce_managed_writes(data, before)
            _atomic_write_json(_CONFIG_FILE, data)
        # config.toml 是只读覆盖层（读取时后合并）：同名子键会盖掉刚写入的值，
        # 保存"成功却不生效"必须留痕
        overlay = _read_toml_overlay(_TOML_CONFIG_FILE)
        if isinstance(overlay, dict):
            section = overlay.get(parent_key)
            if isinstance(section, dict) and child_key in section:
                _log_warning(
                    "config.toml 中定义了键 [%s]，刚写入 config.json 的同名值在下次读取时"
                    "仍会被 TOML 覆盖而不生效；如需修改生效值请编辑或删除 config.toml 中的对应键",
                    dotted_key,
                )
        return parent_key not in rejected
    except Exception as e:
        # 调用方普遍不检查返回值，失败必须留下诊断痕迹而不是静默丢配置
        _log_warning("save_nested_config_value(%r) 写入 config.json 失败: %s", dotted_key, e)
        return False


# ---------------------------------------------------------------------------
# T2 配置未知键校验：手改 config.json 拼错字段名时给出可见告警（此前静默无效）。
# 只校验「键名」，不做类型/取值校验，也不触碰任何写路径。
# 已知键来源 = config.example.json + config.py 默认结构 + 各模块运行时读写键的并集。
# ---------------------------------------------------------------------------

KNOWN_CONFIG_KEYS = frozenset({
    # 模型与厂商
    "active_model_id", "secondary_model_id", "secondary_model_roles", "providers",
    # 旧版单模型格式（_resolve_constant 兼容读取 model/api_key/base_url）
    "model", "api_key", "base_url",
    # 权限与工作区
    "permission_mode", "allow_outside_workspace",
    # 运行与界面
    "theme", "gui_theme_light", "gui_theme_id",
    "command_shell", "shell_env_policy",
    "agent_turn_step_limit", "prompt_cache_keepalive_seconds",
    "mermaid_auto_open", "read_file_result_max_chars",
    # 断点续跑总开关（S4）：只登记键名，缺省=关闭；关闭时状态机、事件序列、
    # 请求字节与改动前完全一致（规格 §9.5）
    "resume",
    # 记忆/图片压缩
    "memory_compression", "image_compression",
    # 工具、安全与数据目录
    "search_exclude_dirs", "skill_search_paths",
    "fetch_url_allow_hosts", "fetch_url_strict_ssrf",
    "expose_mcp_admin_tools", "project_hook_full_input",
    "ai_output_dir",
    # 电脑操控
    "browser_control_enabled", "computer_control_enabled", "computer_browser",
    # 局域网远程控制
    "remote_token_ttl", "remote_pair_token", "remote_pair_token_expires_at",
    "remote_pair_port",
    # 局域网 MCP 服务（供手机端 MCP 客户端调用；未登记会在 strict_config=true
    # 下被 UnknownConfigKeysError 阻断启动）
    "remote_mcp_enabled", "remote_mcp_port",
    # 飞书通道（在飞书里与星瑶对话）
    "feishu_enabled", "feishu_app_id", "feishu_app_secret", "feishu_approvers",
    "feishu_notify_chat_id",
    # 数据迁移
    "media_v1_migration_done",
    # 本任务新增：严格模式开关
    "strict_config",
    # 缓存路由显式配置（core/route_overrides.py CONFIG_KEY、core/llm_dialects.py
    # _KEEPALIVE_ROUTES_CONFIG_KEY）、保活
    # 证据门开关（_KEEPALIVE_EVIDENCE_GATE_CONFIG_KEY）与 prompt_cache_key
    # 白名单（_AFFINITY_LANES_CONFIG_KEY）。
    # deepseek_cache_routes：已废弃不再读取（ledger 改纯模型家族判定），保留
    # 键仅为旧 config.json 兼容（写入无任何效果，不会触发未知键告警）。
    "route_request_overrides", "cache_keepalive_routes", "deepseek_cache_routes",
    "cache_keepalive_evidence_gate", "cache_affinity_lanes",
})

# 已知嵌套对象（两层）允许的子键：顶层键 -> 子键集合
KNOWN_NESTED_CONFIG_KEYS = {
    "secondary_model_roles": frozenset({"vision", "compression", "subagent"}),
    # 断点续跑（默认关闭）：只允许 enabled 一个子键，拼错时启动告警可见
    "resume": frozenset({"enabled"}),
    "image_compression": frozenset({"max_edge", "quality", "max_output_bytes"}),
    "memory_compression": frozenset({
        "retain_count", "retain_count_force",
        "tool_result_truncate_threshold_chars", "tool_result_preview_limit_chars",
        "tool_result_token_limit", "summary_length_first", "summary_length_merge",
        "max_chunk_token_budget", "inter_chunk_delay_seconds", "max_total_seconds",
        "precompress_ratio",
        # 水位线计算基准上限（tokens）：core/memory_compressor 读取、core/context_watermark
        # 读写。漏登记会让启动误报「未知配置键（不会生效）」，strict_config 下还会阻断启动。
        "watermark_base_tokens", "model_watermark_bases",
    }),
    "shell_env_policy": frozenset({
        "inherit", "exclude_sensitive", "exclude", "include_only", "set",
    }),
}

# providers[] 与 providers[].models[] 条目的已知键（含可选扩展键）
_KNOWN_PROVIDER_KEYS = frozenset({
    "id", "name", "base_url", "api_key", "protocol", "preset_id", "models",
    # Qwen 缓存策略可挂在 provider 级（core.llm_dialects._QWEN_CACHE_CONFIG_KEYS）
    "enable_prompt_cache", "prompt_cache", "enable_cache_control", "cache_control",
    # 历史功能键：dd839a0（2026-09-19）前的「预设模型删除痕迹」功能已移除，
    # 老配置可能残留此键，按兼容保留（不报未知，避免 strict 模式误阻断启动）。
    "removed_preset_models",
    # 插件来源标记：插件自举 provider 时写入（core/plugin_loader 据此在插件禁用/
    # 删除时精确回收，绝不误删用户手配厂商）。属于运行时合法键，不登记会让装过
    # 插件的用户在 strict_config=true 下被 UnknownConfigKeysError 阻断启动。
    "managed_by",
})
_KNOWN_MODEL_KEYS = frozenset({
    "id", "name", "litellm_model", "context_window", "supports_vision",
    "supports_function_calling", "max_tokens", "thinking_budget", "enable_thinking",
    "reasoning_effort", "reasoning_choice", "computer_protocol", "system_prompt_extra",
    "enable_prompt_cache", "prompt_cache", "enable_cache_control", "cache_control",
})


class UnknownConfigKeysError(Exception):
    """strict_config=true 且存在未知配置键：拒绝启动。"""


def _collect_unknown_key_entries(mapping, allowed, prefix, found):
    for key in mapping:
        if key not in allowed:
            path = f"{prefix}.{key}" if prefix else str(key)
            found.append((path, allowed))


def _unknown_config_key_entries(data):
    """收集 (点分路径, 同层已知键集合) 二元组；校验是防御性诊断，绝不抛错。"""
    found = []
    if not isinstance(data, dict):
        return found
    for key, value in data.items():
        if key not in KNOWN_CONFIG_KEYS:
            # 顶层未知键只报根因：不再递归其子键，避免一个拼写错误刷出一串噪音
            found.append((str(key), KNOWN_CONFIG_KEYS))
            continue
        if key == "providers":
            if isinstance(value, list):
                for index, provider in enumerate(value):
                    if not isinstance(provider, dict):
                        continue
                    prefix = f"providers[{index}]"
                    _collect_unknown_key_entries(provider, _KNOWN_PROVIDER_KEYS, prefix, found)
                    models = provider.get("models")
                    if isinstance(models, list):
                        for model_index, model in enumerate(models):
                            if isinstance(model, dict):
                                _collect_unknown_key_entries(
                                    model,
                                    _KNOWN_MODEL_KEYS,
                                    f"{prefix}.models[{model_index}]",
                                    found,
                                )
            continue
        allowed_children = KNOWN_NESTED_CONFIG_KEYS.get(key)
        if allowed_children is not None and isinstance(value, dict):
            _collect_unknown_key_entries(value, allowed_children, str(key), found)
    return found


def find_unknown_config_keys(data):
    """返回配置 dict 中未知键的点分路径列表（如 "memory_compression.retain_cout"）。

    覆盖顶层、已知嵌套对象两层、providers[]/providers[].models[] 条目；
    非 dict 输入返回空列表。
    """
    return [path for path, _ in _unknown_config_key_entries(data)]


def _format_unknown_config_key_detail(found):
    """把未知键格式化为「路径（是否想写 xxx？）」提示串（difflib 近似匹配）。"""
    parts = []
    for path, allowed in found:
        leaf = path.rsplit(".", 1)[-1]
        matches = difflib.get_close_matches(leaf, sorted(allowed), n=1, cutoff=0.6)
        if matches:
            parts.append(f"{path}（是否想写 {matches[0]}？）")
        else:
            parts.append(path)
    return "、".join(parts)


def audit_config_keys(data=None):
    """启动自检：未知配置键默认写运行告警；strict_config=true 时抛错阻断启动。

    返回未知键路径列表；strict 命中时抛 UnknownConfigKeysError（不返回）。
    只读诊断：绝不改写 config.json（写路径行为保持不变）。
    """
    if data is None:
        data = _load()
    found = _unknown_config_key_entries(data)
    if not found:
        return []
    detail = _format_unknown_config_key_detail(found)
    if isinstance(data, dict) and data.get("strict_config") is True:
        raise UnknownConfigKeysError(
            f"config.json 存在 {len(found)} 个未知配置键：{detail}。"
            "已开启 strict_config=true，为防止拼写错误的配置静默无效，启动已中止。"
        )
    _log_warning(
        "config.json 存在 %d 个未知配置键（不会生效）：%s。请检查拼写；"
        "如需忽略请删除这些键，或设置 strict_config=true 让程序在存在未知键时拒绝启动",
        len(found),
        detail,
    )
    return [path for path, _ in found]

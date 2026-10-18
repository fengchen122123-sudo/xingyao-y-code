"""JEV 决策服务核心：配置 / Key 策略 / 客户端 / 影子 / 审计 / 探针。

设计依据：docs/superpowers/specs/2026-10-15-jev-decision-service-design.md。

安全约束（硬性）：
- 默认全关：``enabled=false`` 时不产生任何网络请求与副作用；
- Key 只在内存中解密使用，**不落盘、不进日志、不进审计**（审计只记第几把 key_hint）；
- 任何运行时异常一律不外抛，调用方永远拿到 None 走原逻辑；
- 不新增依赖（httpx 已有）。

测试隔离辅助：``_reset_runtime_state()`` 仅清空进程内 Key 状态，供用例调用。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import uuid
from copy import deepcopy
from datetime import datetime
from urllib.parse import urlsplit

import httpx

from core.credentials import unprotect_secret

# ---------------------------------------------------------------------------
# 默认配置（设计文档第 3 节全表）
# ---------------------------------------------------------------------------

DEFAULT_CONFIG: dict = {
    "version": 1,
    "enabled": False,
    "provider_ref": "auto",
    "base_url": "",
    "model_id": "NeoHorse-Jev-4B",
    "endpoint_style": "decision",
    "key_mode": "auto",
    "shadow_mode": True,
    "timeout_ms": 800,
    "scenes": {
        # timeout_ms：业务决策的单次超时（网关读取；真实决策宁可不用也不拖慢主流程）
        "router": {"enabled": False, "shadow": True, "threshold": 0.5, "timeout_ms": 250},
        "secretary": {"enabled": False, "shadow": True, "threshold": 0.6, "timeout_ms": 250},
        "search": {"enabled": False, "shadow": True, "threshold": 0.5, "timeout_ms": 250},
        "safety": {"enabled": False, "shadow": True, "threshold": 0.9, "timeout_ms": 250},
    },
    "audit_enabled": True,
}

MAX_QUESTIONS = 16
MIN_TIMEOUT_MS = 100
MAX_TIMEOUT_MS = 5000
KEY_COOLDOWN_SECONDS = 60

# 探针的无害问题（每把 Key 至多 1 次调用，遵守基元律动技能红线：禁止压测）
_PROBE_STATE = "本请求为无害连通探针，不涉及任何用户数据。"
_PROBE_INSTRUCTIONS = "1+1 是否等于 2？请给出该命题成立的概率。"

# 单条审计记录的字节上限（设计第 5 节：单条 <2KB）
_AUDIT_MAX_BYTES = 1900
_AUDIT_LOCK = threading.Lock()

# Key 分隔符：换行（含 \r\n）、英文/中文逗号、分号（中英文）
_RE_KEY_SEPARATOR = re.compile(r"[\r\n,，;；]+")


# ---------------------------------------------------------------------------
# 路径与基础工具（惰性求值：模块导入零副作用）
# ---------------------------------------------------------------------------

def _ycode_dir() -> str:
    """用户数据目录：跟随 config.YCODE_DIR（测试可用 monkeypatch 重定向）。"""
    try:
        import config

        base = getattr(config, "YCODE_DIR", "") or ""
        if base:
            return str(base)
    except Exception:
        pass
    return os.environ.get("YCODE_DATA_DIR") or os.path.join(os.path.expanduser("~"), ".ycode")


def _decision_config_path() -> str:
    return os.path.join(_ycode_dir(), "decision.json")


def _user_config_path() -> str:
    return os.path.join(_ycode_dir(), "config.json")


def _audit_dir() -> str:
    return os.path.join(_ycode_dir(), "decision_audit")


def _now() -> float:
    """单调时钟（秒）；单独成函数便于测试推进冷却时间。"""
    return time.monotonic()


def _today() -> str:
    return datetime.now().strftime("%Y%m%d")


def _read_json(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _deep_merge(base, overlay):
    """深合并：overlay 优先；dict 递归，其余整体替换；返回新对象，不污染入参。"""
    if isinstance(base, dict) and isinstance(overlay, dict):
        merged = dict(base)
        for key, value in overlay.items():
            merged[key] = _deep_merge(merged[key], value) if key in merged else deepcopy(value)
        return merged
    return deepcopy(overlay)


def _sha16(value) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except Exception:
        text = repr(value)
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]


def _state_digest(state) -> str:
    """state 摘要：字符串取前 80 字符，其余取哈希。"""
    if isinstance(state, str):
        return state if len(state) <= 80 else _sha16(state)
    return _sha16(state)


# ---------------------------------------------------------------------------
# 配置层（~/.ycode/decision.json 独立文件；损坏回落默认值=全关）
# ---------------------------------------------------------------------------

def load_decision_config() -> dict:
    """读取决策配置并与默认值深合并；缺失/损坏一律回落默认值（全关）。"""
    return _deep_merge(deepcopy(DEFAULT_CONFIG), _read_json(_decision_config_path()))


def _validate_config(cfg: dict) -> None:
    """保存前校验；非法值抛 ValueError（桥层负责转 ok=false）。"""
    if not isinstance(cfg.get("enabled"), bool):
        raise ValueError("enabled 必须是布尔值")
    model_id = cfg.get("model_id")
    if cfg.get("enabled") and (not isinstance(model_id, str) or not model_id.strip()):
        raise ValueError("启用决策模型时 model_id 不能为空")
    timeout = cfg.get("timeout_ms")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise ValueError(f"timeout_ms 必须是 {MIN_TIMEOUT_MS}~{MAX_TIMEOUT_MS} 的整数")
    if not MIN_TIMEOUT_MS <= int(timeout) <= MAX_TIMEOUT_MS:
        raise ValueError(f"timeout_ms 必须在 {MIN_TIMEOUT_MS}~{MAX_TIMEOUT_MS} 之间")
    if cfg.get("endpoint_style") not in ("decision", "systemone"):
        raise ValueError('endpoint_style 只支持 "decision" 或 "systemone"')
    mode = cfg.get("key_mode")
    if mode != "auto":
        if not isinstance(mode, str) or not mode.startswith("index:"):
            raise ValueError('key_mode 只支持 "auto" 或 "index:N"')
        try:
            index = int(mode.split(":", 1)[1])
        except (TypeError, ValueError):
            raise ValueError('key_mode 的 "index:N" 中 N 必须是整数') from None
        if index < 0:
            raise ValueError('key_mode 的 "index:N" 中 N 必须 >= 0')
    if not isinstance(cfg.get("scenes"), dict):
        raise ValueError("scenes 必须是字典")


def save_decision_config(patch: dict) -> dict:
    """校验并原子写盘；返回保存后的完整配置。非法值抛 ValueError 且不落盘。"""
    if not isinstance(patch, dict):
        raise ValueError("配置 patch 必须是字典")
    merged = _deep_merge(load_decision_config(), patch)
    _validate_config(merged)
    import config

    config._atomic_write_json(_decision_config_path(), merged)
    return merged


# ---------------------------------------------------------------------------
# Key 层（provider 解析 + 多 Key 解密 + 进程内状态，加锁访问）
# ---------------------------------------------------------------------------

class _KeyState:
    """进程内 Key 运行状态（粘性 index / 403 拒绝 / 429 冷却），绝不落盘。"""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.signature: tuple = ()
        self.active_index = 0
        self.denied: set = set()
        self.cooldowns: dict = {}
        self.last_probe: dict | None = None


_KEY_STATE = _KeyState()


def _reset_runtime_state() -> None:
    """测试隔离辅助：清空进程内 Key 状态（产品代码不调用）。"""
    with _KEY_STATE.lock:
        _KEY_STATE.signature = ()
        _KEY_STATE.active_index = 0
        _KEY_STATE.denied.clear()
        _KEY_STATE.cooldowns.clear()
        _KEY_STATE.last_probe = None


def _sync_keys_locked(keys: list) -> None:
    """Key 列表变化时重置运行状态（调用方须已持有锁）。"""
    signature = tuple(keys)
    if signature != _KEY_STATE.signature:
        _KEY_STATE.signature = signature
        _KEY_STATE.active_index = 0
        _KEY_STATE.denied.clear()
        _KEY_STATE.cooldowns.clear()


def _load_user_config() -> dict:
    """读取 ~/.ycode/config.json（含 provider 列表）。

    优先走 config._read_json_with_backup（自带 .bak 自愈与 mtime 缓存）；读取路径
    被重定向导致解密转换不生效时，_load_keys 会显式调用 unprotect_secret 兜底。
    """
    path = _user_config_path()
    try:
        import config

        data = config._read_json_with_backup(path, default=None)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return _read_json(path)


def _resolve_provider(cfg: dict) -> dict | None:
    """按 provider_ref 定位用户 provider；auto = base_url 含 tokenrhythm 的第一个。"""
    try:
        providers = [
            item
            for item in (_load_user_config().get("providers") or [])
            if isinstance(item, dict)
        ]
        ref = cfg.get("provider_ref") or "auto"
        if isinstance(ref, str) and ref.strip() and ref.strip() != "auto":
            ref = ref.strip()
            for provider in providers:
                if provider.get("id") == ref or provider.get("name") == ref:
                    return provider
            return None
        for provider in providers:
            if "tokenrhythm" in str(provider.get("base_url") or "").lower():
                return provider
        return None
    except Exception:
        return None


def _resolve_base_url(cfg: dict, provider) -> str:
    override = cfg.get("base_url")
    if isinstance(override, str) and override.strip():
        return override.strip().rstrip("/")
    if isinstance(provider, dict):
        base = provider.get("base_url")
        if isinstance(base, str) and base.strip():
            return base.strip().rstrip("/")
    return ""


def _split_key_text(raw) -> list:
    """把 api_key 文本按分隔符拆为多段（空段/重复段丢弃）。"""
    if not isinstance(raw, str):
        return []
    result: list = []
    for part in _RE_KEY_SEPARATOR.split(raw):
        item = part.strip()
        if item and item not in result:
            result.append(item)
    return result


def _load_keys(cfg: dict, provider=None) -> list:
    """解密并返回 provider 的多 Key 列表；任何异常返回空列表。

    Key 只在内存中解密使用：本函数不缓存、不落盘、不写日志、不进审计。
    """
    try:
        if not isinstance(provider, dict):
            provider = _resolve_provider(cfg)
        if not isinstance(provider, dict):
            return []
        keys: list = []
        for part in _split_key_text(provider.get("api_key")):
            try:
                value = unprotect_secret(part)
            except Exception:
                continue
            for item in _split_key_text(value):
                if item not in keys:
                    keys.append(item)
        return keys
    except Exception:
        return []


def _candidate_indexes(keys: list, mode) -> list:
    """计算本次请求的候选 Key 下标（auto 环形，跳过已拒绝/冷却中的 Key）。"""
    if not keys:
        return []
    if isinstance(mode, str) and mode.startswith("index:"):
        try:
            index = int(mode.split(":", 1)[1])
        except (TypeError, ValueError):
            return []
        return [index] if 0 <= index < len(keys) else []
    with _KEY_STATE.lock:
        count = len(keys)
        start = _KEY_STATE.active_index % count
        now = _now()
        candidates: list = []
        for offset in range(count):
            index = (start + offset) % count
            if index in _KEY_STATE.denied:
                continue
            if _KEY_STATE.cooldowns.get(index, 0.0) > now:
                continue
            candidates.append(index)
        return candidates


def _no_candidate_reason(keys: list) -> str:
    if not keys:
        return "no_key"
    with _KEY_STATE.lock:
        now = _now()
        cooling = any(until > now for until in _KEY_STATE.cooldowns.values())
    return "rate_limited" if cooling else "no_permission"


def _remember_success(index: int) -> None:
    with _KEY_STATE.lock:
        _KEY_STATE.active_index = index
        _KEY_STATE.denied.discard(index)
        _KEY_STATE.cooldowns.pop(index, None)


def _mark_denied(index: int) -> None:
    with _KEY_STATE.lock:
        _KEY_STATE.denied.add(index)


def _mark_cooldown(index: int) -> None:
    with _KEY_STATE.lock:
        _KEY_STATE.cooldowns[index] = _now() + KEY_COOLDOWN_SECONDS


def _apply_probe_state(index: int, status: str) -> None:
    if status == "available":
        _remember_success(index)
    elif status in ("no_permission", "invalid_key"):
        _mark_denied(index)
    elif status == "rate_limited":
        _mark_cooldown(index)


# ---------------------------------------------------------------------------
# 客户端层（httpx、非流式；_post / _get 独立函数便于测试 monkeypatch）
# ---------------------------------------------------------------------------

def _endpoint_url(base_url: str, endpoint_style) -> str:
    suffix = "systemone" if endpoint_style == "systemone" else "decision"
    return base_url.rstrip("/") + "/" + suffix


def _headers(key, url: str) -> dict:
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    try:
        from core.llm_policy import tokenrhythm_agent_headers

        headers.update(tokenrhythm_agent_headers(url))
    except Exception:
        pass
    return headers


def _post(url: str, headers: dict, payload: dict, timeout_seconds: float):
    """执行一次非流式 POST；异常由调用方处理（绝不记录 Key）。"""
    with httpx.Client(timeout=timeout_seconds) as client:
        return client.post(url, headers=headers, json=payload)


def _get(url: str, timeout_seconds: float):
    """执行一次 GET（上游 /api/models 公开接口）。"""
    with httpx.Client(timeout=timeout_seconds) as client:
        return client.get(url, headers=_headers(None, url))


def _effective_timeout_ms(cfg: dict, override=None) -> int:
    value = override if override is not None else cfg.get("timeout_ms")
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = DEFAULT_CONFIG["timeout_ms"]
    return max(MIN_TIMEOUT_MS, min(MAX_TIMEOUT_MS, value))


# ---------------------------------------------------------------------------
# 审计层（~/.ycode/decision_audit/YYYYMMDD.jsonl，字段见设计第 5 节）
# ---------------------------------------------------------------------------

def _new_record(cfg: dict, scene: str, shadow: bool, trace_id: str) -> dict:
    return {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "scene": scene,
        "shadow": bool(shadow),
        "model_id": cfg.get("model_id"),
        "key_hint": None,
        "state_digest": None,
        "questions_digest": None,
        "answers": None,
        "latency_ms": None,
        "adopted": False,
        "fallback_reason": None,
        "trace_id": trace_id,
    }


def _cap_record(record: dict) -> dict:
    """保证单条记录 <2KB：超限时依次把 answers / extra 换成摘要。"""
    try:
        if len(json.dumps(record, ensure_ascii=False).encode("utf-8")) <= _AUDIT_MAX_BYTES:
            return record
        capped = dict(record)
        capped["answers"] = {"truncated": True, "digest": _sha16(record.get("answers"))}
        if len(json.dumps(capped, ensure_ascii=False).encode("utf-8")) > _AUDIT_MAX_BYTES:
            capped["extra"] = {"truncated": True, "digest": _sha16(record.get("extra"))}
        return capped
    except Exception:
        return record


# 审计事件监听器：桥侧注册后把「决策已记录」实时转发给前端（徽章/面板实时刷新）。
# listener 在审计写入成功后同步调用；任何异常都被吞掉，绝不影响决策主流程。
_AUDIT_LISTENERS: list = []
_AUDIT_LISTENERS_LOCK = threading.Lock()


def add_audit_listener(callback) -> None:
    """注册审计监听器（幂等；多播；异常隔离）。"""
    if callback is None:
        return
    with _AUDIT_LISTENERS_LOCK:
        if callback not in _AUDIT_LISTENERS:
            _AUDIT_LISTENERS.append(callback)


def _notify_audit_listeners(brief: dict) -> None:
    with _AUDIT_LISTENERS_LOCK:
        listeners = list(_AUDIT_LISTENERS)
    for callback in listeners:
        try:
            callback(dict(brief))
        except Exception:
            pass


def _append_audit(record: dict, enabled: bool) -> None:
    if not enabled:
        return
    try:
        directory = _audit_dir()
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, f"{_today()}.jsonl")
        line = json.dumps(_cap_record(record), ensure_ascii=False)
        with _AUDIT_LOCK:
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        # 写入成功后实时广播（前端徽章/面板刷新用；消费方自行兜底异常）
        _notify_audit_listeners(_audit_brief(record))
    except Exception:
        pass


def _rewrite_lines(path: str, lines: list) -> None:
    tmp_path = f"{path}.{os.getpid()}.tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as handle:
            handle.writelines(lines)
        os.replace(tmp_path, path)
    finally:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass


def mark_adopted(trace_id) -> None:
    """调用方采纳决策时按 trace_id 就地回填审计 adopted=true。"""
    if not isinstance(trace_id, str) or not trace_id:
        return
    try:
        directory = _audit_dir()
        if not os.path.isdir(directory):
            return
        names = sorted(
            (name for name in os.listdir(directory) if name.endswith(".jsonl")),
            reverse=True,
        )
        with _AUDIT_LOCK:
            for name in names:
                path = os.path.join(directory, name)
                try:
                    with open(path, "r", encoding="utf-8") as handle:
                        lines = handle.readlines()
                except OSError:
                    continue
                changed = False
                for position, line in enumerate(lines):
                    try:
                        record = json.loads(line)
                    except Exception:
                        continue
                    if isinstance(record, dict) and record.get("trace_id") == trace_id:
                        record["adopted"] = True
                        lines[position] = json.dumps(record, ensure_ascii=False) + "\n"
                        changed = True
                if changed:
                    _rewrite_lines(path, lines)
                    return
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 主入口：ask_decision
# ---------------------------------------------------------------------------

def _final_reason(failures: list) -> str:
    if not failures:
        return "upstream_error"
    if all(item == "rate_limited" for item in failures):
        return "rate_limited"
    if all(item == "unauthorized" for item in failures):
        return "unauthorized"
    if "no_permission" in failures:
        return "no_permission"
    return failures[-1]


def ask_decision(state, questions, *, scene, timeout_ms=None, image_b64=None, audit_extra=None):
    """向 JEV 发一次决策请求。

    - 禁用 / 场景未启用：直接 None（零请求、零副作用）；
    - 影子：正常发请求并写审计（shadow=true），但返回 None；
    - 成功且非影子：返回上游响应 dict（附带 trace_id 供 mark_adopted 回填）；
    - 失败（403/401/429/超时/网络/5xx/无 Key）：None + 审计记 fallback_reason。
    """
    cfg = load_decision_config()
    if not bool(cfg.get("enabled")):
        return None
    scene_name = scene if isinstance(scene, str) else str(scene or "")
    scenes = cfg.get("scenes") if isinstance(cfg.get("scenes"), dict) else {}
    scene_cfg = scenes.get(scene_name)
    if not isinstance(scene_cfg, dict) or not bool(scene_cfg.get("enabled")):
        return None
    if not isinstance(questions, dict) or not questions:
        raise ValueError("questions 必须是非空字典")
    if len(questions) > MAX_QUESTIONS:
        raise ValueError(
            f"单次最多 {MAX_QUESTIONS} 题（当前 {len(questions)} 题），请由上层拆批"
        )
    if image_b64 is not None and len(questions) > 1:
        raise ValueError("带图请求一次只允许 1 题")

    shadow = bool(cfg.get("shadow_mode")) or bool(scene_cfg.get("shadow", True))
    audit_enabled = bool(cfg.get("audit_enabled", True))
    trace_id = uuid.uuid4().hex[:12]
    timeout_seconds = _effective_timeout_ms(cfg, timeout_ms) / 1000.0
    record = _new_record(cfg, scene_name, shadow, trace_id)
    record["state_digest"] = _state_digest(state)
    record["questions_digest"] = _sha16(questions)
    if isinstance(audit_extra, dict) and audit_extra:
        # 调用方附加上下文（如启发式基线档 baseline_tier）：供影子模式后续
        # 一致性对比分析；体积由 _cap_record 统一兜底
        record["extra"] = dict(list(audit_extra.items())[:8])

    try:
        provider = _resolve_provider(cfg)
        base_url = _resolve_base_url(cfg, provider)
        keys = _load_keys(cfg, provider)
        if not keys:
            record["fallback_reason"] = "no_key"
            _append_audit(record, audit_enabled)
            return None
        if not base_url:
            record["fallback_reason"] = "no_base_url"
            _append_audit(record, audit_enabled)
            return None

        with _KEY_STATE.lock:
            _sync_keys_locked(keys)
        candidates = _candidate_indexes(keys, cfg.get("key_mode") or "auto")
        if not candidates:
            record["fallback_reason"] = _no_candidate_reason(keys)
            _append_audit(record, audit_enabled)
            return None

        model_id = str(cfg.get("model_id") or "")
        payload = {"model": model_id, "state": state, "questions": questions}
        if image_b64 is not None:
            payload["image"] = image_b64
        url = _endpoint_url(base_url, cfg.get("endpoint_style") or "decision")

        failures: list = []
        for index in candidates:
            started = _now()
            try:
                response = _post(url, _headers(keys[index], url), payload, timeout_seconds)
            except Exception:
                # 超时/网络异常：直接回落（时延优先，不重试）
                record["key_hint"] = f"key#{index}"
                record["latency_ms"] = int((_now() - started) * 1000)
                record["fallback_reason"] = "upstream_error"
                _append_audit(record, audit_enabled)
                return None

            status = getattr(response, "status_code", 0)
            record["key_hint"] = f"key#{index}"
            record["latency_ms"] = int((_now() - started) * 1000)

            if status == 200:
                try:
                    body = response.json()
                except Exception:
                    body = None
                if not isinstance(body, dict):
                    record["fallback_reason"] = "upstream_error"
                    _append_audit(record, audit_enabled)
                    return None
                _remember_success(index)
                record["answers"] = body.get("answers", body)
                _append_audit(record, audit_enabled)
                if shadow:
                    return None
                result = dict(body)
                result["trace_id"] = trace_id
                return result

            if status == 403:
                _mark_denied(index)
                failures.append("no_permission")
                continue
            if status == 401:
                _mark_denied(index)
                failures.append("unauthorized")
                continue
            if status == 429:
                _mark_cooldown(index)
                failures.append("rate_limited")
                continue

            # 其余状态码（413/422/5xx 等）：上游故障，不重试
            record["fallback_reason"] = "upstream_error"
            _append_audit(record, audit_enabled)
            return None

        record["fallback_reason"] = _final_reason(failures)
        _append_audit(record, audit_enabled)
        return None
    except Exception:
        try:
            record["fallback_reason"] = record.get("fallback_reason") or "upstream_error"
            _append_audit(record, audit_enabled)
        except Exception:
            pass
        return None


# ---------------------------------------------------------------------------
# 探针（逐 Key 1 题无害 noul；每把 Key 至多 1 次）
# ---------------------------------------------------------------------------

def probe_decision() -> dict:
    """逐 Key 各发 1 题无害 noul 探针，返回分类结果。

    状态取值：available（含 latency_ms）/ no_permission / invalid_key /
    timeout / network_error / rate_limited / error。
    """
    cfg = load_decision_config()
    model_id = str(cfg.get("model_id") or "")
    results: list = []
    msg = ""
    try:
        provider = _resolve_provider(cfg)
        base_url = _resolve_base_url(cfg, provider)
        keys = _load_keys(cfg, provider)
        timeout_seconds = _effective_timeout_ms(cfg, None) / 1000.0
        url = (
            _endpoint_url(base_url, cfg.get("endpoint_style") or "decision")
            if base_url
            else ""
        )
        payload = {
            "model": model_id,
            "state": _PROBE_STATE,
            "questions": {"probe": {"type": "noul", "instructions": _PROBE_INSTRUCTIONS}},
        }
        with _KEY_STATE.lock:
            _sync_keys_locked(keys)
        for index, key in enumerate(keys):
            started = _now()
            status = "error"
            error = ""
            response = None
            if not url:
                error = "未配置可用通道（找不到基元律动 provider 或 base_url）"
            else:
                try:
                    response = _post(url, _headers(key, url), payload, timeout_seconds)
                except httpx.TimeoutException:
                    status = "timeout"
                    error = "请求超时"
                except httpx.HTTPError as exc:
                    status = "network_error"
                    error = str(exc)
                except Exception as exc:
                    status = "error"
                    error = str(exc)
            latency_ms = int((_now() - started) * 1000)
            if response is not None:
                code = getattr(response, "status_code", 0)
                if code == 200:
                    status = "available"
                elif code == 403:
                    status = "no_permission"
                    error = "上游 403（无该模型/灰度权限）"
                elif code == 401:
                    status = "invalid_key"
                    error = "上游 401（密钥无效）"
                elif code == 429:
                    status = "rate_limited"
                    error = "上游 429（限流）"
                else:
                    status = "error"
                    error = f"上游返回 HTTP {code}"
            results.append(
                {
                    "key_index": index,
                    "status": status,
                    "latency_ms": latency_ms,
                    "error": error,
                }
            )
            _apply_probe_state(index, status)
        if not keys:
            msg = "未找到可用 Key（检查 provider_ref / base_url 配置）"
    except Exception as exc:
        msg = f"探针执行失败：{exc}"
    result = {"ok": not msg, "results": results, "model_id": model_id, "msg": msg}
    # 探针也是真实调用：写一条 scene="probe" 的审计，让设置页「决策记录」
    # 面板能立刻证明链路已通（与业务场景记录用 scene 字段区分统计）。
    try:
        record = _new_record(cfg, "probe", False, f"probe-{int(_now() * 1000)}")
        available = next(
            (item for item in results if item["status"] == "available"), None
        )
        record["latency_ms"] = available["latency_ms"] if available else None
        record["adopted"] = available is not None
        if not available:
            record["fallback_reason"] = (
                results[0].get("error") if results else (msg or "no_key")
            )
        _append_audit(record, bool(cfg.get("audit_enabled", True)))
    except Exception:
        pass
    with _KEY_STATE.lock:
        _KEY_STATE.last_probe = result
    return result


# ---------------------------------------------------------------------------
# 上游模型列表（设置页「从上游拉取」下拉）
# ---------------------------------------------------------------------------

def list_decision_providers() -> dict:
    """列出「决策模型」可选的调用通道（不含 Key 内容，仅报 Key 数量）。

    数据源与 ask_decision 同一路径（~/.ycode/config.json），保证"界面能选到的
    通道"和"实际能用的通道"一致；base_url 含 tokenrhythm 的第一个通道额外标记
    auto_match=True（即 provider_ref="auto" 的解析目标），供前端提示。
    """
    try:
        providers = [
            item
            for item in (_load_user_config().get("providers") or [])
            if isinstance(item, dict)
        ]
    except Exception:
        providers = []
    cfg = load_decision_config()
    items: list = []
    auto_ref = ""
    for provider in providers:
        name = str(provider.get("name") or "").strip()
        pid = str(provider.get("id") or "").strip()
        ref = pid or name
        if not ref:
            continue
        base_url = str(provider.get("base_url") or "").strip()
        is_auto = False
        if not auto_ref and "tokenrhythm" in base_url.lower():
            auto_ref = ref
            is_auto = True
        try:
            key_count = len(_load_keys(cfg, provider))
        except Exception:
            key_count = 0
        items.append({
            "ref": ref,
            "name": name or ref,
            "base_url": base_url,
            "key_count": key_count,
            "auto_match": is_auto,
        })
    return {"ok": True, "providers": items}


def _price_text(model: dict) -> str:
    input_price = model.get("effectiveInputPrice")
    output_price = model.get("effectiveOutputPrice")
    if (input_price in (None, "", 0)) and (output_price in (None, "", 0)):
        return ""
    return f"输入 {input_price} / 输出 {output_price}"


def list_decision_models() -> dict:
    """GET 上游 {scheme+host}/api/models，过滤 type=="decision" 的模型。"""
    cfg = load_decision_config()
    provider = _resolve_provider(cfg)
    base_url = _resolve_base_url(cfg, provider)
    if not base_url:
        return {
            "ok": False,
            "models": [],
            "msg": "未找到基元律动通道（provider_ref=auto 需 provider base_url 含 tokenrhythm）",
        }
    parts = urlsplit(base_url)
    if not parts.scheme or not parts.netloc:
        return {"ok": False, "models": [], "msg": f"base_url 无效：{base_url}"}
    url = f"{parts.scheme}://{parts.netloc}/api/models"
    timeout_seconds = min(_effective_timeout_ms(cfg, None), 3000) / 1000.0
    try:
        response = _get(url, timeout_seconds)
    except httpx.TimeoutException:
        return {"ok": False, "models": [], "msg": "拉取模型列表超时"}
    except Exception as exc:
        return {"ok": False, "models": [], "msg": f"拉取模型列表失败：{exc}"}
    code = getattr(response, "status_code", 0)
    if code != 200:
        return {"ok": False, "models": [], "msg": f"上游返回 HTTP {code}"}
    try:
        data = response.json()
    except Exception:
        return {"ok": False, "models": [], "msg": "上游响应不是合法 JSON"}
    items = data
    if isinstance(data, dict):
        items = next(
            (
                data.get(key)
                for key in ("data", "models", "items", "list")
                if isinstance(data.get(key), list)
            ),
            [],
        )
    if not isinstance(items, list):
        items = []
    models: list = []
    for item in items:
        if not isinstance(item, dict) or item.get("type") != "decision":
            continue
        models.append(
            {
                "id": str(item.get("id") or ""),
                "name": str(item.get("name") or item.get("id") or ""),
                "context": item.get("contextWindow") or item.get("context_window") or 0,
                "price_text": _price_text(item),
            }
        )
    return {"ok": True, "models": models, "msg": ""}


# ---------------------------------------------------------------------------
# 设置页快照（配置 + 运行时状态）
# ---------------------------------------------------------------------------

def _audit_brief(record: dict) -> dict:
    """审计记录 → 面板精简条目（不含 state/questions 正文）。"""
    answers = record.get("answers")
    answers_digest = ""
    if isinstance(answers, dict):
        # 只透出紧凑摘要（如 {"intent": "dispatch"}），避免把大对象塞进面板
        answers_digest = json.dumps(answers, ensure_ascii=False)[:160]
    return {
        "ts": str(record.get("ts") or ""),
        "scene": str(record.get("scene") or ""),
        "shadow": bool(record.get("shadow")),
        "adopted": bool(record.get("adopted")),
        "latency_ms": record.get("latency_ms"),
        "fallback_reason": str(record.get("fallback_reason") or ""),
        "model_id": str(record.get("model_id") or ""),
        "answers_digest": answers_digest,
    }


def get_decision_audit_summary(limit: int = 20) -> dict:
    """汇总决策审计：今日计数 + 最近 N 条记录（设置页「决策记录」面板用）。

    只读、容错：目录缺失返回全零；单行损坏跳过该行；绝不抛错。
    """
    try:
        count = max(1, min(int(limit), 200))
    except (TypeError, ValueError):
        count = 20
    directory = _audit_dir()
    summary = {
        "ok": True,
        "today_count": 0,
        "today_shadow_count": 0,
        "today_adopted_count": 0,
        "today_fallback_count": 0,
        "recent": [],
    }
    if not os.path.isdir(directory):
        return summary
    try:
        names = sorted(
            name for name in os.listdir(directory) if name.endswith(".jsonl")
        )
    except Exception:
        return summary
    today_name = f"{_today()}.jsonl"

    def _iter_records(path: str) -> list:
        records: list = []
        try:
            with open(path, "r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        records.append(json.loads(line))
                    except Exception:
                        continue
        except Exception:
            pass
        return records

    if today_name in names:
        for record in _iter_records(os.path.join(directory, today_name)):
            summary["today_count"] += 1
            if record.get("shadow"):
                summary["today_shadow_count"] += 1
            if record.get("adopted"):
                summary["today_adopted_count"] += 1
            if record.get("fallback_reason"):
                summary["today_fallback_count"] += 1
    # 最近 N 条：从最新文件往前收集；收集时把老文件插到前面，保持时间升序
    collected: list = []
    for name in reversed(names):
        if len(collected) >= count:
            break
        briefs = [
            _audit_brief(record)
            for record in _iter_records(os.path.join(directory, name))
        ]
        collected = briefs + collected
    summary["recent"] = collected[-count:]
    return summary


def get_decision_settings_snapshot() -> dict:
    """配置快照 + 运行时状态（可用 Key 序号、拒绝/冷却列表、最近探针结果）。"""
    cfg = load_decision_config()
    with _KEY_STATE.lock:
        now = _now()
        runtime = {
            "key_count": len(_KEY_STATE.signature),
            "active_key_index": _KEY_STATE.active_index if _KEY_STATE.signature else None,
            "denied_key_indexes": sorted(_KEY_STATE.denied),
            "cooldown_key_indexes": sorted(
                index for index, until in _KEY_STATE.cooldowns.items() if until > now
            ),
            "last_probe": deepcopy(_KEY_STATE.last_probe),
        }
    return {"config": cfg, "runtime": runtime}

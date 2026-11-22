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

DEFAULT_CONFIG: dict = {
    "version": 1,
    "enabled": False,
    "provider_ref": "auto",
    "base_url": "",
    "model_id": "NeoHorse-Jev-4B",
    "endpoint_style": "decision",
    "key_mode": "auto",
    "timeout_ms": 800,
    "scenes": {
        "router": {"enabled": False, "threshold": 0.5, "timeout_ms": 250},
        "secretary": {"enabled": False, "threshold": 0.6, "timeout_ms": 250},
        "search": {"enabled": False, "threshold": 0.5, "timeout_ms": 250},
        "safety": {"enabled": False, "threshold": 0.9, "timeout_ms": 250},
    },
    "audit_enabled": True,
}

DECISION_SCENE_META = (
    ("router", "路由", "本回合用会话主模型还是副模型；换模型失败或低置信自动回退主模型"),
    ("secretary", "秘书", "任务派发给哪个候选会话；失败回退原有派发规则"),
    ("search", "搜索", "一次搜索取回几条结果（3 / 5 / 8）；失败用默认 5 条"),
    ("safety", "安全", "高风险命令是否放行（阈值最高 0.9）；失败回退原判定结果"),
)
DECISION_SCENE_MODES = ("off", "live")
DECISION_SCENE_MODE_TEXT = {"off": "关闭", "live": "开启"}

MAX_QUESTIONS = 16
MIN_TIMEOUT_MS = 100
MAX_TIMEOUT_MS = 5000
KEY_COOLDOWN_SECONDS = 60

AUTO_TASK_SCENES = frozenset({
    "loop_guard",
    "subagent_review",
    "plan_recovery",
    "memory_filter",
    "auto_permission",
    "auto_question",
})

_PROBE_STATE = "本请求为无害连通探针，不涉及任何用户数据。"
_PROBE_INSTRUCTIONS = "1+1 是否等于 2？请给出该命题成立的概率。"

# 单条审计记录的字节上限：要容纳「请求快照（state ≤500 字 + questions）」+
# 「上游答案」，同时仍保持一条一行的小体积；超限按 _cap_record 的顺序降级。
_AUDIT_MAX_BYTES = 6000
_AUDIT_LOCK = threading.Lock()

_RE_KEY_SEPARATOR = re.compile(r"[\r\n,，;；]+")


def _ycode_dir() -> str:
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
    if isinstance(state, str):
        text = state.strip()
        return text if len(text) <= 80 else text[:80]
    return _sha16(state)


def _strip_legacy_shadow_fields(cfg: dict) -> dict:
    """清理历史影子模式配置字段，确保旧版 decision.json 与旧入参平滑过渡。"""
    cfg.pop("shadow_mode", None)
    scenes = cfg.get("scenes")
    if isinstance(scenes, dict):
        for scene_cfg in scenes.values():
            if isinstance(scene_cfg, dict):
                scene_cfg.pop("shadow", None)
    return cfg


def load_decision_config() -> dict:
    """读取决策配置并与默认值深合并；缺失/损坏一律回落默认值（全关）。"""
    return _strip_legacy_shadow_fields(
        _deep_merge(deepcopy(DEFAULT_CONFIG), _read_json(_decision_config_path()))
    )


def _validate_config(cfg: dict) -> None:
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
    merged = _strip_legacy_shadow_fields(_deep_merge(load_decision_config(), patch))
    _validate_config(merged)
    import config

    config._atomic_write_json(_decision_config_path(), merged)
    return merged


def build_decision_scene_rows(cfg: dict) -> list[dict]:
    """构造四个业务场景的二态快照（off 关闭 / live 开启）。"""
    scenes = cfg.get("scenes") if isinstance(cfg.get("scenes"), dict) else {}
    rows: list[dict] = []
    for scene, label, description in DECISION_SCENE_META:
        raw_scene = scenes.get(scene)
        scene_cfg = raw_scene if isinstance(raw_scene, dict) else {}
        enabled = bool(scene_cfg.get("enabled", False))
        mode = "live" if enabled else "off"
        raw_threshold = scene_cfg.get("threshold", 0.5)
        if isinstance(raw_threshold, bool) or not isinstance(raw_threshold, (int, float)):
            threshold = 0.5
        else:
            threshold = max(0.0, min(1.0, float(raw_threshold)))
        raw_timeout = scene_cfg.get("timeout_ms", 250)
        if isinstance(raw_timeout, bool) or not isinstance(raw_timeout, (int, float)):
            timeout_ms = 250
        else:
            timeout_ms = int(raw_timeout)
        rows.append(
            {
                "scene": scene,
                "label": label,
                "description": description,
                "mode": mode,
                "mode_text": DECISION_SCENE_MODE_TEXT[mode],
                "enabled": enabled,
                "threshold": threshold,
                "timeout_ms": timeout_ms,
            }
        )
    return rows


def build_scene_change_feedback(cfg: dict, row: dict, mode_name: str) -> tuple[str, str]:
    """根据当前总开关与场景新状态返回人类可读提示语 (msg, hint)。"""
    label = str(row.get("label") or row.get("scene") or "")
    effective_mode = str(row.get("mode") or "off")
    if not bool(cfg.get("enabled")):
        msg = f"{label}场景已保存为{DECISION_SCENE_MODE_TEXT[mode_name]}"
        hint = "决策模型总开关未启用：场景设置要等总开关打开后才生效"
    elif effective_mode == "live":
        msg = f"{label}场景已开启（决策结果会真实影响任务行为）"
        hint = "开启后：失败、超时或低置信都会自动回退原逻辑，不会阻断任务"
    else:
        msg = f"{label}场景已关闭（零请求、零记录，走原逻辑）"
        hint = "关闭后该场景不再询问决策模型，行为与未接入时一致"
    return msg, hint


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
    with _KEY_STATE.lock:
        _KEY_STATE.signature = ()
        _KEY_STATE.active_index = 0
        _KEY_STATE.denied.clear()
        _KEY_STATE.cooldowns.clear()
        _KEY_STATE.last_probe = None


def _sync_keys_locked(keys: list) -> None:
    signature = tuple(keys)
    if signature != _KEY_STATE.signature:
        _KEY_STATE.signature = signature
        _KEY_STATE.active_index = 0
        _KEY_STATE.denied.clear()
        _KEY_STATE.cooldowns.clear()


def _load_user_config() -> dict:
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
    if not isinstance(raw, str):
        return []
    result: list = []
    for part in _RE_KEY_SEPARATOR.split(raw):
        item = part.strip()
        if item and item not in result:
            result.append(item)
    return result


def _load_keys(cfg: dict, provider=None) -> list:
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
    with httpx.Client(timeout=timeout_seconds) as client:
        return client.get(url, headers=_headers(None, url))


def _effective_timeout_ms(cfg: dict, override=None) -> int:
    value = override if override is not None else cfg.get("timeout_ms")
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = DEFAULT_CONFIG["timeout_ms"]
    return max(MIN_TIMEOUT_MS, min(MAX_TIMEOUT_MS, value))


def _new_record(cfg: dict, scene: str, trace_id: str) -> dict:
    return {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "scene": scene,
        "model_id": cfg.get("model_id"),
        "key_hint": None,
        "state_digest": None,
        "questions_digest": None,
        # 请求快照：实际发给决策模型的 model / state / questions，
        # 用于「回看这次到底问了什么」；state 已脱敏并截断（见 _audit_state）
        "request": None,
        "answers": None,
        "latency_ms": None,
        "adopted": False,
        "fallback_reason": None,
        "trace_id": trace_id,
        "session_id": "",
    }


def _audit_state(state, limit: int = 500) -> str:
    """审计用的 state 快照：先脱敏再截断（非字符串先序列化）。

    与发给模型的 state 同源同量级（router 场景本来就只发 500 字符），这样
    「面板回看发给 JEV 的请求」看到的就是真实请求体，而不是另一个摘要。
    """
    if isinstance(state, str):
        text = state
    else:
        try:
            text = json.dumps(state, ensure_ascii=False)
        except Exception:
            text = str(state or "")
    text = text.strip()
    try:
        # 延迟导入：避免 decision_service ↔ decision_gateway 模块级循环依赖
        from core.decision_gateway import redact_sensitive

        text = redact_sensitive(text)
    except Exception:
        pass
    return text if len(text) <= limit else text[:limit]


def _current_session_id() -> str:
    """当前回合所属会话名（取不到返回空串）。

    延迟 import ``core.app_context``：避免模块级循环依赖（app_context 会反向
    引用 core 层）。优先取线程级会话（并行回合稳定归属），其次进程级「当前
    查看的会话」；任何异常一律当作取不到，绝不影响决策主流程。
    """
    try:
        from core import app_context

        for getter in ("get_thread_session", "get_current_session"):
            try:
                value = getattr(app_context, getter)()
            except Exception:
                continue
            value = str(value or "").strip()
            if value:
                return value
        return ""
    except Exception:
        return ""


def _cap_record(record: dict) -> dict:
    try:
        def _size(value) -> int:
            return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))

        if _size(record) <= _AUDIT_MAX_BYTES:
            return record
        capped = dict(record)
        request = capped.get("request")
        if isinstance(request, dict):
            shrunk = dict(request)
            shrunk["state"] = str(request.get("state") or "")[:200]
            shrunk["state_truncated"] = True
            capped["request"] = shrunk
        if _size(capped) <= _AUDIT_MAX_BYTES:
            return capped
        capped["answers"] = {"truncated": True, "digest": _sha16(record.get("answers"))}
        if _size(capped) <= _AUDIT_MAX_BYTES:
            return capped
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


def rename_session_audit(old_name: str, new_name: str) -> int:
    """会话重命名时迁移历史审计记录里的 session_id，避免旧决策记录在新名下消失。"""
    old_sid = str(old_name or "").strip()
    new_sid = str(new_name or "").strip()
    if not old_sid or not new_sid or old_sid == new_sid:
        return 0
    updated = 0
    try:
        directory = _audit_dir()
        if not os.path.isdir(directory):
            return 0
        names = sorted(
            name for name in os.listdir(directory) if name.endswith(".jsonl")
        )
        ascii_escaped = json.dumps(old_sid)[1:-1]
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
                    if old_sid not in line and ascii_escaped not in line:
                        continue
                    try:
                        record = json.loads(line)
                    except Exception:
                        continue
                    if (
                        isinstance(record, dict)
                        and str(record.get("session_id") or "") == old_sid
                    ):
                        record["session_id"] = new_sid
                        lines[position] = json.dumps(record, ensure_ascii=False) + "\n"
                        changed = True
                        updated += 1
                if changed:
                    _rewrite_lines(path, lines)
    except Exception:
        pass
    return updated


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


def ask_decision(
    state,
    questions,
    *,
    scene,
    timeout_ms=None,
    image_b64=None,
    audit_extra=None,
    session_id=None,
):
    cfg = load_decision_config()
    if not bool(cfg.get("enabled")):
        return None
    scene_name = scene if isinstance(scene, str) else str(scene or "")
    scenes = cfg.get("scenes") if isinstance(cfg.get("scenes"), dict) else {}
    scene_cfg = scenes.get(scene_name)
    if isinstance(scene_cfg, dict):
        if not bool(scene_cfg.get("enabled")):
            return None
    elif scene_name not in AUTO_TASK_SCENES:
        return None
    if not isinstance(questions, dict) or not questions:
        raise ValueError("questions 必须是非空字典")
    if len(questions) > MAX_QUESTIONS:
        raise ValueError(
            f"单次最多 {MAX_QUESTIONS} 题（当前 {len(questions)} 题），请由上层拆批"
        )
    if image_b64 is not None and len(questions) > 1:
        raise ValueError("带图请求一次只允许 1 题")

    audit_enabled = bool(cfg.get("audit_enabled", True))
    trace_id = uuid.uuid4().hex[:12]
    timeout_seconds = _effective_timeout_ms(cfg, timeout_ms) / 1000.0
    record = _new_record(cfg, scene_name, trace_id)
    # 会话归属在构造后立即填充，保证下面所有 _append_audit 分支都带上该字段。
    # 调用方显式给出 session_id（如后台学习线程）时优先采纳，否则读取线程/当前会话。
    explicit_sid = str(session_id or "").strip() if session_id is not None else ""
    record["session_id"] = explicit_sid or _current_session_id()
    record["state_digest"] = _state_digest(state)
    record["questions_digest"] = _sha16(questions)
    record["request"] = {
        "model": cfg.get("model_id"),
        "state": _audit_state(state),
        "questions": questions,
    }
    if isinstance(audit_extra, dict) and audit_extra:
        # 调用方附加上下文（如启发式基线档 baseline_tier）：供后续一致性对比分析；
        # 体积由 _cap_record 统一兜底
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


def probe_decision() -> dict:
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
    try:
        record = _new_record(cfg, "probe", f"probe-{int(_now() * 1000)}")
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


def list_decision_providers() -> dict:
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


def _brief_answer(answers) -> str:
    """从审计 answers 里提取一行结论（choice 选项 / noul 概率 / score 等级）。

    只做展示用的字符串化：解析不出来就返回空串，绝不抛错。
    """
    try:
        payload = answers
        if isinstance(payload, dict):
            if "q" in payload:
                payload = payload["q"]
            elif len(payload) == 1:
                payload = next(iter(payload.values()))
            else:
                return ""
        if isinstance(payload, dict):
            if "choice" in payload:
                return str(payload["choice"])[:40]
            if "noul" in payload:
                return f"概率 {float(payload['noul']):.2f}"
            if "score" in payload:
                return f"评分 {payload['score']}"
            return ""
        if isinstance(payload, bool):
            return "是" if payload else "否"
        if isinstance(payload, (int, float, str)):
            return str(payload)[:40]
    except Exception:
        return ""
    return ""


def _audit_brief(record: dict) -> dict:
    """审计记录 → 面板精简条目。

    ``state_digest`` 是**决策当时的输入摘要**（已在构造时脱敏并截断），面板靠它
    显示「这一次到底判的是什么事」；``answer`` 是模型给出的结论（choice 选项 /
    noul 概率），两者都只透出短片段，避免把大对象塞进面板。
    """
    answers = record.get("answers")
    answers_digest = ""
    if isinstance(answers, dict):
        # 只透出紧凑摘要（如 {"intent": "dispatch"}），避免把大对象塞进面板
        answers_digest = json.dumps(answers, ensure_ascii=False)[:160]
    return {
        "ts": str(record.get("ts") or ""),
        "scene": str(record.get("scene") or ""),
        "session_id": str(record.get("session_id") or ""),
        "adopted": bool(record.get("adopted")),
        "latency_ms": record.get("latency_ms"),
        "fallback_reason": str(record.get("fallback_reason") or ""),
        "model_id": str(record.get("model_id") or ""),
        "trace_id": str(record.get("trace_id") or ""),
        "state_digest": str(record.get("state_digest") or "")[:160],
        "answer": _brief_answer(answers),
        "answers_digest": answers_digest,
    }


def get_decision_audit_summary(
    limit: int = 20, session_id=None, session_only: bool = False
) -> dict:
    try:
        count = max(1, min(int(limit), 200))
    except (TypeError, ValueError):
        count = 20
    wanted_session = str(session_id or "").strip()
    directory = _audit_dir()
    summary = {
        "ok": True,
        "today_count": 0,
        "today_adopted_count": 0,
        "today_fallback_count": 0,
        # 所属范围的累计决策总数（传了 session_id 即本会话累计总数，否则全局累计）
        "session_total_count": 0,
        "global_today_count": 0,
        "recent": [],
        "session_scoped": bool(session_only and wanted_session),
        "has_business_records": False,
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

    def _in_scope(record) -> bool:
        if not isinstance(record, dict):
            return False
        if not (session_only and wanted_session):
            return True
        return str(record.get("session_id") or "") == wanted_session

    # 探针（scene="probe"）只是连通性验证，不算真实决策：头部「决策」入口
    # 只认业务场景记录，否则跑过一次探针就会挂出一个没有任何决策内容的入口。
    # 传了 session_id 时再叠加会话过滤：别的会话做过的决策不算本会话做过。
    def _is_business(record: dict) -> bool:
        if not isinstance(record, dict):
            return False
        if str(record.get("scene") or "") in ("", "probe"):
            return False
        if not wanted_session:
            return True
        return str(record.get("session_id") or "") == wanted_session

    collected: list = []
    for name in reversed(names):
        records = _iter_records(os.path.join(directory, name))
        is_today = name == today_name
        file_briefs: list = []
        for record in records:
            if not isinstance(record, dict):
                continue
            if not summary["has_business_records"] and _is_business(record):
                summary["has_business_records"] = True
            if not wanted_session or str(record.get("session_id") or "") == wanted_session:
                summary["session_total_count"] += 1
            if is_today:
                summary["global_today_count"] += 1
                if _in_scope(record):
                    summary["today_count"] += 1
                    if record.get("adopted"):
                        summary["today_adopted_count"] += 1
                    if record.get("fallback_reason"):
                        summary["today_fallback_count"] += 1
            if len(collected) < count and _in_scope(record):
                file_briefs.append(_audit_brief(record))
        if file_briefs:
            collected = file_briefs + collected
    # 最新一条排在最前，面板和设置页无需下滑即可直接看到最近一次决策
    summary["recent"] = list(reversed(collected[-count:]))
    return summary


def get_decision_record_detail(trace_id: str) -> dict:
    """按 trace_id 取单条审计记录的完整内容（请求快照 + 上游答案）。

    面板「点开一条记录」用：``request`` 是真正发给决策模型的 model/state/questions，
    ``answers`` 是上游原样返回的结构（choice 选项 + probabilities 等）。
    只读、容错：找不到返回 ok=false，文件损坏跳过该行，绝不抛错。
    """
    wanted = str(trace_id or "").strip()
    if not wanted:
        return {"ok": False, "msg": "缺少 trace_id"}
    directory = _audit_dir()
    if not os.path.isdir(directory):
        return {"ok": False, "msg": "审计目录不存在"}
    try:
        names = sorted(
            (name for name in os.listdir(directory) if name.endswith(".jsonl")),
            reverse=True,
        )
    except Exception:
        return {"ok": False, "msg": "审计目录不可读"}
    for name in names:
        try:
            with open(os.path.join(directory, name), "r", encoding="utf-8") as handle:
                lines = handle.readlines()
        except Exception:
            continue
        for line in reversed(lines):
            line = line.strip()
            if not line or wanted not in line:
                continue
            try:
                record = json.loads(line)
            except Exception:
                continue
            if isinstance(record, dict) and str(record.get("trace_id") or "") == wanted:
                return {"ok": True, "record": record}
    return {"ok": False, "msg": "未找到这条决策记录"}


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

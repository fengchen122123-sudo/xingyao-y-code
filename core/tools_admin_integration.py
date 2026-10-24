"""管理集成类 Agent 工具：飞书通道 / 局域网远程控制 / 决策模型（JEV）。

设计约束：
- 只复用 core 层公开模块，禁止 import gui_bridge / main / pywebview；
- 所有工具统一返回 JSON 字符串（ensure_ascii=False）：
  成功 ``{"ok": true, ...}``，失败 ``{"ok": false, "error": "中文原因"}``；
- 所有异常在工具入口处兜底，任何情况下都不向调度层抛出；
- 密钥 / 令牌一律脱敏：只回 ``has_secret`` 之类的布尔，绝不回明文；
- 决策工具只操作 JEV 决策模型的开关与业务场景配置，不做权限 / 信任类动作。

工具清单（build_specs() 返回 3 个 ToolSpec）：
feishu_manage / lan_manage / decision_manage

实际复用的 core 接口：
- 飞书：``core.feishu.config.load_feishu_config``（读）、
  ``config.update_config`` + ``core.credentials.protect_secret``（写）、
  ``core.feishu.service.feishu_service``（模块级单例，启停 / 状态）、
  ``core.feishu._lark_ws.Client``（真实连接测试）；
- 局域网：``core.lan_server.LanServer``（start / stop / port / lan_ip / _thread）；
- 决策：``core.decision_service`` 的 load_decision_config /
  save_decision_config / get_decision_settings_snapshot / probe_decision /
  _load_keys（只取数量，绝不回明文）。
"""

from __future__ import annotations

import functools
import json
import socket
import threading
from typing import Any

from core.tool_protocol import ToolSpec


# ---------------------------------------------------------------------------
# 通用返回与容错
# ---------------------------------------------------------------------------

def _dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _ok(**payload: Any) -> str:
    data: dict[str, Any] = {"ok": True}
    data.update(payload)
    return _dumps(data)


def _fail(error: Any, **extra: Any) -> str:
    data: dict[str, Any] = {"ok": False, "error": str(error)}
    data.update(extra)
    return _dumps(data)


def _guard(func):
    """工具入口兜底：任何异常都转成统一失败 JSON，不让异常冒到调度层。"""

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> str:
        try:
            return func(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - 工具边界必须吞掉全部异常
            return _fail(f"{type(exc).__name__}: {exc}")

    return wrapper


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value or "").strip().lower()
    if text in {"1", "true", "yes", "y", "on", "是", "开启"}:
        return True
    if text in {"0", "false", "no", "n", "off", "否", "关闭", ""}:
        return False
    return default


def _as_str_list(value: Any) -> list[str]:
    if isinstance(value, str):
        items = value.replace("，", ",").split(",")
    elif isinstance(value, (list, tuple, set)):
        items = list(value)
    else:
        return []
    return [str(item).strip() for item in items if str(item).strip()]


# 需要脱敏的字段名（命中即替换为掩码说明，绝不回明文）
_SENSITIVE_FIELD_NAMES = frozenset(
    {
        "api_key",
        "apikey",
        "secret",
        "app_secret",
        "client_secret",
        "token",
        "access_token",
        "app_token",
        "password",
        "authorization",
        "bearer",
    }
)


def _sanitize(value: Any, depth: int = 0) -> Any:
    """递归脱敏：敏感字段名一律替换为 ``"***"``，非敏感字段原样保留。"""
    if depth > 8:
        return "***"
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            name = str(key).strip().lower()
            if name in _SENSITIVE_FIELD_NAMES:
                cleaned[key] = "***" if item else ""
            else:
                cleaned[key] = _sanitize(item, depth + 1)
        return cleaned
    if isinstance(value, list):
        return [_sanitize(item, depth + 1) for item in value]
    return value


def _schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        schema["required"] = required
    return schema


# ---------------------------------------------------------------------------
# 飞书通道
# ---------------------------------------------------------------------------

FEISHU_ACTIONS = ("get", "set", "test", "status")

# set 允许写入的 payload 键（其余键忽略并在返回里说明）
_FEISHU_WRITABLE_KEYS = ("enabled", "app_id", "app_secret", "approvers", "notify_chat_id")


def _feishu_status_snapshot() -> dict[str, Any]:
    """当前飞书长连接服务的运行时状态（无 broker 时按未启动处理）。"""
    from core.feishu.service import feishu_service

    status = feishu_service.status()
    return status if isinstance(status, dict) else {}


def _feishu_settings() -> dict[str, Any]:
    """飞书设置快照：**不含 app_secret 明文**，只回 has_secret 布尔。"""
    from core.feishu.config import load_feishu_config

    cfg = load_feishu_config()
    return {
        "enabled": bool(cfg.enabled),
        "app_id": cfg.app_id,
        "has_secret": bool(cfg.has_secret),
        "approvers": list(cfg.approvers),
        "notify_chat_id": cfg.notify_chat_id,
        "configured": bool(cfg.configured),
    }


def _refresh_feishu_service() -> dict[str, Any]:
    """配置变更后让飞书服务按新配置生效（启动 / 停止 / 重连）。

    与 gui_bridge._refresh_feishu_service 同口径，但走 core.feishu.service 的
    模块级单例，不依赖 GUI 桥接对象。返回最新状态快照（不抛异常）。
    """
    from core.feishu.config import load_feishu_config
    from core.feishu.service import feishu_service

    try:
        configured = bool(load_feishu_config().configured)
    except Exception:
        configured = False

    try:
        running = bool(feishu_service.status().get("running"))
    except Exception:
        running = False

    try:
        if not configured:
            if running:
                feishu_service.stop()
        elif running:
            feishu_service.restart()
        else:
            feishu_service.start()
    except Exception:
        # 启动/重启异常不回抛：下方用真实状态快照把原因带给调用方
        pass

    try:
        return feishu_service.status()
    except Exception:
        return {}


def _feishu_test_connection(app_id: str, app_secret: str) -> dict[str, Any]:
    """请求飞书长连接接入点，校验凭据是否可用；异常原样带回真实原因。"""
    from core.feishu._lark_ws import Client
    from core.feishu.config import load_feishu_config

    cfg = load_feishu_config()
    use_id = str(app_id or "").strip() or cfg.app_id
    use_secret = str(app_secret or "").strip() or cfg.app_secret
    if not use_id or not use_secret:
        return {"ok": False, "error": "请先填写 App ID 与 App Secret（或先在设置里保存凭据）"}
    client = Client(app_id=use_id, app_secret=use_secret, source="xingyao")
    url = client._get_conn_url()
    return {
        "ok": True,
        "endpoint_ready": bool(url),
        "msg": "凭据有效，飞书已返回长连接地址" if url else "凭据请求未返回长连接地址",
        "app_id": use_id,
    }


@_guard
def feishu_manage(action: str = "get", payload: dict | None = None) -> str:
    """飞书通道管理：get / set / test / status。"""
    act = str(action or "get").strip().lower()
    if act not in FEISHU_ACTIONS:
        return _fail(f"未知 action「{act or '(空)'}」；合法值为 {' / '.join(FEISHU_ACTIONS)}")

    if act == "get":
        return _ok(action="get", settings=_feishu_settings(), status=_feishu_status_snapshot())

    if act == "status":
        status = _feishu_status_snapshot()
        return _ok(
            action="status",
            running=bool(status.get("running")),
            state=str(status.get("state") or ""),
            configured=bool(status.get("configured")),
            last_error=str(status.get("last_error") or ""),
            received_events=int(status.get("received_events") or 0),
            duplicate_events=int(status.get("duplicate_events") or 0),
            uptime_seconds=int(status.get("uptime_seconds") or 0),
            status=status,
        )

    if act == "test":
        data = payload if isinstance(payload, dict) else {}
        result = _feishu_test_connection(
            str(data.get("app_id") or ""),
            str(data.get("app_secret") or ""),
        )
        if not result.get("ok"):
            return _fail(result.get("error") or "连接测试失败")
        return _ok(
            action="test",
            endpoint_ready=bool(result.get("endpoint_ready")),
            msg=str(result.get("msg") or ""),
            app_id=str(result.get("app_id") or ""),
        )

    # act == "set"
    if payload is not None and not isinstance(payload, dict):
        return _fail("payload 必须是对象（键值对）")
    data = dict(payload or {})

    from core.credentials import protect_secret
    import config as app_config

    ignored = sorted(key for key in data if key not in _FEISHU_WRITABLE_KEYS)
    app_id = str(data.get("app_id") or "").strip()
    approvers = _as_str_list(data.get("approvers"))
    notify_chat_id = str(data.get("notify_chat_id") or "").strip()
    enabled = _as_bool(data.get("enabled"), False)
    secret = str(data.get("app_secret") or "")
    secret_provided = bool(secret.strip())

    def mutate(stored: dict[str, Any]) -> None:
        stored[app_config.FEISHU_ENABLED_KEY] = enabled
        stored[app_config.FEISHU_APP_ID_KEY] = app_id
        if secret_provided:
            # 密钥写盘前加密（dpapi:/keyring:）；空字符串表示「不修改已有密钥」
            stored[app_config.FEISHU_APP_SECRET_KEY] = protect_secret(secret.strip())
        stored[app_config.FEISHU_APPROVERS_KEY] = approvers
        stored[app_config.FEISHU_NOTIFY_CHAT_ID_KEY] = notify_chat_id

    app_config.update_config(mutate)
    runtime = _refresh_feishu_service()
    result: dict[str, Any] = {
        "action": "set",
        "msg": "飞书设置已保存",
        "settings": _feishu_settings(),
        "status": runtime,
    }
    if ignored:
        result["ignored_keys"] = ignored
    return _ok(**result)


# ---------------------------------------------------------------------------
# 局域网远程控制
# ---------------------------------------------------------------------------

LAN_ACTIONS = ("status", "start", "stop")

_LAN_LOCK = threading.Lock()
# 本工具进程内单例（避免重复启动；不持有 gui_bridge 的实例）
_LAN_STATE: dict[str, Any] = {"server": None}


def _lan_owned_server() -> Any:
    return _LAN_STATE.get("server")


def _lan_running(server: Any) -> bool:
    """服务是否在运行：监听 socket 存在且 daemon 线程存活。"""
    if server is None:
        return False
    if getattr(server, "_server", None) is None:
        return False
    thread = getattr(server, "_thread", None)
    try:
        return bool(thread is not None and thread.is_alive())
    except Exception:
        return False


def _port_listening(port: int, host: str = "127.0.0.1", timeout: float = 0.3) -> bool:
    """探测本机端口是否已被监听（用于识别「别人已经在跑」而不是起第二个）。"""
    if not port or not (0 < int(port) < 65536):
        return False
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.settimeout(timeout)
    try:
        return probe.connect_ex((host, int(port))) == 0
    except Exception:
        return False
    finally:
        try:
            probe.close()
        except Exception:
            pass


def _persisted_lan_port() -> int:
    """读取 config.json 里持久化的远程端口（无效返回 0）。"""
    try:
        import config as app_config

        port = int(app_config._load().get(app_config.REMOTE_PAIR_PORT_KEY) or 0)
    except Exception:
        return 0
    return port if 1024 <= port <= 65535 else 0


def _lan_snapshot(server: Any = None) -> dict[str, Any]:
    """监听地址 / 端口 / 运行状态快照（不启动任何东西，不做网络 IO）。"""
    owned = server is not None
    running = _lan_running(server)
    if owned:
        port = int(getattr(server, "port", 0) or 0)
        lan_ip = str(getattr(server, "lan_ip", "") or "")
    else:
        port = _persisted_lan_port()
        lan_ip = ""
        try:
            from core.lan_server import LanServer

            lan_ip = str(LanServer().lan_ip or "")
        except Exception:
            lan_ip = ""
    url = f"http://{lan_ip}:{port}/" if (lan_ip and port) else ""
    return {
        "running": running,
        "owned_by_tool": owned and running,
        "host": "0.0.0.0" if running else "",
        "port": port,
        "lan_ip": lan_ip,
        "url": url,
    }


@_guard
def lan_manage(action: str = "status") -> str:
    """局域网远程控制服务管理：status / start / stop。"""
    act = str(action or "status").strip().lower()
    if act not in LAN_ACTIONS:
        return _fail(f"未知 action「{act or '(空)'}」；合法值为 {' / '.join(LAN_ACTIONS)}")

    if act == "status":
        with _LAN_LOCK:
            server = _lan_owned_server()
            snapshot = _lan_snapshot(server)
        if not snapshot["running"] and snapshot["port"]:
            # 端口被监听但本工具未持有实例：说明服务由其他入口（桌面端）启动
            if _port_listening(snapshot["port"]):
                snapshot["running"] = True
                snapshot["host"] = "0.0.0.0"
                snapshot["note"] = "服务由其他入口（如桌面端）启动，本工具未持有实例"
        return _ok(action="status", **snapshot)

    if act == "start":
        with _LAN_LOCK:
            server = _lan_owned_server()
            if _lan_running(server):
                snapshot = _lan_snapshot(server)
                snapshot["msg"] = "远程服务已在运行，未重复启动"
                return _ok(action="start", **snapshot)

            if server is None:
                # 端口已被占用（多半是桌面端已在跑）：不再起第二个服务，如实回报
                persisted = _persisted_lan_port()
                if _port_listening(persisted):
                    snapshot = {
                        "running": True,
                        "owned_by_tool": False,
                        "host": "0.0.0.0",
                        "port": persisted,
                        "lan_ip": _lan_snapshot(None)["lan_ip"],
                        "url": "",
                        "note": "端口已被占用，服务可能已由桌面端启动；未重复启动",
                    }
                    snapshot["url"] = (
                        f"http://{snapshot['lan_ip']}:{persisted}/" if snapshot["lan_ip"] else ""
                    )
                    return _ok(action="start", **snapshot)

                from core.lan_server import LanServer

                server = LanServer(bridge=None)
                try:
                    import config as app_config

                    ttl = app_config._load().get(
                        app_config.REMOTE_TOKEN_TTL_KEY,
                        getattr(app_config, "REMOTE_TOKEN_TTL_DEFAULT", 86400),
                    )
                    server.set_ttl(ttl)
                except Exception:
                    pass

            info = server.start()
            if not isinstance(info, dict) or not info.get("ok", True):
                started = False
            else:
                started = True
            if not started:
                return _fail("远程服务启动失败：LanServer.start() 未返回成功")
            _LAN_STATE["server"] = server
            snapshot = _lan_snapshot(server)
            snapshot["msg"] = "远程服务已启动"
            if not snapshot["running"]:
                snapshot["note"] = "监听已建立但守护线程尚未存活，请用 status 复查"
            snapshot["note"] = (
                (snapshot.get("note") or "")
                or "本工具启动的实例没有对话入口（bridge 为空）：静态页面可访问，远端消息派发不可用"
            )
            return _ok(action="start", **snapshot)

    # act == "stop"
    with _LAN_LOCK:
        server = _lan_owned_server()
        if server is None:
            persisted = _persisted_lan_port()
            if _port_listening(persisted):
                snapshot = _lan_snapshot(None)
                snapshot["running"] = True
                snapshot["host"] = "0.0.0.0"
                return _fail(
                    f"端口 {persisted} 上有一个不是本工具启动的服务在运行，无法安全停止；"
                    "请在桌面端设置页停止远程服务",
                    **snapshot,
                )
            snapshot = _lan_snapshot(None)
            snapshot["msg"] = "远程服务当前未运行"
            return _ok(action="stop", **snapshot)

        port = int(getattr(server, "port", 0) or 0)
        lan_ip = str(getattr(server, "lan_ip", "") or "")
        server.stop()
        _LAN_STATE["server"] = None
    running_after = _port_listening(port)
    result = {
        "action": "stop",
        "running": running_after,
        "owned_by_tool": False,
        "port": port,
        "lan_ip": lan_ip,
        "url": f"http://{lan_ip}:{port}/" if (lan_ip and port) else "",
        "msg": "远程服务已停止" if not running_after else "已调用停止，但端口仍在监听，请复查",
    }
    return _ok(**result)


# ---------------------------------------------------------------------------
# 决策模型（JEV）
# ---------------------------------------------------------------------------

DECISION_ACTIONS = ("get", "set", "probe", "scenes", "set_scene")

# 四个业务场景的元信息（顺序与 core.decision_service.DEFAULT_CONFIG["scenes"] 一致）
_DECISION_SCENE_META = (
    ("router", "路由", "本回合用会话主模型还是副模型；换模型失败或低置信自动回退主模型"),
    ("secretary", "秘书", "任务派发给哪个候选会话；失败回退原有派发规则"),
    ("search", "搜索", "一次搜索取回几条结果（3 / 5 / 8）；失败用默认 5 条"),
    ("safety", "安全", "高风险命令是否放行（阈值最高 0.9）；失败回退原判定结果"),
)
_DECISION_SCENES = tuple(item[0] for item in _DECISION_SCENE_META)
_DECISION_SCENE_MODES = ("off", "shadow", "live")
_DECISION_SCENE_MODE_TEXT = {"off": "关闭", "shadow": "影子", "live": "正式"}


def _decision_scene_mode(cfg: dict, scene_cfg: dict) -> str:
    """按两个布尔的组合反推三态（口径与 core.decision_gateway.decide 一致）。"""
    if not isinstance(scene_cfg, dict) or not bool(scene_cfg.get("enabled")):
        return "off"
    if bool(cfg.get("shadow_mode")) or bool(scene_cfg.get("shadow", True)):
        return "shadow"
    return "live"


def _decision_scene_rows(cfg: dict) -> list[dict[str, Any]]:
    """配置 → 四个场景的展示行（三态、阈值、超时；形状容错）。"""
    scenes = cfg.get("scenes") if isinstance(cfg.get("scenes"), dict) else {}
    global_shadow = bool(cfg.get("shadow_mode"))
    rows: list[dict[str, Any]] = []
    for scene, label, description in _DECISION_SCENE_META:
        raw_scene = scenes.get(scene)
        scene_cfg = raw_scene if isinstance(raw_scene, dict) else {}
        mode = _decision_scene_mode(cfg, scene_cfg)
        if not bool(scene_cfg.get("enabled")):
            requested = "off"
        else:
            requested = "shadow" if bool(scene_cfg.get("shadow", True)) else "live"
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
                "mode_text": _DECISION_SCENE_MODE_TEXT[mode],
                "requested_mode": requested,
                "enabled": bool(scene_cfg.get("enabled", False)),
                "shadow": bool(scene_cfg.get("shadow", True)),
                "threshold": threshold,
                "timeout_ms": timeout_ms,
                "global_shadow": global_shadow,
            }
        )
    return rows


def _decision_scene_rows_safe() -> list[dict[str, Any]]:
    from core import decision_service

    try:
        return _decision_scene_rows(decision_service.load_decision_config())
    except Exception:
        return []


def _decision_key_count(cfg: dict) -> int:
    """已配置的决策模型 Key 数量（只取数量，明文绝不出本模块）。"""
    from core import decision_service

    try:
        return len(decision_service._load_keys(cfg, None))
    except Exception:
        return 0


def _decision_settings() -> dict[str, Any]:
    """决策设置快照（配置 + 运行时），密钥一律脱敏。"""
    from core import decision_service

    snapshot = decision_service.get_decision_settings_snapshot()
    cfg = snapshot.get("config") or {}
    runtime = snapshot.get("runtime") or {}
    safe_cfg = _sanitize(cfg)
    return {
        "settings": safe_cfg,
        "runtime": _sanitize(runtime),
        "key_count": _decision_key_count(cfg),
    }


@_guard
def decision_manage(
    action: str = "get",
    payload: dict | None = None,
    scene: str = "",
    mode: str = "",
    threshold: float | None = None,
) -> str:
    """决策模型（JEV）管理：get / set / probe / scenes / set_scene。"""
    from core import decision_service

    act = str(action or "get").strip().lower()
    if act not in DECISION_ACTIONS:
        return _fail(
            f"未知 action「{act or '(空)'}」；合法值为 {' / '.join(DECISION_ACTIONS)}"
        )

    if act == "get":
        return _ok(action="get", **_decision_settings())

    if act == "probe":
        result = decision_service.probe_decision()
        results = result.get("results") or []
        ok = bool(result.get("ok", True))
        payload_out: dict[str, Any] = {
            "action": "probe",
            "model_id": str(result.get("model_id") or ""),
            "results": _sanitize(results),
            "msg": str(result.get("msg") or ""),
        }
        if not ok:
            reason = str(result.get("msg") or "").strip() or "决策模型探测失败"
            if not results:
                return _fail(reason, **payload_out)
            payload_out["ok_count"] = sum(
                1 for item in results if isinstance(item, dict) and item.get("status") == "available"
            )
            payload_out["error"] = reason
            payload_out["ok"] = False
            return _dumps(payload_out)
        return _ok(**payload_out)

    if act == "scenes":
        cfg = decision_service.load_decision_config()
        return _ok(
            action="scenes",
            scenes=_decision_scene_rows(cfg),
            shadow_mode=bool(cfg.get("shadow_mode")),
            enabled=bool(cfg.get("enabled")),
            settings=_sanitize(cfg),
        )

    if act == "set":
        if payload is None:
            return _fail("set 需要 payload（要修改的决策配置键值对）")
        if not isinstance(payload, dict):
            return _fail("payload 必须是对象（键值对）")
        try:
            merged = decision_service.save_decision_config(dict(payload))
        except ValueError as error:
            current: dict[str, Any] = {}
            try:
                current = _sanitize(decision_service.load_decision_config())
            except Exception:
                current = {}
            return _fail(str(error), action="set", settings=current)
        except Exception as error:  # noqa: BLE001 - 写盘失败要带真实原因
            return _fail(f"保存决策设置失败：{error}")
        current_cfg = merged if isinstance(merged, dict) else {}
        return _ok(
            action="set",
            msg="决策设置已保存",
            settings=_sanitize(current_cfg),
            key_count=_decision_key_count(current_cfg),
        )

    # act == "set_scene"
    scene_name = str(scene or "").strip()
    mode_name = str(mode or "").strip().lower()
    if scene_name not in _DECISION_SCENES:
        return _fail(
            f"未知场景「{scene_name or '(空)'}」；合法值为 {' / '.join(_DECISION_SCENES)}",
            scenes=_decision_scene_rows_safe(),
        )
    if mode_name not in _DECISION_SCENE_MODES:
        return _fail(
            f"未知模式「{mode_name or '(空)'}」；合法值为 off / shadow / live",
            scenes=_decision_scene_rows_safe(),
        )

    scene_patch: dict[str, Any] = {"enabled": mode_name != "off"}
    if mode_name == "shadow":
        scene_patch["shadow"] = True
    elif mode_name == "live":
        scene_patch["shadow"] = False
    # off 不动场景自身的 shadow：保留上一次档位，重新开启时语义不变

    if threshold is not None:
        if isinstance(threshold, bool):
            return _fail("阈值必须是 0.0~1.0 之间的数字", scenes=_decision_scene_rows_safe())
        try:
            threshold_value = float(threshold)
        except (TypeError, ValueError):
            return _fail("阈值必须是 0.0~1.0 之间的数字", scenes=_decision_scene_rows_safe())
        if threshold_value != threshold_value:  # NaN：拒绝写入，避免污染配置
            return _fail("阈值必须是 0.0~1.0 之间的数字", scenes=_decision_scene_rows_safe())
        scene_patch["threshold"] = round(max(0.0, min(1.0, threshold_value)), 2)

    try:
        decision_service.save_decision_config({"scenes": {scene_name: scene_patch}})
    except ValueError as error:
        return _fail(str(error), scenes=_decision_scene_rows_safe())
    except Exception as error:  # noqa: BLE001 - 写盘失败要带真实原因
        return _fail(f"保存场景设置失败：{error}", scenes=_decision_scene_rows_safe())

    cfg = decision_service.load_decision_config()
    rows = _decision_scene_rows(cfg)
    row = next((item for item in rows if item["scene"] == scene_name), {}) or {}
    effective_mode = str(row.get("mode") or "off")
    label = str(row.get("label") or scene_name)
    if mode_name == "live" and effective_mode != "live":
        msg = f"{label}场景已标记为正式，但全局影子模式仍开启，当前实际按影子运行"
        hint = "到决策设置里关闭「影子模式」后，本场景才会正式生效"
    elif not bool(cfg.get("enabled")):
        msg = f"{label}场景已保存为{_DECISION_SCENE_MODE_TEXT[mode_name]}"
        hint = "决策模型总开关未启用：场景设置要等总开关打开后才生效"
    elif effective_mode == "live":
        msg = f"{label}场景已切换为正式（决策结果会真实影响任务行为）"
        hint = "正式模式：失败、超时或低置信都会自动回退原逻辑，不会阻断任务"
    elif effective_mode == "shadow":
        msg = f"{label}场景已切换为影子（真发请求、真写审计，结果丢弃）"
        hint = "影子模式用于观察决策质量；确认无误后再切正式"
    else:
        msg = f"{label}场景已关闭（零请求、零记录，走原逻辑）"
        hint = "关闭后该场景不再询问决策模型，行为与未接入时一致"
    return _ok(
        action="set_scene",
        msg=msg,
        hint=hint,
        scene=row,
        scenes=rows,
        requested_mode=mode_name,
        effective_mode=effective_mode,
        mode_text=_DECISION_SCENE_MODE_TEXT.get(effective_mode, "关闭"),
        global_shadow=bool(cfg.get("shadow_mode")),
        settings=_sanitize(cfg),
    )


# ---------------------------------------------------------------------------
# 工具契约
# ---------------------------------------------------------------------------

def build_specs() -> list[ToolSpec]:
    """返回 3 个集成管理工具契约（供工具注册表装配）。"""
    return [
        ToolSpec(
            name="feishu_manage",
            description=(
                "管理飞书通道集成：get 读取当前设置（app_secret 只回是否已配置）、"
                "set 保存开关/App ID/App Secret/审批人/通知群、test 用真实请求校验凭据、"
                "status 查看长连接运行状态与最近错误。"
            ),
            input_schema=_schema(
                {
                    "action": {
                        "type": "string",
                        "enum": list(FEISHU_ACTIONS),
                        "description": "get 读设置 / set 保存 / test 测试连接 / status 运行状态",
                    },
                    "payload": {
                        "type": "object",
                        "description": (
                            "set 时的设置项：enabled、app_id、app_secret、approvers、"
                            "notify_chat_id；test 时可传 app_id / app_secret 临时校验"
                        ),
                    },
                },
                required=["action"],
            ),
            execute=feishu_manage,
            read_only=False,
            mutates_files=False,
            requires_permission=True,
            cancellable=True,
            parallel_safe=False,
            activity_description="管理飞书通道",
            side_effect_scope="network",
        ),
        ToolSpec(
            name="lan_manage",
            description=(
                "管理局域网手机远程控制服务：status 查看监听地址/端口/运行状态、"
                "start 启动服务、stop 停止服务。单例管理，不会重复启动同一进程内的服务。"
            ),
            input_schema=_schema(
                {
                    "action": {
                        "type": "string",
                        "enum": list(LAN_ACTIONS),
                        "description": "status 查状态 / start 启动 / stop 停止",
                    }
                },
                required=["action"],
            ),
            execute=lan_manage,
            read_only=False,
            mutates_files=False,
            requires_permission=True,
            cancellable=True,
            parallel_safe=False,
            activity_description="管理远程服务",
            side_effect_scope="system",
        ),
        ToolSpec(
            name="decision_manage",
            description=(
                "管理 JEV 决策模型：get 读配置与运行状态（密钥脱敏）、set 保存开关与全局参数、"
                "probe 逐 Key 真实探测可用性、scenes 查看四个业务场景三态、"
                "set_scene 设置单个场景（off 关闭 / shadow 影子 / live 正式，可同时改阈值）。"
            ),
            input_schema=_schema(
                {
                    "action": {
                        "type": "string",
                        "enum": list(DECISION_ACTIONS),
                        "description": (
                            "get 读设置 / set 保存全局配置 / probe 探测模型 / "
                            "scenes 查场景 / set_scene 改场景"
                        ),
                    },
                    "payload": {
                        "type": "object",
                        "description": (
                            "set 时的配置 patch，如 {enabled, shadow_mode, model_id, "
                            "timeout_ms, base_url, endpoint_style, key_mode}"
                        ),
                    },
                    "scene": {
                        "type": "string",
                        "enum": list(_DECISION_SCENES),
                        "description": "set_scene 的目标场景：router / secretary / search / safety",
                    },
                    "mode": {
                        "type": "string",
                        "enum": list(_DECISION_SCENE_MODES),
                        "description": "set_scene 的三态：off 关闭 / shadow 影子 / live 正式",
                    },
                    "threshold": {
                        "type": "number",
                        "description": "set_scene 可选阈值，0.0~1.0（越界钳制）",
                    },
                },
                required=["action"],
            ),
            execute=decision_manage,
            read_only=False,
            mutates_files=False,
            requires_permission=True,
            cancellable=True,
            parallel_safe=False,
            activity_description="管理决策模型",
            side_effect_scope="network",
        ),
    ]


__all__ = [
    "build_specs",
    "feishu_manage",
    "lan_manage",
    "decision_manage",
]

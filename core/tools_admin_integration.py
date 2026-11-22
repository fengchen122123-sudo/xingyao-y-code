from __future__ import annotations

import functools
import json
import socket
import threading
from typing import Any

from core.tool_protocol import ToolSpec


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


FEISHU_ACTIONS = ("get", "set", "test", "status")

_FEISHU_WRITABLE_KEYS = ("enabled", "app_id", "app_secret", "approvers", "notify_chat_id")


def _feishu_status_snapshot() -> dict[str, Any]:
    from core.feishu.service import feishu_service

    status = feishu_service.status()
    return status if isinstance(status, dict) else {}


def _feishu_settings() -> dict[str, Any]:
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


LAN_ACTIONS = ("status", "start", "stop")

_LAN_LOCK = threading.Lock()
# 本工具进程内单例（避免重复启动；不持有 gui_bridge 的实例）
_LAN_STATE: dict[str, Any] = {"server": None}


def _lan_owned_server() -> Any:
    return _LAN_STATE.get("server")


def _lan_running(server: Any) -> bool:
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
    try:
        import config as app_config

        port = int(app_config._load().get(app_config.REMOTE_PAIR_PORT_KEY) or 0)
    except Exception:
        return 0
    return port if 1024 <= port <= 65535 else 0


def _lan_snapshot(server: Any = None) -> dict[str, Any]:
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


DECISION_ACTIONS = ("get", "set", "probe", "scenes", "set_scene")


def _decision_scene_names() -> list[str]:
    from core import decision_service

    return [item[0] for item in decision_service.DECISION_SCENE_META]


def _decision_scene_modes() -> list[str]:
    from core import decision_service

    return list(decision_service.DECISION_SCENE_MODES)


def _decision_scene_rows(cfg: dict) -> list[dict[str, Any]]:
    from core import decision_service

    return decision_service.build_decision_scene_rows(cfg)


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

    res = decision_service.update_decision_scene(scene, mode, threshold)
    if not res.get("ok"):
        return _fail(str(res.get("msg") or "保存场景设置失败"), scenes=res.get("scenes") or [])

    return _ok(
        action="set_scene",
        msg=res["msg"],
        hint=res["hint"],
        scene=res["scene"],
        scenes=res["scenes"],
        mode=res["mode"],
        mode_text=res["mode_text"],
        settings=_sanitize(res["settings"]),
    )


def build_specs() -> list[ToolSpec]:
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
                "probe 逐 Key 真实探测可用性、scenes 查看三个业务场景开关、"
                "set_scene 设置单个场景（off 关闭 / live 开启，可同时改阈值）。"
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
                            "set 时的配置 patch，如 {enabled, model_id, "
                            "timeout_ms, base_url, endpoint_style, key_mode}"
                        ),
                    },
                    "scene": {
                        "type": "string",
                        "enum": _decision_scene_names(),
                        "description": "set_scene 的目标场景：secretary / search / safety",
                    },
                    "mode": {
                        "type": "string",
                        "enum": _decision_scene_modes(),
                        "description": "set_scene 的开关：off 关闭 / live 开启",
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

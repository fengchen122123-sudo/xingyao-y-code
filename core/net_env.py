from __future__ import annotations

import os
import socket

PROXY_ENV_VARS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
)

_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
_PROBE_TIMEOUT = 0.3


def _parse_local_target(value: str):
    text = (value or "").strip()
    if not text:
        return None, None
    if "://" in text:
        text = text.split("://", 1)[1]
    text = text.rstrip("/")
    host, sep, port = text.rpartition(":")
    if not sep or not port.isdigit():
        return None, None
    host = host.strip().strip("[]").lower()
    if host not in _LOCAL_HOSTS:
        return None, None
    return host, int(port)


def _port_alive(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=_PROBE_TIMEOUT):
            return True
    except OSError:
        return False


def sanitize_dead_local_proxy(log=None) -> list:
    """移除指向本机但端口未监听的代理环境变量，返回被移除的变量名列表。"""
    cleared = []
    for name in PROXY_ENV_VARS:
        value = os.environ.get(name)
        if not value:
            continue
        host, port = _parse_local_target(value)
        if port is None or _port_alive(host, port):
            continue
        os.environ.pop(name, None)
        cleared.append(name)
    if cleared and log is not None:
        try:
            log("已移除失效的本机代理环境变量：" + ", ".join(cleared))
        except Exception:
            pass
    return cleared

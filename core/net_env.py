"""启动期网络环境自愈：清掉"指向本机、但已经没人监听"的代理变量。

背景：本机代理客户端（如 FengWoMeta 这类 mihomo 内核客户端）退出后，终端会话里可能
残留 HTTP_PROXY/HTTPS_PROXY=http://127.0.0.1:17980。任何从该会话继承环境启动的进程
都会把外网请求发往一个没人监听的端口，统一报 WinError 10061（目标计算机积极拒绝），
对外表现为"登录失败""模型连接失败"，看起来像服务端故障，实际是本机代理残影。

这里在启动最早阶段做一次极短探测：变量指向本机而端口不通就移除；指向远程代理的
一律不动（远程代理断开属于对方问题，不该由我们擅自改配置）。任何异常静默放过，
绝不影响启动。
"""
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
    """解析代理地址，返回 (host, port)；不是本机地址则返回 (None, None)。"""
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

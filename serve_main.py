"""无窗口 serve 入口。由 gui_main.py --serve 在导入 GUI 之前转进来。

这一段只保证进程能在 Linux 上起来、健康检查可访问、没有客户端时权限会拒绝。
不启动浏览器，不打开 8765。
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

logger = logging.getLogger("serve_main")


def _data_dir() -> Path:
    env_dir = os.environ.get("YCODE_DATA_DIR")
    if env_dir and str(env_dir).strip():
        return Path(env_dir)
    return Path.home() / ".ycode"


def _configure_logging() -> None:
    log_dir = _data_dir() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        log_dir / "serve-runtime.log",
        maxBytes=2 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    root.addHandler(stream)


def _audit_config_or_exit() -> None:
    try:
        from config import UnknownConfigKeysError, audit_config_keys

        audit_config_keys()
    except UnknownConfigKeysError as error:
        logger.error("配置校验失败（strict_config=true）：%s", error)
        raise SystemExit(2)
    except Exception:
        logger.debug("配置未知键自检失败（忽略）", exc_info=True)


def _listen_endpoint() -> tuple[str, int]:
    from core.serve import SERVE_BIND, SERVE_PORT

    bind = os.environ.get("YCODE_SERVE_BIND", SERVE_BIND).strip() or SERVE_BIND
    raw_port = os.environ.get("YCODE_SERVE_PORT", str(SERVE_PORT)).strip()
    try:
        port = int(raw_port)
    except ValueError:
        logger.error("YCODE_SERVE_PORT 不是整数: %s", raw_port)
        raise SystemExit(2)
    if not 1 <= port <= 65535:
        logger.error("YCODE_SERVE_PORT 超出范围: %s", port)
        raise SystemExit(2)
    return bind, port


def _start_computer_runtime():
    """热键在无窗口进程里直接放弃。浏览器留给后面的切片。"""
    from core.computer.models import ComputerControlError
    from core.computer.runtime import attach_preview_publisher, start_computer_runtime

    def _no_hotkey():
        raise ComputerControlError("serve 进程不注册全局热键")

    runtime = start_computer_runtime(hotkey_factory=_no_hotkey)
    attach_preview_publisher(runtime)
    return runtime


def main() -> int:
    os.environ["YCODE_TOOL_PROFILE"] = "server"
    _configure_logging()
    logger.info("serve 进程启动 pid=%s", os.getpid())
    _audit_config_or_exit()

    from core.serve.lock import ServeLock

    lock = ServeLock(_data_dir() / "serve.lock")
    if not lock.acquire():
        logger.error("已有 serve 进程持有 %s，本次退出", lock.path)
        return 1

    api = None
    try:
        try:
            from core.plugin_loader import set_trust_prompt

            set_trust_prompt(lambda _path, _name, _fingerprint: False)
        except Exception:
            logger.debug("注册插件信任回调失败（不影响启动）", exc_info=True)
        try:
            from core.mcp_client import mcp_manager
        except Exception:
            mcp_manager = None
            logger.debug("MCP 客户端不可用（不影响启动）", exc_info=True)

        from gui_bridge import ApiBridge
        from core.lan_server import PROCESS_BOOT_ID, LanServer
        from core.serve.permission_policy import install_serve_permission_policy

        try:
            from core.serve.keyring_setup import install_server_keyring

            if not install_server_keyring(_data_dir()):
                logger.info("未配置钥匙串口令文件，模型 Key 暂不能加密落盘")
        except Exception:
            logger.exception("安装文件钥匙串失败")

        api = ApiBridge(mcp_manager=mcp_manager)
        bind, port = _listen_endpoint()
        lan = LanServer(bridge=api, port=port, bind=bind)
        info = lan.start(strict_port=True)
        api._lan_server = lan
        install_serve_permission_policy(api, client_count=lan.client_count)
        logger.info(
            "serve 正在监听 http://%s:%s/health boot_id=%s",
            info.get("host"),
            info.get("port"),
            PROCESS_BOOT_ID,
        )
        try:
            _start_computer_runtime()
        except Exception:
            logger.exception("电脑操控运行时未启动（健康检查仍可用）")

        stop = threading.Event()

        def _handle(signum, _frame):
            logger.info("收到信号 %s，开始退出", signum)
            stop.set()

        signal.signal(signal.SIGTERM, _handle)
        signal.signal(signal.SIGINT, _handle)
        stop.wait()
        return 0
    finally:
        if api is not None:
            try:
                api.shutdown()
            except Exception:
                logger.exception("退出时关闭 ApiBridge 失败")
        try:
            from core.computer.runtime import shutdown_process_computer_runtime

            shutdown_process_computer_runtime("serve_exit")
        except Exception:
            logger.exception("退出时关闭电脑操控运行时失败")
        lock.release()


if __name__ == "__main__":
    raise SystemExit(main())

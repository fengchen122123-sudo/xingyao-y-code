"""工人容器启动前先解锁共享钥匙串。口令或数据库缺失就明确退出，不留半个进程。

由 deploy/worker.Dockerfile 的 CMD 在 gui_main 之前调用：
    python -m gateway.worker_bootstrap && exec python gui_main.py --serve
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def main() -> int:
    data_dir = Path(os.environ.get("YCODE_DATA_DIR") or "/data")
    try:
        from core.serve.keyring_setup import install_server_keyring, keyring_file
    except ImportError as error:  # pragma: no cover - 镜像里依赖缺失
        print(f"[worker-bootstrap] 缺少钥匙串依赖：{error}", file=sys.stderr)
        return 1
    path = keyring_file(data_dir)
    if not install_server_keyring(data_dir):
        print(
            "[worker-bootstrap] 缺少钥匙串口令：请注入 YCODE_KEYRING_PASSWORD_FILE"
            f"（当前 {os.environ.get('YCODE_KEYRING_PASSWORD_FILE', '未设置')}）",
            file=sys.stderr,
        )
        return 1
    if not path.is_file():
        print(f"[worker-bootstrap] 共享钥匙串数据库不存在：{path}", file=sys.stderr)
        return 1
    print(f"[worker-bootstrap] 钥匙串已就绪 {path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

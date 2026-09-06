"""受管浏览器执行进程的测试桩：纯协议回声，绝不启动 Playwright/浏览器。

模式（--mode）：
- normal：ready → 请求逐一响应（observe 返回罐装观察并写入假 PNG）；
- hang_command：ready 后对所有请求永不响应（模拟 Playwright 调用卡死）；
- hang_boot：永不发 ready（模拟启动挂死）；
- no_exit_on_close：响应 close=ok 后继续存活（测试硬终止路径）；
- crash_after_first：第一个请求正常响应后立刻退出（模拟进程崩溃）。

--pid-file：ready 后把自身 pid 写入该文件，供测试断言进程真实退出。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path


def _emit(message: dict) -> None:
    sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", required=True)
    parser.add_argument("--pid-file", default="")
    args = parser.parse_args()

    if args.mode == "hang_boot":
        time.sleep(3600)
        return 1

    screenshot_dir = tempfile.mkdtemp(prefix="ycode-stub-shots-")
    counter = {"n": 0}

    def observation_for(run_id: str) -> dict:
        counter["n"] += 1
        png_path = Path(screenshot_dir) / f"shot-{counter['n']}.png"
        png_path.write_bytes(b"\x89PNG\r\n\x1a\nstub")
        return {
            "run_id": run_id,
            "target": "browser",
            "action": "observe",
            "ok": True,
            "screen_sha256": f"stub-sha-{counter['n']}",
            "png_path": str(png_path),
            "width": 1440,
            "height": 900,
            "original_width": 1440,
            "original_height": 900,
            "origin_x": 0,
            "origin_y": 0,
            "scale_x": 1.0,
            "scale_y": 1.0,
            "window_title": "",
            "page_origin": "https://stub.example",
            "page_title": "Stub",
            "surface_id": "page-1",
            "surface_generation": 1,
        }

    _emit(
        {
            "event": "ready",
            "browser_pid": 555001,
            "screenshot_dir": screenshot_dir,
        }
    )
    if args.pid_file:
        Path(args.pid_file).write_text(str(os.getpid()), encoding="ascii")

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        request = json.loads(line)
        command_id = request["id"]
        op = request["op"]
        if args.mode == "hang_command":
            time.sleep(3600)  # 永不响应
        if op == "observe":
            _emit(
                {
                    "id": command_id,
                    "ok": True,
                    "observation": observation_for(str(request.get("run_id") or "")),
                }
            )
        elif op == "close":
            _emit({"id": command_id, "ok": True})
            if args.mode == "no_exit_on_close":
                time.sleep(3600)  # 假装收尾成功但不退出
            return 0
        else:
            _emit({"id": command_id, "ok": True})
        if args.mode == "crash_after_first":
            os._exit(1)
    return 0


if __name__ == "__main__":
    sys.exit(main())

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
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
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
            time.sleep(3600)
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
                time.sleep(3600)
            return 0
        else:
            _emit({"id": command_id, "ok": True})
        if args.mode == "crash_after_first":
            os._exit(1)
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""受管浏览器执行进程入口（R2 第二段）：``python -m core.computer.browser_worker``。

职责：独占一个 Playwright 实例/context/page 的执行边界。父进程不持有任何
Playwright 对象；本进程从 stdin 读结构化请求（协议见 browser_worker_protocol），
经进程内的 BrowserComputerBackend（线程模式，与既有单 worker 线程语义一致）
执行，再把观察元数据/错误写回 stdout。

崩溃与卡死边界：本进程挂死时父进程按有界流程先收 admission、再终止本进程
与其能确认归属的浏览器进程树（Windows Job Object，KILL_ON_JOB_CLOSE），等待
退出后才释放 profile/租约。本进程绝不记录动作正文/截图/凭据。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _emit(message: dict) -> None:
    sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _boot_failed(code: str, message: str) -> int:
    _emit({"event": "boot_failed", "error_code": code, "error_message": message})
    return 1


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser", required=True, choices=("edge", "chrome", "chromium"))
    parser.add_argument("--profile", required=True)
    parser.add_argument("--command-timeout", type=float, default=30.0)
    parser.add_argument("--viewport-width", type=int, default=1440)
    parser.add_argument("--viewport-height", type=int, default=900)
    args = parser.parse_args(argv)

    from core.computer.browser_backend import BrowserComputerBackend
    from core.computer.browser_worker_protocol import (
        decode_request,
        encode_response,
        normalize_action_payload,
        observation_payload,
    )
    from core.computer.models import ComputerAction, ComputerControlError
    from core.computer.platforms import get_platform_adapter

    # R5 受控页裁决的系统侧窗口提示由平台层提供（与生产父进程接线一致）。
    hint_provider = getattr(get_platform_adapter(), "browser_window_hints", None)
    if hint_provider is not None and not callable(hint_provider):
        hint_provider = None
    backend = BrowserComputerBackend(
        args.browser,
        Path(args.profile),
        command_timeout=args.command_timeout,
        window_hint_provider=hint_provider,
        viewport=(args.viewport_width, args.viewport_height),
    )
    try:
        backend.start()
    except ComputerControlError as error:
        return _boot_failed(error.code, error.message)
    except Exception as error:  # 防御：启动异常统一归一为稳定错误码
        return _boot_failed(
            "computer_backend_start_failed", f"浏览器 backend 启动失败：{type(error).__name__}"
        )
    _emit(
        {
            "event": "ready",
            "browser_pid": int(backend._browser_pid or 0),
            "screenshot_dir": str(backend._screenshot_dir or ""),
        }
    )

    try:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                request = decode_request(line)
            except (ValueError, json.JSONDecodeError) as error:
                # 会话期协议错误用独立事件名：父进程把 boot_failed 记进
                # _boot_error（启动后无人再读），语义误导且难排查
                _emit(
                    {
                        "event": "worker_protocol_error",
                        "error_code": "computer_provider_protocol_error",
                        "error_message": f"受管进程请求不合法：{error}",
                    }
                )
                return 1
            command_id = request["id"]
            op = request["op"]
            try:
                if op == "observe":
                    observation = backend.observe(str(request.get("run_id") or ""))
                    _emit(
                        {
                            "id": command_id,
                            "ok": True,
                            "observation": observation_payload(observation),
                        }
                    )
                elif op == "execute":
                    action = ComputerAction(
                        **normalize_action_payload(request["action"])
                    )
                    backend.execute(str(request.get("run_id") or ""), action)
                    _emit({"id": command_id, "ok": True})
                elif op == "release":
                    backend.release_inputs()
                    _emit({"id": command_id, "ok": True})
                else:  # close（白名单内最后一个）
                    backend.close()
                    _emit({"id": command_id, "ok": True})
                    return 0
            except ComputerControlError as error:
                _emit(
                    {
                        "id": command_id,
                        "ok": False,
                        "error_code": error.code,
                        "error_message": error.message,
                    }
                )
            except Exception as error:
                _emit(
                    {
                        "id": command_id,
                        "ok": False,
                        "error_code": "computer_backend_error",
                        "error_message": f"受管执行进程异常：{type(error).__name__}",
                    }
                )
    finally:
        # 父进程 EOF：尽力收尾（真实关闭由父进程的有界终止流程兜底）。
        try:
            backend.close()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())

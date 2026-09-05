"""Manual smoke probe for Y-CODE computer control prerequisites.

This script is NEVER run by automated tests. It must only be executed by a
human who explicitly allows a visible system browser window to open or a real
desktop screenshot / mouse move / click / typing. It does not download
browsers and only navigates to about:blank.

Modes:
- --probe-browser edge|chrome: raw Playwright channel probe on about:blank.
- --browser-observe edge|chrome: start the dedicated BrowserComputerBackend,
  take exactly one observation (screenshot only, no input), print the redacted
  summary (origin / truncated title / sha256), then close.
- --desktop-observe: start the real WindowsComputerBackend (preflight +
  exactly one desktop screenshot, no input), print the redacted summary, close.
- --desktop-input --confirm-real-input I_UNDERSTAND: perform exactly one
  move + one click + one fixed harmless type in the visible test area.
  Before EVERY step the script prints the redacted action it is about to
  perform and waits for a single Enter; any other input aborts immediately.
  The user must first open a non-sensitive test window (e.g. an empty editor
  or blank page) in the foreground. Missing or wrong confirmation exits 2
  without touching anything. release/stop is called after the test.
"""

import argparse
import sys

# --desktop-input 固定敲入的无敏感测试文本（可见测试区域）。
SMOKE_TYPE_TEXT = "ycode smoke"


def _confirm_step(step_number: int, description: str) -> bool:
    """单步确认：打印脱敏动作描述，等待单次 Enter；非空输入立即中止。"""
    print(f"\n[步骤 {step_number}] 即将执行：{description}")
    try:
        answer = input("确认执行请按一次 Enter（输入其他内容并回车 = 立即中止）：")
    except (EOFError, KeyboardInterrupt):
        print("\n已中止：未获得单步确认。")
        return False
    if answer.strip() != "":
        print("已中止：单步确认要求单次 Enter。")
        return False
    return True


def probe_browser(browser: str) -> int:
    from playwright.sync_api import sync_playwright

    channel = {"edge": "msedge", "chrome": "chrome"}[browser]
    with sync_playwright() as runtime:
        instance = runtime.chromium.launch(channel=channel, headless=False)
        try:
            page = instance.new_page(viewport={"width": 1440, "height": 900})
            page.goto("about:blank")
            assert page.viewport_size == {"width": 1440, "height": 900}
        finally:
            instance.close()
    return 0


def browser_observe(browser: str) -> int:
    """Observation-only probe through the real backend: screenshot, no input."""
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from core.computer.browser_backend import BrowserComputerBackend
    from core.computer.models import ComputerControlError
    from core.computer.settings import get_browser_profile_dir

    try:
        backend = BrowserComputerBackend(browser, get_browser_profile_dir(browser))
        backend.start()
        try:
            observation = backend.observe("smoke-browser-observe")
        finally:
            backend.close()
    except ComputerControlError as error:
        print(f"[{error.code}] {error.message}", file=sys.stderr)
        return 1
    print(
        "observe ok: "
        f"target={observation.target} "
        f"viewport={observation.width}x{observation.height} "
        f"sha256={observation.screen_sha256} "
        f"origin={observation.page_origin} "
        f"title={observation.page_title!r}"
    )
    return 0


def _repo_root_on_path() -> None:
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def desktop_observe() -> int:
    """Desktop observation-only probe: preflight + one screenshot, no input."""
    _repo_root_on_path()
    from core.computer.models import ComputerControlError
    from core.computer.windows_backend import WindowsComputerBackend

    try:
        backend = WindowsComputerBackend.create_default()
        backend.start()
        try:
            observation = backend.observe("smoke-desktop-observe")
        finally:
            backend.close()
    except ComputerControlError as error:
        print(f"[{error.code}] {error.message}", file=sys.stderr)
        return 1
    print(
        "observe ok: "
        f"target={observation.target} "
        f"screen={observation.width}x{observation.height} "
        f"original={observation.original_width}x{observation.original_height} "
        f"origin=({observation.origin_x},{observation.origin_y}) "
        f"sha256={observation.screen_sha256} "
        f"window={observation.window_title!r}"
    )
    return 0


def desktop_input(confirm: str | None) -> int:
    """Real-input probe: one move + one click + one fixed type in the visible
    test area; every step requires a single Enter after a redacted preview."""
    if confirm != "I_UNDERSTAND":
        print(
            "--desktop-input 会移动/点击真实鼠标并敲入固定测试文本，必须显式附加 "
            "--confirm-real-input I_UNDERSTAND",
            file=sys.stderr,
        )
        return 2
    print(
        "前置要求：请先打开一个无敏感数据的测试窗口（如空白记事本/空白页）\n"
        "并保持在前台；后续 move/click 会作用于屏幕中心的可见测试区域。"
    )
    if not _confirm_step(0, "先截取一次桌面观察（只截图，不输入）"):
        return 2
    _repo_root_on_path()
    from core.computer.models import ComputerAction, ComputerControlError
    from core.computer.windows_backend import WindowsComputerBackend

    try:
        backend = WindowsComputerBackend.create_default()
        backend.start()
        try:
            observation = backend.observe("smoke-desktop-input")
            center_x = observation.width // 2
            center_y = observation.height // 2
            steps = [
                (
                    1,
                    f"移动鼠标到屏幕中心（截图坐标 {center_x}x{center_y}）",
                    ComputerAction(kind="move", target="desktop", x=center_x, y=center_y),
                ),
                (
                    2,
                    "在屏幕中心点击一次（左键单击）",
                    ComputerAction(kind="click", target="desktop", x=center_x, y=center_y),
                ),
                (
                    3,
                    f"敲入固定测试文本（长度 {len(SMOKE_TYPE_TEXT)} 字符，内容已脱敏）",
                    ComputerAction(
                        kind="type_text", target="desktop", text=SMOKE_TYPE_TEXT
                    ),
                ),
            ]
            for step_number, description, action in steps:
                if not _confirm_step(step_number, description):
                    return 2
                backend.execute("smoke-desktop-input", action)
            # 测试后必须释放按下的键鼠并停止 backend（brief Step 5）
            backend.release_inputs()
        finally:
            backend.close()
    except ComputerControlError as error:
        print(f"[{error.code}] {error.message}", file=sys.stderr)
        return 1
    print(
        "input ok: move/click/type 已在可见测试区域完成，输入状态已释放，"
        "backend 已关闭。"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Y-CODE computer control manual smoke probe (requires explicit arguments).",
    )
    parser.add_argument(
        "--probe-browser",
        choices=("edge", "chrome"),
        help="Open the selected system browser channel on about:blank and verify the 1440x900 viewport.",
    )
    parser.add_argument(
        "--browser-observe",
        choices=("edge", "chrome"),
        help="Start the dedicated browser backend, take one observation (screenshot only, no input), then close.",
    )
    parser.add_argument(
        "--desktop-observe",
        action="store_true",
        help="Run desktop preflight and take exactly one screenshot (no input), then close.",
    )
    parser.add_argument(
        "--desktop-input",
        action="store_true",
        help=(
            "Perform one REAL move + click + fixed-harmless type in the visible test "
            "area; every step prints a redacted preview and waits for a single Enter. "
            "Requires --confirm-real-input I_UNDERSTAND."
        ),
    )
    parser.add_argument(
        "--confirm-real-input",
        default=None,
        help="Required acknowledgement for --desktop-input; must be exactly I_UNDERSTAND.",
    )
    args = parser.parse_args(argv)
    if args.probe_browser:
        return probe_browser(args.probe_browser)
    if args.browser_observe:
        return browser_observe(args.browser_observe)
    if args.desktop_observe:
        return desktop_observe()
    if args.desktop_input:
        return desktop_input(args.confirm_real_input)
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())

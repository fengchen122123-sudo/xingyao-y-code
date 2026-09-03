"""Manual smoke probe for Y-CODE computer control prerequisites.

This script is NEVER run by automated tests. It must only be executed by a
human who explicitly allows a visible system browser window to open or a real
desktop screenshot / mouse move. It does not download browsers and only
navigates to about:blank.

Modes:
- --probe-browser edge|chrome: raw Playwright channel probe on about:blank.
- --browser-observe edge|chrome: start the dedicated BrowserComputerBackend,
  take exactly one observation (screenshot only, no input), print the redacted
  summary (origin / truncated title / sha256), then close.
- --desktop-observe: start the real WindowsComputerBackend (preflight +
  exactly one desktop screenshot, no input), print the redacted summary, close.
- --desktop-input --confirm-real-input I_UNDERSTAND: additionally move the
  real mouse once to the screenshot center. Missing or wrong confirmation
  exits 2 without touching anything.
"""

import argparse
import sys


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
    """Real-input probe: moves the real mouse once; requires explicit confirmation."""
    if confirm != "I_UNDERSTAND":
        print(
            "--desktop-input 会移动真实鼠标，必须显式附加 "
            "--confirm-real-input I_UNDERSTAND",
            file=sys.stderr,
        )
        return 2
    _repo_root_on_path()
    from core.computer.models import ComputerAction, ComputerControlError
    from core.computer.windows_backend import WindowsComputerBackend

    try:
        backend = WindowsComputerBackend.create_default()
        backend.start()
        try:
            observation = backend.observe("smoke-desktop-input")
            backend.execute(
                "smoke-desktop-input",
                ComputerAction(
                    kind="move",
                    target="desktop",
                    x=observation.width // 2,
                    y=observation.height // 2,
                ),
            )
            backend.release_inputs()
        finally:
            backend.close()
    except ComputerControlError as error:
        print(f"[{error.code}] {error.message}", file=sys.stderr)
        return 1
    print(
        "input ok: moved mouse to screenshot center "
        f"({observation.width // 2}, {observation.height // 2})"
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
        help="Move the REAL mouse once to the screenshot center. Requires --confirm-real-input I_UNDERSTAND.",
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

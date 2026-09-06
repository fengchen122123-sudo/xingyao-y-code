"""Local latency benchmark. It uses only a loopback page and desktop screenshots."""
from __future__ import annotations

import argparse
import ctypes
import importlib.util
import json
import os
import platform
import statistics
import subprocess
import tempfile
import time
from pathlib import Path


def _audit():
    path = Path(__file__).with_name("audit_computer_live.py")
    spec = importlib.util.spec_from_file_location("ycode_audit_benchmark", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def stats(values):
    values = sorted(values)
    return {"samples": len(values), "p50_ms": statistics.median(values) if values else None,
            "p95_ms": values[max(0, int(len(values) * .95) - 1)] if values else None,
            "min_ms": min(values) if values else None, "max_ms": max(values) if values else None}


def measure(fn, count):
    values, errors = [], []
    for _ in range(count):
        started = time.perf_counter()
        try:
            fn()
            values.append(round((time.perf_counter() - started) * 1000, 3))
        except Exception as exc:
            errors.append({"error_code": getattr(exc, "code", ""), "error": str(exc)})
    result = stats(values)
    result["errors"] = errors
    return result


def environment(browser):
    screen = {"width": None, "height": None}
    dpi = None
    if os.name == "nt":
        user32 = ctypes.windll.user32
        screen = {"width": user32.GetSystemMetrics(0), "height": user32.GetSystemMetrics(1)}
        try:
            dpi = ctypes.windll.user32.GetDpiForSystem()
        except AttributeError:
            pass
    cpu = None
    memory = None
    try:
        import psutil
        cpu = {"model": platform.processor(), "logical": psutil.cpu_count(logical=True), "physical": psutil.cpu_count(logical=False)}
        memory = psutil.virtual_memory().total
    except Exception:
        cpu = {"model": platform.processor()}
    browser_version = None
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            executable = getattr(getattr(pw, browser), "executable_path", None)
            if executable:
                browser_version = subprocess.run([executable, "--version"], capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception as exc:
        browser_version = {"error": str(exc)}
    return {"os": platform.platform(), "cpu": cpu, "physical_memory_bytes": memory, "screen_physical": screen, "dpi": dpi, "browser": browser, "browser_version": browser_version}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", type=int, default=20)
    parser.add_argument("--browser", choices=["edge", "chrome"], default="edge")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.samples < 1:
        parser.error("--samples must be positive")
    audit = _audit()
    from core.computer.browser_backend import BrowserComputerBackend
    from core.computer.models import ComputerAction
    from core.computer.windows_backend import WindowsComputerBackend

    cold = []
    for _ in range(args.samples):
        with tempfile.TemporaryDirectory(prefix="ycode-bench-") as directory:
            backend = BrowserComputerBackend(args.browser, Path(directory) / "profile", use_process_worker=True)
            started = time.perf_counter()
            try:
                backend.start()
                cold.append(round((time.perf_counter() - started) * 1000, 3))
            finally:
                backend.close()

    with tempfile.TemporaryDirectory(prefix="ycode-bench-hot-") as directory:
        backend = BrowserComputerBackend(args.browser, Path(directory) / "profile", use_process_worker=True)
        server = audit.ThreadingHTTPServer(("127.0.0.1", 0), audit.Handler)
        import threading
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            backend.start()
            backend.execute("benchmark", ComputerAction(kind="navigate", target="browser", url=f"http://127.0.0.1:{server.server_port}"))
            hot_click = measure(lambda: backend.execute("benchmark", ComputerAction(kind="click", target="browser", x=150, y=75)), args.samples)
            hot_observe = measure(lambda: backend.observe("benchmark"), args.samples)
        finally:
            backend.close(); server.shutdown(); server.server_close()

    desktop_start = measure(lambda: (lambda b: (b.start(), b.close()))(WindowsComputerBackend.create_default()), args.samples)
    desktop = WindowsComputerBackend.create_default(); desktop.start()
    try:
        desktop_observe = measure(lambda: desktop.observe("benchmark"), args.samples)
    finally:
        desktop.close()
    output = {"environment": environment(args.browser), "browser": {"cold_start": stats(cold), "hot_click": hot_click, "hot_observe": hot_observe}, "desktop": {"start": desktop_start, "observe": desktop_observe}}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

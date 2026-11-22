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


def measure_stop_and_lease(service, owner, capability, target: str = "browser") -> dict:
    """测量停止响应与 lease 释放耗时，供 Task 0 门禁基线采数使用。"""
    run = service.start_run(owner, capability, target=target)
    started = time.perf_counter()
    snap = service.stop(run.run_id, "benchmark_stop")
    elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
    return {
        "run_id": run.run_id,
        "status": snap.status,
        "stop_response_ms": elapsed_ms,
        "lease_release_ms": elapsed_ms,
        "lease_released": service._run is None,
    }


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
            backend.observe("benchmark")
            hot_click = measure(lambda: backend.execute("benchmark", ComputerAction(kind="click", target="browser", x=150, y=75)), args.samples)
            hot_observe = measure(lambda: backend.observe("benchmark"), args.samples)
        finally:
            backend.close(); server.shutdown(); server.server_close()

    desktop_start = measure(lambda: (lambda b: (b.start(), b.close()))(WindowsComputerBackend.create_default()), args.samples)
    desktop = WindowsComputerBackend.create_default()
    desktop_error = ""
    try:
        desktop.start()
        desktop_observe = measure(lambda: desktop.observe("benchmark"), args.samples)
    except Exception as exc:
        desktop_error = f"{getattr(exc, 'code', '') or type(exc).__name__}: {exc}"
        desktop_observe = {"samples": 0, "p50_ms": None, "p95_ms": None, "min_ms": None, "max_ms": None, "errors": [{"error_code": getattr(exc, "code", ""), "error": str(exc)}]}
    finally:
        desktop.close()
    from core.computer.controller import ComputerControlService
    from core.computer.models import ComputerCapability, ComputerOwner, ComputerProtocol
    import threading

    stop_samples = []
    with tempfile.TemporaryDirectory(prefix="ycode-bench-stop-") as directory:
        svc = ComputerControlService(
            backend_factory=lambda _t, _i: BrowserComputerBackend(
                args.browser, Path(directory) / "profile", use_process_worker=True
            ),
            generation_provider=lambda: 1,
            abort_streams=lambda _e: None,
        )
        cap = ComputerCapability(
            enabled=True,
            protocol=ComputerProtocol.GENERIC.value,
            targets=("browser",),
            browser=args.browser,
            settings_generation=1,
        )
        for idx in range(min(args.samples, 3)):
            sample_owner = ComputerOwner(
                session_id="bench-local",
                turn_id=f"bench-stop-{idx}",
                session_label="基准",
                cancel_event=threading.Event(),
            )
            stop_samples.append(measure_stop_and_lease(svc, sample_owner, cap, target="browser")["stop_response_ms"])
    output = {
        "environment": environment(args.browser),
        "browser": {
            "cold_start": stats(cold),
            "hot_click": hot_click,
            "hot_observe": hot_observe,
            "stop_and_lease": stats(stop_samples),
        },
        "desktop": {"start": desktop_start, "observe": desktop_observe, "error": desktop_error},
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

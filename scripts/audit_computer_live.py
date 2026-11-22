import argparse
import json
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.computer.browser_backend import BrowserComputerBackend
from core.computer.models import ComputerAction, ComputerControlError


HTML = """<!doctype html><meta charset="utf-8"><title>YCODE LOCAL AUDIT</title>
<style>body{margin:0;height:4000px;font:20px Arial}button,input,a,#drag{position:absolute;left:50px;width:300px;height:50px}button{top:50px}input{top:150px}a{top:250px}#drag{top:350px;background:skyblue}</style>
<button id="button">Click audit</button><input id="field"><a href="/second">Navigation audit</a><div id="drag">Drag audit</div>
<script>window.audit={clicks:0,ctrl:false,shift:false,double:0,drag:0};
button.addEventListener('click',e=>{audit.clicks++;audit.ctrl=e.ctrlKey;audit.shift=e.shiftKey;document.title='YCODE CLICKED '+audit.clicks;});
field.addEventListener('input',()=>{document.title='YCODE INPUT '+field.value.length;});
button.addEventListener('dblclick',()=>audit.double++);
drag.addEventListener('mousedown',()=>audit.drag++);</script>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == "/slow.png":
            time.sleep(1.5)
            body = b"not-an-image"
        elif self.path == "/second":
            body = (HTML + "").replace("YCODE LOCAL AUDIT", "YCODE SECOND").encode("utf-8")
        else:
            body = (HTML + ('<img src="/slow.png">' if self.path == "/slow" else "")).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def browser_probe(channel):
    results = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    with tempfile.TemporaryDirectory(prefix="ycode-audit-profile-") as directory:
        from core.computer.platforms import get_platform_adapter

        hint_provider = getattr(get_platform_adapter(), "browser_window_hints", None)
        backend = BrowserComputerBackend(
            channel, Path(directory) / "profile", window_hint_provider=hint_provider
        )

        def measured(name, operation, verify=lambda value: True):
            start = time.perf_counter()
            value = operation()
            item = {"case": name, "ms": round((time.perf_counter() - start) * 1000, 1), "ok": bool(verify(value))}
            if isinstance(value, (str, int, float, bool, dict)):
                item["actual"] = value
            results.append(item)
            print(json.dumps(item, ensure_ascii=False), flush=True)
            return value

        def read(expression):
            return backend._call("audit oracle", lambda: backend.active_page.evaluate(expression))

        def action(kind, **kwargs):
            return backend.execute("live-audit", ComputerAction(kind=kind, target="browser", **kwargs))

        try:
            measured("start", backend.start)
            measured("navigate_local", lambda: action("navigate", url=base))
            measured("observe", lambda: backend.observe("live-audit"), lambda value: (value.width, value.height) == (1440, 900))
            measured("plain_click", lambda: (action("click", x=150, y=75), read("audit.clicks"))[1], lambda value: value == 1)
            measured("ctrl_click", lambda: (action("click", x=150, y=75, keys=("CTRL",)), read("audit.ctrl"))[1], lambda value: value is True)
            measured("shift_click", lambda: (action("click", x=150, y=75, keys=("SHIFT",)), read("audit.shift"))[1], lambda value: value is True)
            measured("double_click", lambda: (action("double_click", x=150, y=75), read("audit.double"))[1], lambda value: value == 1)
            action("click", x=150, y=175)
            measured("unicode_text", lambda: (action("type_text", text="中文🙂 audit"), read("field.value"))[1], lambda value: value == "中文🙂 audit")
            measured("ctrl_a_replace", lambda: (action("keypress", keys=("CTRL", "A")), action("type_text", text="replaced"), read("field.value"))[2], lambda value: value == "replaced")
            measured("drag", lambda: (action("drag", path=((100, 375), (250, 375))), read("audit.drag"))[1], lambda value: value == 1)
            measured("scroll_dispatch", lambda: action("scroll", x=500, y=400, delta_y=500))
            backend._call("wait scroll oracle", lambda: backend.active_page.wait_for_function("scrollY > 0"))
            measured("scroll_result", lambda: read("scrollY"), lambda value: value > 0)
            measured("navigate_second", lambda: action("navigate", url=base + "/second"))
            measured("back", lambda: (action("back"), read("location.pathname"))[1], lambda value: value == "/")
            measured("forward", lambda: (action("forward"), read("location.pathname"))[1], lambda value: value == "/second")
            measured("reload", lambda: (action("reload"), read("audit.clicks"))[1], lambda value: value == 0)
            measured("navigate_slow_subresource", lambda: action("navigate", url=base + "/slow"))

            def focus_probe():
                # R5 契约：用户切回第一页后，观察必须跟随可辨认的可见前台页，
                # 且后续点击落在该页（坐标按观察锁定的页面生效）。
                first = backend.active_page
                second = backend._context.new_page()
                second.goto(base + "/second")
                second.evaluate("document.title = 'YCODE AUDIT SECOND'")
                first.bring_to_front()
                deadline = time.perf_counter() + 5
                while time.perf_counter() < deadline:
                    hints = backend._window_hint_provider(backend._browser_pid) or []
                    if any(title.startswith("YCODE LOCAL AUDIT") for _h, title, _fg in hints):
                        break
                    time.sleep(0.1)
                observation = backend._cmd_observe("live-audit")
                tracked_first = backend.active_page is first
                before = first.evaluate("audit.clicks")
                backend._cmd_execute(
                    "live-audit",
                    ComputerAction(kind="click", target="browser", x=150, y=75),
                )
                after = first.evaluate("audit.clicks")
                return {
                    "tracked_first": tracked_first,
                    "click_landed_on_first": after == before + 1,
                    "surface_id": observation.surface_id,
                    "surface_generation": observation.surface_generation,
                }

            measured(
                "manual_tab_focus_tracking",
                lambda: backend._call("focus fixture", focus_probe),
                lambda value: value["tracked_first"] and value["click_landed_on_first"],
            )
        finally:
            backend.close()
            server.shutdown()
            server.server_close()
    return results


def _platform_hint_provider():
    from core.computer.platforms import get_platform_adapter

    provider = getattr(get_platform_adapter(), "browser_window_hints", None)
    return provider if callable(provider) else None


def _profile_orphans(profile_resolved: str) -> int:
    import psutil

    count = 0
    needle = profile_resolved.lower().replace("/", "\\")
    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            cmdline = [str(arg) for arg in (proc.info.get("cmdline") or [])]
            if any(
                arg.startswith("--user-data-dir=") and needle in arg.lower()
                for arg in cmdline
            ):
                count += 1
        except Exception:
            continue
    return count


def browser_process_probe(channel):
    results = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    with tempfile.TemporaryDirectory(prefix="ycode-audit-process-") as directory:
        profile = Path(directory) / "profile"
        backend = BrowserComputerBackend(
            channel,
            profile,
            window_hint_provider=_platform_hint_provider(),
            use_process_worker=True,
            command_timeout=3.0,
        )

        def record(name, ok, **extra):
            item = {"case": name, "ok": bool(ok), **extra}
            results.append(item)
            print(json.dumps(item, ensure_ascii=False), flush=True)

        try:
            started = time.perf_counter()
            backend.start()
            record(
                "process_start",
                True,
                ms=round((time.perf_counter() - started) * 1000, 1),
                browser_pid=backend._browser_pid,
            )
            started = time.perf_counter()
            backend.execute("live-audit", ComputerAction(kind="navigate", target="browser", url=base))
            observation = backend.observe("live-audit")
            record(
                "process_navigate_observe",
                observation.page_title == "YCODE LOCAL AUDIT"
                and (observation.width, observation.height) == (1440, 900)
                and bool(observation.surface_id),
                ms=round((time.perf_counter() - started) * 1000, 1),
                title=observation.page_title,
                surface_id=observation.surface_id,
            )
            started = time.perf_counter()
            backend.execute(
                "live-audit", ComputerAction(kind="click", target="browser", x=150, y=75)
            )
            observation = backend.observe("live-audit")
            record(
                "process_click_title_oracle",
                observation.page_title == "YCODE CLICKED 1",
                ms=round((time.perf_counter() - started) * 1000, 1),
                title=observation.page_title,
            )
            started = time.perf_counter()
            backend.execute(
                "live-audit", ComputerAction(kind="click", target="browser", x=150, y=175)
            )
            backend.execute(
                "live-audit",
                ComputerAction(kind="type_text", target="browser", text="中文🙂 audit"),
            )
            observation = backend.observe("live-audit")
            record(
                "process_unicode_title_oracle",
                observation.page_title == "YCODE INPUT 10",
                ms=round((time.perf_counter() - started) * 1000, 1),
                title=observation.page_title,
            )
            backend.execute(
                "live-audit",
                ComputerAction(kind="navigate", target="browser", url=base + "/second"),
            )
            observation = backend.observe("live-audit")
            record(
                "process_navigation_title_oracle",
                observation.page_title == "YCODE SECOND",
                title=observation.page_title,
            )
            backend.release_inputs()
        finally:
            close_started = time.perf_counter()
            backend.close()
            close_ms = round((time.perf_counter() - close_started) * 1000, 1)
            time.sleep(0.3)
            orphans = _profile_orphans(str(profile.resolve()))
            record(
                "process_clean_close_no_orphans",
                orphans == 0,
                close_ms=close_ms,
                orphan_browser_processes=orphans,
            )
            server.shutdown()
            server.server_close()

    # 停止边界：动作在飞时 close（导航到不可路由地址挂住命令预算）。
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    with tempfile.TemporaryDirectory(prefix="ycode-audit-stop-") as directory:
        profile = Path(directory) / "profile"
        backend = BrowserComputerBackend(
            channel,
            profile,
            window_hint_provider=_platform_hint_provider(),
            use_process_worker=True,
            command_timeout=30.0,
        )
        try:
            backend.start()
            backend.execute("live-audit", ComputerAction(kind="navigate", target="browser", url=base))
            backend.observe("live-audit")
            in_flight = threading.Thread(
                target=lambda: backend.execute(
                    "live-audit",
                    ComputerAction(
                        kind="navigate", target="browser", url="http://10.255.255.1:9/"
                    ),
                ),
                daemon=True,
            )
            in_flight.start()
            time.sleep(0.3)
            close_started = time.perf_counter()
            backend.close()
            close_ms = round((time.perf_counter() - close_started) * 1000, 1)
            admission_started = time.perf_counter()
            rejected_fast = False
            try:
                backend.execute(
                    "live-audit", ComputerAction(kind="click", target="browser", x=1, y=1)
                )
            except ComputerControlError:
                rejected_fast = (time.perf_counter() - admission_started) < 0.2
            time.sleep(0.5)
            orphans = _profile_orphans(str(profile.resolve()))
            record(
                "stop_during_action_hard_stop",
                close_ms < 6000 and rejected_fast and orphans == 0,
                close_ms=close_ms,
                admission_reject_under_200ms=rejected_fast,
                orphan_browser_processes=orphans,
            )
            retry = BrowserComputerBackend(
                channel,
                profile,
                window_hint_provider=_platform_hint_provider(),
                use_process_worker=True,
            )
            try:
                retry.start()
                retry.close()
                record("profile_reusable_after_hard_stop", True)
            except ComputerControlError as error:
                record("profile_reusable_after_hard_stop", False, error=error.code)
        finally:
            backend.close()
            server.shutdown()
            server.server_close()
    return results


def browser_window_matrix_probe(channel):
    results = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    with tempfile.TemporaryDirectory(prefix="ycode-audit-matrix-") as directory:
        backend = BrowserComputerBackend(
            channel, Path(directory) / "profile", window_hint_provider=_platform_hint_provider()
        )

        def record(name, ok, **extra):
            item = {"case": name, "ok": bool(ok), **extra}
            results.append(item)
            print(json.dumps(item, ensure_ascii=False), flush=True)

        def read(page, expression):
            return backend._call("matrix oracle", lambda: page.evaluate(expression))

        def wait_foreground_prefix(prefix, timeout=5.0):
            provider = backend._window_hint_provider
            deadline = time.perf_counter() + timeout
            while time.perf_counter() < deadline:
                hints = provider(backend._browser_pid) or []
                if any(title.startswith(prefix) for _h, title, _fg in hints):
                    return True
                time.sleep(0.05)
            return False

        try:
            backend.start()
            backend.execute(
                "matrix-audit", ComputerAction(kind="navigate", target="browser", url=base)
            )
            main_page = backend.active_page
            read(main_page, "document.title='YCODE MATRIX MAIN'")
            record("matrix_setup", True)

            read(
                main_page,
                "window.open('about:blank','_blank','popup,width=480,height=360')",
            )
            deadline = time.perf_counter() + 5
            while len(backend._context.pages) < 2 and time.perf_counter() < deadline:
                time.sleep(0.05)
            popup = backend._context.pages[-1]
            read(popup, "document.title='YCODE MATRIX POPUP'")
            backend._call("fixture popup front", lambda: popup.bring_to_front())
            # popup 置前台后：观察必须落到 popup 页（窗口标题提示裁决）。
            assert wait_foreground_prefix("YCODE MATRIX POPUP"), "popup 未成为前台窗口"
            observation = backend.observe("matrix-audit")
            record(
                "multi_window_focus_follows_popup",
                observation.page_title == "YCODE MATRIX POPUP",
                title=observation.page_title,
            )
            backend._call(
                "fixture main front", lambda: main_page.bring_to_front()
            )
            wait_foreground_prefix("YCODE MATRIX MAIN")
            observation = backend.observe("matrix-audit")
            record(
                "multi_window_focus_returns_to_main",
                observation.page_title == "YCODE MATRIX MAIN",
                title=observation.page_title,
            )
            backend.execute(
                "matrix-audit",
                ComputerAction(kind="click", target="browser", x=150, y=75),
            )
            record(
                "multi_window_click_lands_on_main",
                read(main_page, "audit.clicks") == 1
                and read(popup, "window.audit ? audit.clicks : 0") == 0,
            )

            # Ctrl+click 后台标签页：前台仍是主窗口，观察不得漂移到新标签。
            try:
                backend.execute(
                    "matrix-audit",
                    ComputerAction(
                        kind="click", target="browser", x=150, y=275, keys=("CTRL",)
                    ),
                )
            except ComputerControlError as error:
                snapshot = backend._call(
                    "diagnostic",
                    lambda: [
                        (p.title()[:28], p.url[-12:], p.evaluate("document.hasFocus()"))
                        for p in backend._context.pages
                        if not p.is_closed()
                    ],
                )
                hints = backend._window_hint_provider(backend._browser_pid) or []
                import ctypes as _ct
                record(
                    "ctrl_click_diagnostic",
                    False,
                    error=error.code,
                    foreground=_ct.windll.user32.GetForegroundWindow(),
                    pages=snapshot,
                    hints=[(t[:40], f) for _h, t, f in hints],
                )
                raise
            deadline = time.perf_counter() + 5
            while len(backend._context.pages) < 3 and time.perf_counter() < deadline:
                time.sleep(0.05)
            backend._call("fixture popup close", popup.close)
            time.sleep(0.3)
            wait_foreground_prefix("YCODE MATRIX MAIN")
            try:
                observation = backend.observe("matrix-audit")
            except ComputerControlError as error:
                open_pages = [p for p in backend._context.pages if not p.is_closed()]
                hints = backend._window_hint_provider(backend._browser_pid) or []
                record(
                    "background_tab_zero_misoperation",
                    False,
                    error=error.code,
                    pages=[(p.title()[:28], p.url[-12:]) for p in open_pages],
                    hints=[(t[:36], f) for _h, t, f in hints],
                )
                raise
            landed_main = read(main_page, "audit.clicks")
            backend.execute(
                "matrix-audit",
                ComputerAction(kind="click", target="browser", x=150, y=75),
            )
            landed_after = read(main_page, "audit.clicks")
            second_state = None
            for page in backend._context.pages:
                if page.url.endswith("/second"):
                    second_state = read(page, "audit.clicks")
            record(
                "background_tab_zero_misoperation",
                observation.page_title == "YCODE CLICKED 1"
                and landed_after == landed_main + 1
                and second_state == 0,
                observed_title=observation.page_title,
                background_tab_clicks=second_state,
            )
        finally:
            backend.close()
            server.shutdown()
            server.server_close()
    return results


def desktop_probe():
    from core.computer.windows_backend import WindowsComputerBackend

    backend = WindowsComputerBackend.create_default()
    start = time.perf_counter()
    try:
        backend.start()
        started = time.perf_counter()
        observation = backend.observe("live-audit")
        result = {"case": "desktop_observe", "ok": True, "start_ms": round((started-start)*1000, 1), "observe_ms": round((time.perf_counter()-started)*1000, 1), "original": [observation.original_width, observation.original_height], "model": [observation.width, observation.height], "origin": [observation.origin_x, observation.origin_y]}
        print(json.dumps(result), flush=True)
        return [result]
    finally:
        backend.close()


def desktop_input_probe():
    """只对本探针创建并校验前台标题的 Tk 测试窗口注入真实输入。"""
    import tkinter as tk
    from core.computer.windows_backend import WindowsComputerBackend

    backend = WindowsComputerBackend.create_default()
    backend.start()
    root = tk.Tk()
    title = "YCODE DESKTOP AUDIT"
    root.title(title)
    root.geometry("700x600+100+100")
    root.attributes("-topmost", True)
    counts = {"click": 0, "double": 0, "right": 0, "drag": 0}
    button = tk.Button(root, text="Local mouse test", command=lambda: counts.update(click=counts["click"] + 1))
    button.pack(fill="x", padx=30, pady=20)
    button.bind("<Double-Button-1>", lambda event: counts.update(double=counts["double"] + 1))
    button.bind("<Button-3>", lambda event: counts.update(right=counts["right"] + 1))
    entry = tk.Entry(root, font=("Arial", 16))
    entry.pack(fill="x", padx=30, pady=20)
    text = tk.Text(root, height=15)
    text.pack(fill="both", expand=True, padx=30, pady=20)
    text.insert("1.0", "\n".join(f"local audit line {n}" for n in range(200)))
    text.bind("<B1-Motion>", lambda event: counts.update(drag=counts["drag"] + 1))
    results = []

    def pump():
        deadline = time.perf_counter() + 0.15
        while time.perf_counter() < deadline:
            root.update()
            time.sleep(0.005)

    def point(widget, dx=0):
        observation = backend.observe("desktop-input-audit")
        px = widget.winfo_rootx() + widget.winfo_width() // 2 + dx
        py = widget.winfo_rooty() + widget.winfo_height() // 2
        return round((px-observation.origin_x)/observation.scale_x), round((py-observation.origin_y)/observation.scale_y)

    def execute(kind, **kwargs):
        if backend._adapter.foreground_window_title() != title:
            raise RuntimeError("Test window lost foreground; refusing desktop input")
        backend.execute("desktop-input-audit", ComputerAction(kind=kind, target="desktop", **kwargs))
        pump()

    def record(name, operation, verify):
        start = time.perf_counter()
        operation()
        actual = verify()
        item = {"case": name, "ok": bool(actual), "ms_including_150ms_event_pump": round((time.perf_counter()-start)*1000, 1)}
        results.append(item)
        print(json.dumps(item), flush=True)

    try:
        root.update()
        root.lift()
        root.focus_force()
        import ctypes
        user32 = ctypes.windll.user32
        user32.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        user32.GetAncestor.restype = ctypes.c_void_p
        user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
        test_hwnd = user32.GetAncestor(root.winfo_id(), 2)
        kernel32 = ctypes.windll.kernel32
        user32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        user32.GetWindowThreadProcessId.restype = ctypes.c_uint
        user32.AttachThreadInput.argtypes = [ctypes.c_uint, ctypes.c_uint, ctypes.c_bool]
        user32.AttachThreadInput.restype = ctypes.c_bool

        def _acquire_foreground():
            foreground_hwnd = user32.GetForegroundWindow()
            our_thread = kernel32.GetCurrentThreadId()
            foreground_thread = user32.GetWindowThreadProcessId(foreground_hwnd, None)
            attached = False
            if foreground_thread and foreground_thread != our_thread:
                attached = bool(user32.AttachThreadInput(our_thread, foreground_thread, True))
            try:
                root.lift()
                return bool(user32.SetForegroundWindow(test_hwnd)) or attached
            finally:
                if attached:
                    user32.AttachThreadInput(our_thread, foreground_thread, False)

        acquired = False
        for _attempt in range(10):
            _acquire_foreground()
            pump()
            if backend._adapter.foreground_window_title() == title:
                acquired = True
                break
        if not acquired:
            raise RuntimeError("Test window lost foreground; refusing desktop input")
        x, y = point(button)
        record("desktop_move", lambda: execute("move", x=x, y=y), lambda: True)
        record("desktop_click", lambda: execute("click", x=x, y=y), lambda: counts["click"] == 1)
        record("desktop_right_click", lambda: execute("click", x=x, y=y, button="right"), lambda: counts["right"] == 1)
        import ctypes
        time.sleep(ctypes.windll.user32.GetDoubleClickTime()/1000 + 0.05)
        record("desktop_double_click", lambda: execute("double_click", x=x, y=y), lambda: counts["double"] >= 1)
        x, y = point(entry)
        execute("click", x=x, y=y)
        record("desktop_unicode_text", lambda: execute("type_text", text="中文 audit"), lambda: entry.get() == "中文 audit")
        record("desktop_backspace", lambda: execute("keypress", keys=("BACKSPACE",)), lambda: entry.get() == "中文 audi")
        x, y = point(text)
        record("desktop_drag", lambda: execute("drag", path=((x, y), (x+100, y+30))), lambda: counts["drag"] > 0)
        text.yview_moveto(0.5)
        pump()
        before = text.yview()[0]
        record("positive_scroll_matches_browser_down_direction", lambda: execute("scroll", x=x, y=y, delta_y=1), lambda: text.yview()[0] > before)
        record("desktop_wait", lambda: execute("wait", duration_ms=100), lambda: True)
    finally:
        backend.close()
        root.destroy()
    return results


def desktop_surfaces_probe():
    from core.computer.windows_backend import WindowsComputerBackend

    backend = WindowsComputerBackend.create_default()
    backend.start()
    results = []

    def record(name, value, ok=True, **extra):
        item = {"case": name, "ok": bool(ok), **extra}
        if isinstance(value, (str, int, float, bool, list, dict)):
            item["actual"] = value
        results.append(item)
        print(json.dumps(item, ensure_ascii=False), flush=True)

    def list_candidates():
        backend.execute("surface-audit", ComputerAction(kind="list_surfaces", target="desktop"))
        return backend.observe("surface-audit").surface_candidates

    def wechat_windows(candidates):
        return [
            c for c in candidates
            if c["kind"] == "window"
            and any(key in c["label"] for key in ("微信", "WeChat", "Weixin"))
        ]

    def wechat_tray(candidates):
        return [
            c for c in candidates
            if c["kind"] == "tray_icon"
            and any(key in c["label"] for key in ("微信", "WeChat", "Weixin"))
        ]

    def wait_wechat_window(timeout=5.0):
        deadline = time.perf_counter() + timeout
        while time.perf_counter() < deadline:
            time.sleep(0.5)
            found = wechat_windows(list_candidates())
            if found:
                return found
        return []

    try:
        candidates = list_candidates()
        record(
            "list_surfaces_returns_candidates",
            {"windows": sum(1 for c in candidates if c["kind"] == "window"),
             "tray_icons": sum(1 for c in candidates if c["kind"] == "tray_icon")},
            ok=len(candidates) > 0,
        )
        record(
            "wechat_candidate_found",
            [c["label"] for c in wechat_windows(candidates) + wechat_tray(candidates)],
            ok=bool(wechat_windows(candidates) or wechat_tray(candidates)),
        )

        locator = backend._surface_locator
        windows = wechat_windows(candidates)
        if not windows:
            fresh = locator.list_tray_icons()
            tray_restore = [
                c for c in fresh
                if "invoke" in c.supported_operations
                and any(key in c.label.strip() for key in ("微信", "WeChat", "Weixin"))
                and locator._host.element_source(locator._native_keys[c.ref]) == "Shell_TrayWnd"
            ]
            if tray_restore:
                locator.invoke_element(tray_restore[0].ref, tray_restore[0].surface_generation)
                windows = wait_wechat_window()
            record("restore_wechat_window_for_matrix", bool(windows))

        candidates = list_candidates()
        windows = wechat_windows(candidates)
        if windows:
            activated = None
            last_error = ""
            for _attempt in range(3):
                candidates = list_candidates()
                windows = wechat_windows(candidates)
                if not windows:
                    break
                target = windows[0]
                try:
                    backend.execute(
                        "surface-audit",
                        ComputerAction(kind="activate_surface", target="desktop",
                                       surface_ref=target["ref"],
                                       surface_generation=target["surface_generation"]),
                    )
                    activated = target
                    break
                except ComputerControlError as error:
                    last_error = error.code
                    time.sleep(0.4)
            record(
                "activate_wechat_window",
                bool(activated),
                ref=activated["ref"] if activated else "",
                attempts_last_error=last_error,
            )
            if activated:
                observation = backend.observe("surface-audit")
                record(
                    "wechat_window_foreground_title",
                    observation.window_title,
                    ok=any(key in observation.window_title for key in ("微信", "WeChat", "Weixin")),
                )

                locator = backend._surface_locator
                hwnd = locator._native_keys[activated["ref"]]
                locator._host.close_window(hwnd)
                deadline = time.perf_counter() + 5
                gone = False
                while time.perf_counter() < deadline and not gone:
                    time.sleep(0.5)
                    gone = not wechat_windows(list_candidates())
                record("wechat_window_hidden_to_tray", gone)
            else:
                record("wechat_window_hidden_to_tray", "skipped_activation_failed", ok=False)
        else:
            record("activate_wechat_window", "no_visible_window", ok=False)

        candidates = list_candidates()
        expander = [c for c in candidates if "显示隐藏的图标" in c["label"]]
        if not (expander and "invoke" in expander[0]["operations"]):
            record("hidden_overflow_wechat_window_opened", "no expander candidate", ok=False)
            return results
        backend.execute(
            "surface-audit",
            ComputerAction(kind="invoke_surface", target="desktop",
                           surface_ref=expander[0]["ref"],
                           surface_generation=expander[0]["surface_generation"]),
        )
        hidden_candidates = []
        deadline = time.perf_counter() + 4
        while time.perf_counter() < deadline and not hidden_candidates:
            time.sleep(0.25)
            for c in locator.list_tray_icons():
                source = locator._host.element_source(locator._native_keys[c.ref])
                if "Overflow" in source or "Xaml" in source:
                    hidden_candidates.append(c)
        if not hidden_candidates:
            fresh = locator.list_tray_icons()
            expander = [c for c in fresh if "显示隐藏的图标" in c.label]
            if expander and "invoke" in expander[0]["operations"]:
                backend.execute(
                    "surface-audit",
                    ComputerAction(kind="invoke_surface", target="desktop",
                                   surface_ref=expander[0]["ref"],
                                   surface_generation=expander[0]["surface_generation"]),
                )
                deadline = time.perf_counter() + 4
                while time.perf_counter() < deadline and not hidden_candidates:
                    time.sleep(0.25)
                    for c in locator.list_tray_icons():
                        source = locator._host.element_source(locator._native_keys[c.ref])
                        if "Overflow" in source or "Xaml" in source:
                            hidden_candidates.append(c)
        record(
            "hidden_overflow_enumerated",
            len(hidden_candidates),
            ok=len(hidden_candidates) > 0,
        )
        # 微信当前在隐藏区 → 用隐藏区候选；否则用 Shell_TrayWnd 上可 Invoke
        # 的微信托盘图标（本机图标被用户提升到可见区，如实记录）。
        def _is_main_wechat(label: str) -> bool:
            normalized = label.strip()
            return normalized in ("微信", "WeChat", "Weixin") or normalized.startswith("WeChat")

        wechat_hidden = [c for c in hidden_candidates if _is_main_wechat(c.label)]
        if wechat_hidden:
            restore_candidate = wechat_hidden[0]
            source_note = "hidden_overflow"
        else:
            fresh = locator.list_tray_icons()
            shell_candidates = [
                c for c in fresh
                if "invoke" in c.supported_operations
                and _is_main_wechat(c.label)
                and locator._host.element_source(locator._native_keys[c.ref]) == "Shell_TrayWnd"
            ]
            restore_candidate = shell_candidates[0] if shell_candidates else None
            source_note = "shell_tray_promoted"
        record(
            "wechat_tray_candidate_selected",
            {"source": source_note, "label": restore_candidate.label if restore_candidate else None},
            ok=restore_candidate is not None,
        )
        if restore_candidate is None:
            record("hidden_overflow_wechat_window_opened", False)
            return results
        coordinate_ok = None
        if source_note == "hidden_overflow":
            candidate_ref = restore_candidate.ref
            candidate_generation = restore_candidate.surface_generation
            backend.execute(
                "surface-audit",
                ComputerAction(kind="observe_region", target="desktop", surface_ref=candidate_ref, surface_generation=candidate_generation),
            )
            # 必须消费一次观察：建立区域坐标基线并刷新前台签名（飞出面板
            # 打开后前台已切换，旧基线上的输入会被正确拒绝）。
            region_observation = backend.observe("surface-audit")
            bounds = restore_candidate.bounds
            coordinate_ok = bool(bounds[2] > 0 and bounds[3] > 0)
            if coordinate_ok:
                backend.execute(
                    "surface-audit",
                    ComputerAction(kind="click", target="desktop",
                                   x=region_observation.width // 2,
                                   y=region_observation.height // 2),
                )
                coordinate_ok = bool(wait_wechat_window())
            record("hidden_overflow_coordinate_click", coordinate_ok, source=source_note)
        if coordinate_ok:
            reopened = True
            record(
                "hidden_overflow_wechat_window_opened",
                True,
                source=source_note,
                invoke_skipped="window already opened by coordinate click",
            )
        else:
            locator.invoke_element(restore_candidate.ref, restore_candidate.surface_generation)
            reopened = wait_wechat_window()
            record(
                "hidden_overflow_wechat_window_opened",
                bool(reopened),
                source=source_note,
            )
        if reopened:
            observation = backend.observe("surface-audit")
            record(
                "hidden_overflow_foreground_title",
                observation.window_title,
                ok=any(key in observation.window_title for key in ("微信", "WeChat", "Weixin")),
            )
        return results
    finally:
        backend.close()


def desktop_apps_probe():
    """通用软件链路：自建记事本允许输入，其余软件只激活和观察。"""
    import os
    import subprocess
    import tempfile
    from core.computer.windows_backend import WindowsComputerBackend

    backend = WindowsComputerBackend.create_default()
    backend.start()
    results = []
    notepad = None

    def record(app, **item):
        item["application"] = app
        results.append(item)
        print(json.dumps(item, ensure_ascii=False), flush=True)

    def candidates():
        backend.execute("apps-audit", ComputerAction(kind="list_surfaces", target="desktop"))
        return backend.observe("apps-audit").surface_candidates

    def windows_for(process_name, title_tokens=()):
        found = []
        for candidate in candidates():
            if candidate.get("kind") != "window":
                continue
            label = candidate.get("label", "")
            if process_name.lower() in label.lower() or any(token.lower() in label.lower() for token in title_tokens):
                found.append(candidate)
        return found

    def activate_and_observe(app, candidate, allow_input=False):
        started = time.perf_counter()
        backend.execute("apps-audit", ComputerAction(kind="activate_surface", target="desktop", surface_ref=candidate["ref"], surface_generation=candidate["surface_generation"]))
        observation = backend.observe("apps-audit")
        before = observation.screen_sha256
        backend.execute("apps-audit", ComputerAction(kind="observe_region", target="desktop", surface_ref=candidate["ref"], surface_generation=candidate["surface_generation"]))
        local = backend.observe("apps-audit")
        changed = False
        if allow_input:
            backend.execute("apps-audit", ComputerAction(kind="type_text", target="desktop", text="ycode test"))
            changed = backend.observe("apps-audit").screen_sha256 != before
        record(app, ok=True, foreground_title=local.window_title, local_size=[local.width, local.height], screen_changed=changed, duration_ms=round((time.perf_counter() - started) * 1000, 1))

    try:
        notepad_tmp = Path(tempfile.gettempdir()) / f"ycode-apps-audit-{os.getpid()}.txt"
        notepad_tmp.write_text("", encoding="utf-8")
        notepad = subprocess.Popen(
            ["notepad.exe", str(notepad_tmp)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        marker = notepad_tmp.name
        deadline = time.perf_counter() + 8
        match = []
        while time.perf_counter() < deadline and not match:
            match = windows_for("notepad.exe", (marker,))
            if not match:
                time.sleep(0.2)
        record("notepad", matched=len(match), ok=bool(match), marker=marker)
        if match:
            activate_and_observe("notepad", match[0], allow_input=True)
            hwnd = backend._surface_locator._native_keys[match[0]["ref"]]
            backend._surface_locator._host.close_window(hwnd)
            time.sleep(0.5)
            gone = not windows_for("notepad.exe", (marker,))
            if not gone:
                backend.execute("apps-audit", ComputerAction(kind="keypress", target="desktop", keys=("ALT", "N")))
                time.sleep(0.5)
                gone = not windows_for("notepad.exe", (marker,))
            record("notepad", closed=gone, ok=gone)
        try:
            notepad_tmp.unlink(missing_ok=True)
        except OSError:
            pass

        for app, process_name, tokens in (("explorer", "explorer.exe", ("文件资源管理器", "File Explorer")), ("calculator", "Calculator.exe", ("计算器", "Calculator")), ("electron", "Code.exe", ("Visual Studio Code", "VS Code"))):
            found = windows_for(process_name, tokens)
            if not found:
                record(app, ok=True, skipped=True, reason="not_running")
                continue
            try:
                activate_and_observe(app, found[0], allow_input=False)
            except Exception as exc:
                record(app, ok=False, error_code=getattr(exc, "code", ""), error=str(exc))
        return results
    finally:
        if notepad is not None and notepad.poll() is None:
            notepad.terminate()
        backend.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--browser", choices=["edge", "chrome"])
    parser.add_argument("--browser-process", choices=["edge", "chrome"])
    parser.add_argument("--browser-window-matrix", choices=["edge", "chrome"])
    parser.add_argument("--desktop-observe", action="store_true")
    parser.add_argument("--desktop-input", action="store_true", help="Real input only into this probe's own visible test window")
    parser.add_argument("--desktop-surfaces", action="store_true", help="Locate and open the WeChat window via semantic surfaces; no messages sent")
    parser.add_argument("--desktop-apps", action="store_true", help="Validate generic application activation and local observation")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not any(
        [args.browser, args.browser_process, args.browser_window_matrix,
         args.desktop_observe, args.desktop_input, args.desktop_surfaces, args.desktop_apps]
    ):
        parser.error(
            "choose --browser, --browser-process, --browser-window-matrix, "
            "--desktop-observe, --desktop-input, --desktop-surfaces, or --desktop-apps"
        )
    try:
        if args.desktop_input:
            results = desktop_input_probe()
        elif args.desktop_observe:
            results = desktop_probe()
        elif args.desktop_surfaces:
            results = desktop_surfaces_probe()
        elif args.desktop_apps:
            results = desktop_apps_probe()
        elif args.browser_process:
            results = browser_process_probe(args.browser_process)
        elif args.browser_window_matrix:
            results = browser_window_matrix_probe(args.browser_window_matrix)
        else:
            results = browser_probe(args.browser)
    except Exception as error:
        import traceback
        traceback.print_exc()
        results = [{"ok": False, "error_type": type(error).__name__, "error_code": getattr(error, "code", ""), "error": str(error)}]
        print(json.dumps(results, ensure_ascii=False), flush=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    sys.exit(0 if all(item["ok"] for item in results) else 1)

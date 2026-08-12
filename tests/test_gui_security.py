import threading

import gui_main
from gui_bridge import ApiBridge, _sanitize_for_ui


def test_vite_identity_requires_ycode_marker(monkeypatch):
    class Response:
        def __init__(self, body):
            self._body = body

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, size):
            return self._body

    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *args, **kwargs: Response(b'<script type="module" src="/@vite/client"></script>'),
    )
    assert gui_main._is_ycode_vite_dev_server() is False

    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *args, **kwargs: Response(b'<meta name="application-name" content="Y-CODE">'),
    )
    assert gui_main._is_ycode_vite_dev_server() is True


def test_file_preview_rejects_path_outside_current_workspace(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    allowed = workspace / "ok.txt"
    allowed.write_text("ok", encoding="utf-8")
    outside = tmp_path / "secret.txt"
    outside.write_text("secret", encoding="utf-8")
    bridge = ApiBridge.__new__(ApiBridge)
    bridge._current_session = "session"
    bridge._session_workdir = lambda session: str(workspace)

    assert bridge.read_file_preview(str(allowed))["ok"] is True
    denied = bridge.read_file_preview(str(outside))
    assert denied["ok"] is False
    assert "工作区" in denied["msg"]


def test_open_path_rejects_path_outside_current_workspace(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    allowed = workspace / "ok.txt"
    allowed.write_text("ok", encoding="utf-8")
    outside = tmp_path / "launch.cmd"
    outside.write_text("echo unsafe", encoding="utf-8")
    opened = []
    monkeypatch.setattr("os.startfile", lambda path: opened.append(path), raising=False)
    bridge = ApiBridge.__new__(ApiBridge)
    bridge._current_session = "session"
    bridge._session_workdir = lambda session: str(workspace)

    assert bridge.open_path(str(allowed))["ok"] is True
    denied = bridge.open_path(str(outside))

    assert denied["ok"] is False
    assert opened == [str(allowed.resolve())]


def test_open_path_rejects_executable_inside_current_workspace(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    executable = workspace / "launch.cmd"
    executable.write_text("echo unsafe", encoding="utf-8")
    opened = []
    monkeypatch.setattr("os.startfile", lambda path: opened.append(path), raising=False)
    bridge = ApiBridge.__new__(ApiBridge)
    bridge._current_session = "session"
    bridge._session_workdir = lambda session: str(workspace)

    denied = bridge.open_path(str(executable))

    assert denied["ok"] is False
    assert opened == []


def test_ui_sanitizer_redacts_common_secret_shapes():
    text = (
        "Bearer abc.def.ghi "
        "postgresql://alice:db-pass@localhost/app "
        "api_key=plain-secret sk-live-1234567890"
    )

    result = _sanitize_for_ui(text)

    for secret in ("abc.def.ghi", "db-pass", "plain-secret", "sk-live-1234567890"):
        assert secret not in result


def test_turn_workdir_gate_serializes_workers():
    bridge = ApiBridge.__new__(ApiBridge)
    bridge._turn_workdir_lock = threading.Lock()
    active = 0
    max_active = 0
    guard = threading.Lock()

    def worker(*args, **kwargs):
        nonlocal active, max_active
        with guard:
            active += 1
            max_active = max(max_active, active)
        threading.Event().wait(0.03)
        with guard:
            active -= 1

    bridge._run_turn_worker = worker
    threads = [
        threading.Thread(target=bridge._run_turn_worker_serialized, args=("x", str(i)))
        for i in range(2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=1)

    assert max_active == 1


def test_shutdown_blocks_new_background_summary_turns():
    bridge = ApiBridge.__new__(ApiBridge)
    bridge._shutting_down = True
    bridge._active_turns = {}
    bridge._cancel_events = {}
    bridge._turn_start_lock = threading.Lock()
    bridge._bg_summary_lock = threading.Lock()
    bridge._bg_summary_triggered = set()

    bridge._maybe_start_bg_summary_turn("session")

    assert bridge._active_turns == {}
    assert bridge._cancel_events == {}

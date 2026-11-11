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


def test_turn_worker_serialized_forwards_without_serialization():
    """2026-08-17 多会话并行改造：_run_turn_worker_serialized 不再用
    _turn_workdir_lock 串行化（直接转发 _run_turn_worker）；workdir 隔离
    改由线程本地上下文（core.workspace_context）承担，并发回合允许并行。"""
    bridge = ApiBridge.__new__(ApiBridge)
    bridge._turn_workdir_lock = threading.Lock()
    calls = []

    def worker(*args, **kwargs):
        calls.append((args, kwargs))

    bridge._run_turn_worker = worker
    bridge._run_turn_worker_serialized("text", "session", internal_kind="bg_summary")

    # 直接转发：参数原样到达 _run_turn_worker
    assert calls == [(("text", "session"), {"internal_kind": "bg_summary"})]
    # 串行锁全程未被持有：并发路径不会被 _turn_workdir_lock 阻塞
    assert bridge._turn_workdir_lock.acquire(blocking=False) is True
    bridge._turn_workdir_lock.release()


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


# ---------------------------------------------------------------------------
# open_path 工作区限制放宽：GLOBAL 放行 + 凭据文件永久拒绝（沙盒开关跟随同
# read_file_preview，由 core.gui_path_policy 统一裁决）
# ---------------------------------------------------------------------------
import core.gui_path_policy as gpp


def test_open_path_global_session_allows_text_file(tmp_path, monkeypatch):
    f = tmp_path / "note.txt"
    f.write_text("hi", encoding="utf-8")
    opened = []
    monkeypatch.setattr("os.startfile", lambda path: opened.append(path), raising=False)
    bridge = ApiBridge.__new__(ApiBridge)
    bridge._current_session = "session"
    bridge._session_workdir = lambda session: "GLOBAL"

    res = bridge.open_path(str(f))

    assert res["ok"] is True
    assert opened == [str(f.resolve())]


def test_open_path_credential_config_denied(tmp_path, monkeypatch):
    ydata = tmp_path / "ydata"
    ydata.mkdir()
    monkeypatch.setattr(gpp, "_ycode_dir_override", str(ydata))
    monkeypatch.setattr(gpp, "_allow_outside_workspace", lambda: True)
    f = ydata / "config.json"
    f.write_text("{}", encoding="utf-8")
    opened = []
    monkeypatch.setattr("os.startfile", lambda path: opened.append(path), raising=False)
    bridge = ApiBridge.__new__(ApiBridge)
    bridge._current_session = "session"
    bridge._session_workdir = lambda session: "GLOBAL"

# 凭据文件必须拒绝，且不能真的去启动它
    res = bridge.open_path(str(f))

    assert res["ok"] is False
    assert opened == []


# ---------------------------------------------------------------------------
# reveal_in_explorer：只开资源管理器定位，不启动文件本身（文档页签工具栏用）
# ---------------------------------------------------------------------------


def test_reveal_in_explorer_prefers_shell_api(tmp_path, monkeypatch):
    import gui_bridge as gb

    f = tmp_path / "报告.docx"
    f.write_text("hi", encoding="utf-8")
    popped = []
    monkeypatch.setattr(gb, "_reveal_via_shell_api", lambda path: True)
    monkeypatch.setattr("subprocess.Popen", lambda args, **kw: popped.append(args))
    bridge = ApiBridge.__new__(ApiBridge)
    bridge._current_session = "session"
    bridge._session_workdir = lambda session: "GLOBAL"

    res = bridge.reveal_in_explorer(str(f))

    assert res["ok"] is True
    # Shell API 成功时不再拼命令行（避免任何解析变形）
    assert popped == []


def test_reveal_in_explorer_falls_back_to_single_command_string(tmp_path, monkeypatch):
    import sys

    import gui_bridge as gb

    if sys.platform != "win32":
        return
    f = tmp_path / "报告 with space.docx"
    f.write_text("hi", encoding="utf-8")
    popped = []
    # Shell API 不可用（没 CoInitialize 的老环境等）时走命令行兜底
    monkeypatch.setattr(gb, "_reveal_via_shell_api", lambda path: False)
    monkeypatch.setattr("subprocess.Popen", lambda args, **kw: popped.append(args))
    bridge = ApiBridge.__new__(ApiBridge)
    bridge._current_session = "session"
    bridge._session_workdir = lambda session: "GLOBAL"

    res = bridge.reveal_in_explorer(str(f))

    assert res["ok"] is True
    # 必须是整串命令：用参数列表时 subprocess 会转义引号，explorer 解析不到
    # /select, 的路径，会退回打开「桌面」（2026-11-06 用户实测踩到）
    assert popped == [f'explorer /select,"{f.resolve()}"']


def test_reveal_in_explorer_opens_directory_itself(tmp_path, monkeypatch):
    opened = []
    monkeypatch.setattr("os.startfile", lambda path: opened.append(path), raising=False)
    bridge = ApiBridge.__new__(ApiBridge)
    bridge._current_session = "session"
    bridge._session_workdir = lambda session: "GLOBAL"

    res = bridge.reveal_in_explorer(str(tmp_path))

    assert res["ok"] is True
    assert opened == [str(tmp_path.resolve())]


def test_reveal_in_explorer_missing_path(tmp_path):
    bridge = ApiBridge.__new__(ApiBridge)
    bridge._current_session = "session"
    bridge._session_workdir = lambda session: "GLOBAL"

    res = bridge.reveal_in_explorer(str(tmp_path / "nope.docx"))

    assert res["ok"] is False
    assert "不存在" in res["msg"]

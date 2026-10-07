"""调试适配器探测测试（规格 6.2：Python 用 debugpy、Go 用 dlv、C/C++/Rust 用 lldb-dap）。

探测结果在进程内缓存（与 S2 的 `core/ide/server_provision.py` 同口径），因此每个
用例前后都 invalidate_cache：否则 monkeypatch 出来的探测结果会跨用例互相污染
（同一语言先被探成「缺失」、后被探成「可用」，缓存会让后一条读到前一条）。
"""

import sys

import pytest

from core.ide import debug_adapters as da


@pytest.fixture(autouse=True)
def _isolate_probe_cache():
    da.invalidate_cache()
    yield
    da.invalidate_cache()


def test_python_without_debugpy_returns_pip_hint(monkeypatch):
    monkeypatch.setattr(da, "_run_probe", lambda command, timeout=10.0: False)

    probe = da.probe_adapter("python")

    assert probe.ok is False and probe.adapter_id == "debugpy"
    assert "pip install debugpy" in probe.hint
    # 提示里的解释器必须是当前解释器：debugpy 装在别处时 -m debugpy.adapter 找不到
    assert sys.executable in probe.hint


def test_python_with_debugpy_returns_stdio_spec(monkeypatch):
    monkeypatch.setattr(da, "_run_probe", lambda command, timeout=10.0: True)

    probe = da.probe_adapter("python")

    assert probe.ok is True
    assert probe.spec.command == [sys.executable, "-m", "debugpy.adapter"]
    assert probe.spec.transport == da.STDIO


def test_go_uses_dlv_dap_over_tcp(monkeypatch):
    monkeypatch.setattr(da.shutil, "which", lambda name: r"C:\tools\dlv.exe" if name == "dlv" else None)

    probe = da.probe_adapter("go")

    assert probe.ok is True
    assert probe.spec.command == [r"C:\tools\dlv.exe", "dap", "--listen=127.0.0.1:0"]
    assert probe.spec.transport == da.TCP


def test_go_missing_dlv_hints_go_install(monkeypatch):
    monkeypatch.setattr(da.shutil, "which", lambda name: None)

    probe = da.probe_adapter("go")

    assert probe.ok is False and "go install" in probe.hint and "dlv" in probe.hint


def test_cpp_prefers_lldb_dap_stdio(monkeypatch):
    monkeypatch.setattr(da.shutil, "which", lambda name: r"C:\LLVM\bin\lldb-dap.exe" if name == "lldb-dap" else None)

    probe = da.probe_adapter("cpp")

    assert probe.ok is True
    assert probe.spec.command == [r"C:\LLVM\bin\lldb-dap.exe", "--stdio"]
    assert probe.spec.transport == da.STDIO


def test_cpp_falls_back_to_lldb_vscode(monkeypatch):
    mapping = {"lldb-vscode": r"C:\LLVM\bin\lldb-vscode.exe"}
    monkeypatch.setattr(da.shutil, "which", lambda name: mapping.get(name))

    probe = da.probe_adapter("c")

    assert probe.ok is True and probe.spec.command[0].endswith("lldb-vscode.exe")


def test_cpp_missing_lldb_hint_mentions_llvm(monkeypatch):
    monkeypatch.setattr(da.shutil, "which", lambda name: None)

    probe = da.probe_adapter("rust")

    assert probe.ok is False and "LLVM" in probe.hint


def test_unsupported_language_returns_hint():
    probe = da.probe_adapter("java")

    assert probe.ok is False and "java" in probe.hint and probe.spec is None


def test_availability_lists_all_supported_languages(monkeypatch):
    monkeypatch.setattr(da, "_run_probe", lambda command, timeout=10.0: False)
    monkeypatch.setattr(da.shutil, "which", lambda name: None)

    rows = da.adapter_availability()

    assert [row["language_id"] for row in rows] == ["python", "go", "c", "cpp", "rust"]
    assert all(row["available"] is False for row in rows)
    assert all(str(row["hint"]) for row in rows)


def test_run_probe_returns_false_when_executable_missing():
    """真探测路径：不存在的可执行文件必须返回 False 而不是抛异常。"""
    assert da._run_probe(["definitely-not-an-executable-ycode"], timeout=5.0) is False


# --- 缓存隔离 / 硬化（在计划用例之外补的加强项，见 Task 2 报告「自查修正」） ---


def test_spec_command_list_is_not_shared_with_cache(monkeypatch):
    """返回给调用方的 command 不得与缓存共用同一个 list（S2 的 _clone_result 同坑）。"""
    monkeypatch.setattr(da, "_run_probe", lambda command, timeout=10.0: True)

    first = da.probe_adapter("python")
    first.spec.command.append("--污染")

    second = da.probe_adapter("python")

    assert second.spec.command == [sys.executable, "-m", "debugpy.adapter"]
    assert da.adapter_availability()[0]["available"] is True


def test_probe_result_is_cached_between_calls(monkeypatch):
    """同语言重复探测不得重复起进程（面板刷新会反复问）。"""
    calls = {"n": 0}

    def fake_probe(command, timeout=10.0):
        calls["n"] += 1
        return True

    monkeypatch.setattr(da, "_run_probe", fake_probe)

    da.probe_adapter("python")
    da.probe_adapter("python")

    assert calls["n"] == 1

    da.invalidate_cache()
    da.probe_adapter("python")

    assert calls["n"] == 2


def test_candidate_that_is_a_directory_gives_diagnosable_hint(monkeypatch, tmp_path):
    """PATH 里命中的坏候选（目录）不能静默当作「未安装」：提示要写清原因。"""
    monkeypatch.setattr(da.shutil, "which", lambda name: str(tmp_path) if name == "dlv" else None)

    probe = da.probe_adapter("go")

    assert probe.ok is False and probe.spec is None
    assert "dlv" in probe.hint and "目录" in probe.hint


def test_windows_script_candidate_is_wrapped_for_popen(monkeypatch):
    """Windows 的 .cmd/.bat shim 不能直接交给 Popen([...])：必须 cmd.exe /c 包一层。"""
    monkeypatch.setattr(da.shutil, "which", lambda name: r"C:\tools\dlv.cmd" if name == "dlv" else None)

    probe = da.probe_adapter("go")

    assert probe.ok is True
    command = probe.spec.command
    assert command[1] == "/c" and command[2] == r"C:\tools\dlv.cmd"
    assert command[3:] == ["dap", "--listen=127.0.0.1:0"]
    assert not command[0].lower().endswith((".cmd", ".bat"))


def test_lldb_dap_missing_while_which_order_prefers_lldb_dap(monkeypatch):
    """两个候选名都在时才用 lldb-dap（新名优先于老名 lldb-vscode）。"""
    mapping = {"lldb-dap": r"C:\LLVM\bin\lldb-dap.exe", "lldb-vscode": r"C:\LLVM\bin\lldb-vscode.exe"}
    monkeypatch.setattr(da.shutil, "which", lambda name: mapping.get(name))

    probe = da.probe_adapter("rust")

    assert probe.spec.command == [r"C:\LLVM\bin\lldb-dap.exe", "--stdio"]

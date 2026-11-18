"""第三批高吞吐执行、代码理解升级与编排减负的回归测试。"""

from __future__ import annotations

import json
from types import SimpleNamespace

import core.app_context as app_context
from core import lsp, tool_cache
from core.harness import tool_pipeline
from core.llm_native_transport import _http2_enabled
from core.tools import SubagentManager, list_directory, search_code


def test_list_directory_markers_and_tree_depth(tmp_path, monkeypatch):
    """list_directory 支持目录 '/' 标注与 depth>1 树状展开并忽略噪音目录。"""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "sub.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("git\n", encoding="utf-8")
    (tmp_path / "app.py").write_text("print(1)\n", encoding="utf-8")

    flat = list_directory(str(tmp_path))
    assert "pkg/" in flat
    assert "app.py" in flat
    assert "sub.py" not in flat

    tree = list_directory(str(tmp_path), depth=2)
    assert "pkg/" in tree
    assert "  sub.py" in tree
    assert ".git" not in tree


def test_search_code_modes_glob_case_and_python_fallback(tmp_path, monkeypatch):
    """search_code 支持 output_mode(content/files/count)、glob 与区分大小写。"""
    monkeypatch.chdir(tmp_path)
    tool_cache.clear_all()
    (tmp_path / "a.py").write_text("TargetFunc()\ntargetfunc()\nTargetFunc()\n", encoding="utf-8")
    (tmp_path / "b.md").write_text("TargetFunc in docs\n", encoding="utf-8")

    # 强制走 Python 回退分支覆盖两者等价性
    monkeypatch.setattr("core.tools._get_rg_executable", lambda: None)

    files_out = search_code("TargetFunc", dir_path=str(tmp_path), glob="*.py", output_mode="files")
    assert "a.py" in files_out
    assert "b.md" not in files_out

    count_sensitive = search_code(
        "TargetFunc", dir_path=str(tmp_path), glob="*.py", output_mode="count", case_sensitive=True
    )
    assert "a.py:2" in count_sensitive

    count_insensitive = search_code(
        "TargetFunc", dir_path=str(tmp_path), glob="*.py", output_mode="count", case_sensitive=False
    )
    assert "a.py:3" in count_insensitive


def test_lsp_get_outline_and_find_references(tmp_path, monkeypatch):
    """get_outline 提取类/方法签名行号，find_references 按整词查找全仓引用。"""
    monkeypatch.chdir(tmp_path)
    tool_cache.clear_all()

    src = tmp_path / "service.py"
    src.write_text(
        "class Engine:\n"
        "    def start(self, port=8080):\n"
        "        return port\n"
        "\n"
        "async def run_helper(x):\n"
        "    return x\n",
        encoding="utf-8",
    )
    caller = tmp_path / "main.py"
    caller.write_text(
        "from service import Engine, run_helper\n"
        "e = Engine()\n"
        "run_helper_ignored = 1\n"
        "run_helper(10)\n",
        encoding="utf-8",
    )

    outline = lsp.get_outline(str(src))
    assert "class Engine" in outline
    assert "def start(self, port)" in outline
    assert "async def run_helper(x)" in outline

    # 关闭 rg 验证内置回退也可准确整词匹配
    monkeypatch.setattr(lsp, "_get_rg_executable", lambda: "nonexistent-rg-bin")
    refs = lsp.find_references("run_helper", dir_path=str(tmp_path))
    assert "service.py:5" in refs
    assert "main.py:1" in refs
    assert "main.py:4" in refs
    assert "run_helper_ignored" not in refs


def test_subagent_implement_and_review_inherit_primary_model(monkeypatch):
    """implement/review/general 子代理默认不降级 secondary；explore/test 可用 secondary。"""
    import core.tools as tools_mod

    recorded_calls = []

    def fake_snapshot(prefer_secondary=True, session_name=None):
        recorded_calls.append((prefer_secondary, session_name))
        return ("openai/main-model", "k", "u", "m1")

    monkeypatch.setattr(tools_mod, "_snapshot_model_config", fake_snapshot)
    monkeypatch.setattr(
        SubagentManager,
        "_run_default_subagent",
        lambda self, task: "ok " + "结论" * 30,
    )
    mgr = SubagentManager()

    mgr.run("探路", "s1", "t1", subagent_type="explore", timeout_seconds=30)
    mgr.run("写代码", "s1", "t2", subagent_type="implement", timeout_seconds=30)
    mgr.run("审查代码", "s1", "t3", subagent_type="review", timeout_seconds=30)
    mgr.run("跑测试", "s1", "t4", subagent_type="test", timeout_seconds=30)

    assert recorded_calls == [
        (True, "s1"),
        (False, "s1"),
        (False, "s1"),
        (True, "s1"),
    ]


def test_completed_test_subagent_satisfies_verification_claim_guard(mocker):
    """写文件后委派 subagent_type='test' 完成验证，收尾声称测试通过不应再误报警。"""
    from core.llm import chat_with_model_events

    def _chunk(content="", tool_calls=None):
        return SimpleNamespace(
            choices=[SimpleNamespace(delta=SimpleNamespace(content=content, reasoning_content="", tool_calls=tool_calls, provider_specific_fields=None))],
            usage=None,
        )

    tc_write = SimpleNamespace(
        index=0,
        id="c1",
        function=SimpleNamespace(name="write_file", arguments=json.dumps({"file_path": "a.py", "content": "x = 1"})),
    )
    tc_test = SimpleNamespace(
        index=1,
        id="c2",
        function=SimpleNamespace(name="run_subagent", arguments=json.dumps({"task_description": "跑单测", "subagent_type": "test"})),
    )

    def _fake_exec(tc, **_kw):
        name = tc["function"]["name"]
        content = (
            json.dumps({"status": "completed", "result": "10 passed"}, ensure_ascii=False)
            if name == "run_subagent"
            else "ok"
        )
        return {"role": "tool", "tool_call_id": tc["id"], "name": name, "content": content}

    completion = mocker.patch(
        "core.llm.completion",
        side_effect=[
            iter([_chunk(tool_calls=[tc_write, tc_test])]),
            iter([_chunk("已完成改造，全部测试通过。")]),
        ],
    )
    mocker.patch("core.llm._execute_single_tool_call", side_effect=_fake_exec)

    messages = [{"role": "user", "content": "修改并验证"}]
    list(
        chat_with_model_events(
            "fake-model",
            "fake-key",
            messages,
            persist=False,
            turn_id="turn-subagent-test-verify",
            consume_background_notifications=False,
        )
    )
    assert completion.call_count == 2
    assert not any("没有执行任何命令" in str(m.get("content") or "") for m in messages)


def test_finish_reason_length_runs_safe_readonly_calls_only(mocker):
    """finish_reason=='length' 时只安全执行完整只读调用，写调用与尾截断调用仍拦截。"""
    import core.llm as llm_mod

    def _chunk(content="", tool_calls=None, finish_reason=None):
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(
                        content=content,
                        reasoning_content="",
                        tool_calls=tool_calls,
                        provider_specific_fields=None,
                    ),
                    finish_reason=finish_reason,
                )
            ],
            usage=None,
        )

    executed = []

    def _fake_exec(tc, **_kw):
        executed.append(tc["function"]["name"])
        return {
            "role": "tool",
            "tool_call_id": tc["id"],
            "name": tc["function"]["name"],
            "content": "file body",
        }

    tc_read = SimpleNamespace(
        index=0,
        id="r1",
        function=SimpleNamespace(name="read_file", arguments=json.dumps({"file_path": "a.py"})),
    )
    tc_write = SimpleNamespace(
        index=1,
        id="w1",
        function=SimpleNamespace(name="write_file", arguments=json.dumps({"file_path": "a.py", "content": "x"})),
    )
    tc_tail = SimpleNamespace(
        index=2,
        id="r2",
        function=SimpleNamespace(name="read_file", arguments='{"file_path": "b.p'),
    )

    mocker.patch(
        "core.llm.completion",
        side_effect=[
            iter([_chunk(tool_calls=[tc_read, tc_write, tc_tail], finish_reason="length")]),
            iter([_chunk("读取结果已收到，任务完成。")]),
        ],
    )
    mocker.patch("core.llm._execute_single_tool_call", side_effect=_fake_exec)

    messages = [{"role": "user", "content": "查看再改"}]
    list(
        llm_mod.chat_with_model_events(
            "fake-model",
            "fake-key",
            messages,
            persist=False,
            turn_id="turn-length-partial",
            consume_background_notifications=False,
        )
    )
    assert executed == ["read_file"]


def test_pooled_daemon_call_restores_tls_and_thread_name():
    """线程池复用期间不泄漏线程本地 session，且保持 'ycode-tool-call' 线程名契约。"""
    import threading

    app_context.set_thread_session("sess-A")
    app_context.set_thread_permission_mode("full")

    def job_a():
        return threading.current_thread().name, app_context.get_thread_session()

    tname, tsess = tool_pipeline._submit_daemon_call(job_a).result(timeout=5)
    assert tname == "ycode-tool-call"
    assert tsess == "sess-A"

    app_context.set_thread_session(None)
    app_context.set_thread_permission_mode(None)

    def job_b():
        return app_context.get_thread_session()

    assert tool_pipeline._submit_daemon_call(job_b).result(timeout=5) is None


def test_http2_enabled_helper(monkeypatch):
    """_http2_enabled 在安装 h2 时开启，且尊重 YCODE_HTTP2=0 开关。"""
    monkeypatch.delenv("YCODE_HTTP2", raising=False)
    assert _http2_enabled() is True
    monkeypatch.setenv("YCODE_HTTP2", "0")
    assert _http2_enabled() is False

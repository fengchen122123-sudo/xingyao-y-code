"""GUI 首轮 AI 总结命名的调度语义测试（不起真实线程/不调真实模型）。"""

import threading
import types

import core.session_title
import gui_bridge


class _FakeMemory:
    def __init__(self, history, sources):
        self.history = history
        self.sources = list(sources)

    def get_session_title_source(self, name):
        if len(self.sources) > 1:
            return self.sources.pop(0)
        return self.sources[0]

    def get_history(self, name):
        return self.history


def _stub(memory):
    stub = types.SimpleNamespace()
    stub._memory = memory
    stub._goal_runs = {}
    stub._pending_title_sessions = set()
    stub._pending_title_lock = threading.Lock()
    stub._is_turn_active = lambda name: False
    stub._goal_runs_map = lambda: {}
    stub._derive_title_from_first_user_msg = lambda name: "本地兜底标题"
    stub._renames = []
    stub._rename_session_to_title = (
        lambda name, title, **kwargs: stub._renames.append((name, title, kwargs))
    )
    stub._claim_title_job = gui_bridge.ApiBridge._claim_title_job.__get__(stub)
    stub._release_title_job = gui_bridge.ApiBridge._release_title_job.__get__(stub)
    return stub


def _first_turn_history():
    return [
        {"role": "user", "content": "帮我写个冒泡排序"},
        {"role": "assistant", "content": "好的，这是实现"},
    ]


def test_run_auto_title_job_uses_generated_title(monkeypatch):
    memory = _FakeMemory(_first_turn_history(), [None, None])
    stub = _stub(memory)
    monkeypatch.setattr(
        gui_bridge,
        "get_active_model",
        lambda name: {"litellm_model": "m", "api_key": "k", "base_url": "u"},
    )
    monkeypatch.setattr(
        core.session_title, "generate_session_title", lambda *args, **kwargs: "冒泡排序"
    )

    gui_bridge.ApiBridge._run_auto_title_job(stub, "session_abc123")

    assert stub._renames == [
        ("session_abc123", "冒泡排序", {"require_untitled": True})
    ]


def test_run_auto_title_job_discards_after_manual_rename(monkeypatch):
    memory = _FakeMemory(_first_turn_history(), [None])
    stub = _stub(memory)
    monkeypatch.setattr(
        gui_bridge,
        "get_active_model",
        lambda name: {"litellm_model": "m", "api_key": "k", "base_url": "u"},
    )

    def _generate(*args, **kwargs):
        # 模拟模型生成期间用户手动改名：落库前的二次校验必须放弃
        memory.sources = ["manual"]
        return "冒泡排序"

    monkeypatch.setattr(core.session_title, "generate_session_title", _generate)

    gui_bridge.ApiBridge._run_auto_title_job(stub, "session_abc123")

    assert stub._renames == []


def test_run_auto_title_job_falls_back_to_local_title(monkeypatch):
    memory = _FakeMemory(_first_turn_history(), [None, None])
    stub = _stub(memory)
    monkeypatch.setattr(
        gui_bridge,
        "get_active_model",
        lambda name: {"litellm_model": "m", "api_key": "k", "base_url": "u"},
    )
    monkeypatch.setattr(
        core.session_title, "generate_session_title", lambda *args, **kwargs: None
    )

    gui_bridge.ApiBridge._run_auto_title_job(stub, "session_abc123")

    assert stub._renames == [
        ("session_abc123", "本地兜底标题", {"require_untitled": True})
    ]


def test_maybe_auto_rename_skips_already_titled_session(monkeypatch):
    memory = _FakeMemory(
        [
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": "b"},
        ],
        ["ai"],
    )
    stub = _stub(memory)
    started = []

    class FakeThread:
        def __init__(self, **kwargs):
            started.append(kwargs)

        def start(self):
            pass

    monkeypatch.setattr(gui_bridge.threading, "Thread", FakeThread)

    result = gui_bridge.ApiBridge._maybe_auto_rename(stub, "session_abc123")

    assert result == "session_abc123"
    assert started == []


def test_maybe_auto_rename_retries_on_later_turn_when_still_untitled(monkeypatch):
    # 首轮命名可能因用户紧接着发下一条而放弃；后续回合仍要补上（材料取首轮）
    memory = _FakeMemory(
        [
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": "b"},
            {"role": "user", "content": "c"},
        ],
        [None],
    )
    stub = _stub(memory)
    started = []

    class FakeThread:
        def __init__(self, **kwargs):
            started.append(kwargs)

        def start(self):
            pass

    monkeypatch.setattr(gui_bridge.threading, "Thread", FakeThread)

    gui_bridge.ApiBridge._maybe_auto_rename(stub, "session_abc123")

    assert len(started) == 1


def test_maybe_auto_rename_starts_background_job_once(monkeypatch):
    memory = _FakeMemory([{"role": "user", "content": "a"}], [None])
    stub = _stub(memory)
    started = []

    class FakeThread:
        def __init__(self, **kwargs):
            started.append(kwargs)

        def start(self):
            pass

    monkeypatch.setattr(gui_bridge.threading, "Thread", FakeThread)

    assert gui_bridge.ApiBridge._maybe_auto_rename(stub, "session_abc123") == "session_abc123"
    gui_bridge.ApiBridge._maybe_auto_rename(stub, "session_abc123")

    assert len(started) == 1
    assert started[0]["daemon"] is True

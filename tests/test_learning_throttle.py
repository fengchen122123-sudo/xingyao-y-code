import json

import pytest

import config
from core import identity_store as store
from core import learning


@pytest.fixture
def ycode_dir(tmp_path, monkeypatch):
    root = tmp_path / "ycode-data"
    monkeypatch.setattr(config, "YCODE_DIR", str(root))
    monkeypatch.setenv("YCODE_USER", "learner")
    return root


class _FakeMessage:
    def __init__(self, content):
        self.content = content


class _FakeChoice:
    def __init__(self, content):
        self.message = _FakeMessage(content)


class _FakeResponse:
    def __init__(self, content):
        self.choices = [_FakeChoice(content)]


class _FakeManager:

    def __init__(self, history):
        self.history = history

    def get_history_for_llm(self, session):  # noqa: ARG002 - 接口兼容
        return list(self.history)


def _history(user_messages=6, assistant_messages=None):
    if assistant_messages is None:
        assistant_messages = max(0, user_messages - 1)
    history = []
    for index in range(user_messages):
        history.append({"role": "user", "content": f"第{index}个问题：帮我看看这个 bug"})
        if index < assistant_messages:
            history.append({"role": "assistant", "content": f"第{index}个回答：已修好"})
    return history


GOOD_PAYLOAD = {
    "user_facts": [{"kind": "preference", "text": "喜欢简短回答"}],
    "lessons": [
        {
            "slug": "Debug-Crash-Loop",
            "scene": "测试反复崩溃",
            "how": "先看 traceback 再改代码",
        }
    ],
    "self_notes": ["回答要更直接"],
}


def _patch_completion(monkeypatch, payload):
    import core.llm

    calls = []

    def fake_completion(**kwargs):
        calls.append(kwargs)
        text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        return _FakeResponse(text)

    monkeypatch.setattr(core.llm, "completion", fake_completion)
    return calls


MODEL_CFG = ("gpt-test", "sk-test", "https://example.invalid/v1")


def test_second_turn_learning_is_throttled(ycode_dir, monkeypatch):
    _patch_completion(monkeypatch, GOOD_PAYLOAD)
    manager = _FakeManager(_history(user_messages=6))

    assert learning.run_turn_learning("sess-1", manager, MODEL_CFG) is True
    assert learning.run_turn_learning("sess-1", manager, MODEL_CFG) is False

    state = json.loads((ycode_dir / "self" / "learning_state.json").read_text("utf-8"))
    assert state["last_global_at"] > 0
    assert state["sessions"]["sess-1"]["msg_count"] == 6
    assert state["sessions"]["sess-1"]["last_at"] > 0

    assert learning.should_learn("sess-2", 6) is False


def test_not_enough_new_user_messages(ycode_dir, monkeypatch):
    calls = _patch_completion(monkeypatch, GOOD_PAYLOAD)
    manager = _FakeManager(_history(user_messages=3))

    assert learning.should_learn("sess-1", 3) is False
    assert learning.run_turn_learning("sess-1", manager, MODEL_CFG) is False
    assert calls == []


def test_bad_json_writes_nothing(ycode_dir, monkeypatch):
    _patch_completion(monkeypatch, "这不是 JSON，只是一段乱码 <<<>>>")
    manager = _FakeManager(_history(user_messages=6))

    assert (
        learning.extract_turn_lessons("sess-1", manager.history, MODEL_CFG) is None
    )
    assert learning.run_turn_learning("sess-1", manager, MODEL_CFG) is False

    profile = store.get_user_profile()
    assert profile["title"] == ""
    assert profile["preferences"] == []
    assert profile["forbidden"] == []
    assert store.iter_lessons() == []
    assert not (ycode_dir / "users").exists()
    assert not (ycode_dir / "lessons").exists()
    assert not (ycode_dir / "self" / "learning_state.json").exists()


def test_missing_api_key_returns_false(ycode_dir, monkeypatch):
    calls = _patch_completion(monkeypatch, GOOD_PAYLOAD)
    manager = _FakeManager(_history(user_messages=6))

    assert learning.extract_turn_lessons("sess-1", manager.history, ("m", "", None)) is None
    assert learning.run_turn_learning("sess-1", manager, ("m", "", None)) is False
    assert calls == []
    assert store.iter_lessons() == []


def test_apply_learning_same_slug_increments_hits(ycode_dir):
    result = {
        "user_facts": [],
        "lessons": [{"slug": "repeat-lesson", "scene": "场景", "how": "做法"}],
        "self_notes": [],
    }
    first = learning.apply_learning(result)
    second = learning.apply_learning(result)
    assert first["lessons"] == 1
    assert second["lessons"] == 1

    lessons = store.iter_lessons()
    assert len(lessons) == 1
    assert lessons[0]["slug"] == "repeat-lesson"
    assert lessons[0]["hits"] == 2


def test_self_evolution_without_key_skipped(ycode_dir, monkeypatch):
    calls = _patch_completion(monkeypatch, {"traits": [], "narrative": "x"})
    assert learning.run_self_evolution_sync("m", "") == "skipped"
    assert learning.run_self_evolution_sync("m", None) == "skipped"
    assert calls == []


def test_long_text_is_clipped_to_200(ycode_dir):
    long_text = "长" * 500
    result = {
        "user_facts": [
            {"kind": "title", "text": long_text},
            {"kind": "preference", "text": long_text},
            {"kind": "forbidden", "text": long_text},
        ],
        "lessons": [{"slug": "long-lesson", "scene": long_text, "how": long_text}],
        "self_notes": [long_text],
    }
    counts = learning.apply_learning(result, uid="alice")
    assert counts == {"user_facts": 3, "lessons": 1, "self_notes": 1}

    profile = store.get_user_profile("alice")
    assert len(profile["title"]) == 200
    assert all(len(item) <= 200 for item in profile["preferences"])
    assert all(len(item) <= 200 for item in profile["forbidden"])

    lesson = store.iter_lessons()[0]
    assert len(lesson["scene"]) <= 200
    assert len(lesson["how"]) <= 200

    growth_log = store.get_self_model()["growth_log"]
    assert growth_log
    assert all(len(entry["note"]) <= 200 for entry in growth_log)


def test_apply_learning_keeps_new_note_when_growth_log_full(ycode_dir):
    existing = [
        {"date": "2026-01-01", "note": f"旧笔记{i}"} for i in range(store.MAX_GROWTH_LOG)
    ]
    assert store.save_self_model({"growth_log": existing}) is True

    counts = learning.apply_learning(
        {"user_facts": [], "lessons": [], "self_notes": ["刚学到的新笔记"]}
    )
    assert counts["self_notes"] == 1
    log = store.get_self_model()["growth_log"]
    assert len(log) == store.MAX_GROWTH_LOG
    assert log[-1]["note"] == "刚学到的新笔记"


def test_learning_recovers_after_history_compression(ycode_dir, monkeypatch):
    _patch_completion(monkeypatch, GOOD_PAYLOAD)
    manager = _FakeManager(_history(user_messages=8))
    assert learning.run_turn_learning("sess-1", manager, MODEL_CFG) is True

    state_path = ycode_dir / "self" / "learning_state.json"
    state = json.loads(state_path.read_text("utf-8"))
    assert state["sessions"]["sess-1"]["msg_count"] == 8
    state["last_global_at"] = 0
    state["sessions"]["sess-1"]["last_at"] = 0
    state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")

    compressed = _FakeManager(_history(user_messages=6))
    assert learning.run_turn_learning("sess-1", compressed, MODEL_CFG) is True


def test_learning_disabled_skips_llm_call(ycode_dir, monkeypatch):
    calls = _patch_completion(monkeypatch, GOOD_PAYLOAD)
    store.save_growth_settings(learning_enabled=False)
    manager = _FakeManager(_history(user_messages=8))

    assert learning.run_turn_learning("sess-1", manager, MODEL_CFG) is False
    assert calls == []
    assert store.iter_lessons() == []
    assert not (ycode_dir / "users").exists()
    assert not (ycode_dir / "self" / "learning_state.json").exists()


def test_self_evolution_disabled_skipped(ycode_dir, monkeypatch):
    calls = _patch_completion(monkeypatch, {"traits": ["新特质"], "narrative": "新叙事"})
    store.save_growth_settings(enabled=False)

    assert learning.run_self_evolution_sync("m", "sk-test") == "skipped"
    assert calls == []
    assert store.get_self_model()["traits"] == []


def test_clock_rollback_does_not_disable_learning(ycode_dir):
    state_path = ycode_dir / "self" / "learning_state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps({"last_global_at": 9999999999.0, "sessions": {}}), "utf-8"
    )
    assert learning.should_learn("sess-1", 6) is True


def test_concurrent_turn_learning_calls_llm_once(ycode_dir, monkeypatch):
    import threading
    import time

    import core.llm

    calls = []

    def slow_completion(**kwargs):
        calls.append(kwargs)
        time.sleep(0.2)
        return _FakeResponse(json.dumps(GOOD_PAYLOAD, ensure_ascii=False))

    monkeypatch.setattr(core.llm, "completion", slow_completion)

    barrier = threading.Barrier(2)
    results = []

    def worker(session):
        barrier.wait(timeout=10)
        results.append(
            learning.run_turn_learning(session, _FakeManager(_history(6)), MODEL_CFG)
        )

    threads = [threading.Thread(target=worker, args=(f"s{i}",)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert sorted(results) == [False, True]
    assert len(calls) == 1


def test_clip_block_keeps_paragraphs(ycode_dir):
    text = learning._clip_block("第一段\n\n第二段\n\n\n第三段")
    assert text == "第一段\n\n第二段\n\n第三段"


def test_evolution_narrative_file_keeps_paragraphs(ycode_dir, monkeypatch):
    _patch_completion(monkeypatch, {"traits": [], "narrative": "第一段\n\n第二段"})
    learning.run_self_evolution_sync("m", "sk-test")

    content = (ycode_dir / "self" / "narrative.md").read_text(encoding="utf-8")
    assert "第一段" in content and "第二段" in content
    assert "\n\n" in content

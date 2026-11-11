"""core/learning.py 的节流、抽取、落盘与自我演化测试。

全部用例用 monkeypatch 把 ``config.YCODE_DIR`` 指到 tmp_path，绝口不碰真实
``~/.ycode``；大模型调用统一替换 ``core.llm.completion``，不联网。
"""

import json

import pytest

import config
from core import identity_store as store
from core import learning


# ------------------------------------------------------------------ fixtures
@pytest.fixture
def ycode_dir(tmp_path, monkeypatch):
    """把数据根目录切到临时目录，并固定用户键保证断言可复现。"""
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
    """最小会话管理器：只提供 get_history_for_llm。"""

    def __init__(self, history):
        self.history = history

    def get_history_for_llm(self, session):  # noqa: ARG002 - 接口兼容
        return list(self.history)


def _history(user_messages=6, assistant_messages=None):
    """构造 user/assistant 交替的历史。"""
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
    """把 core.llm.completion 换成返回固定内容的桩，并统计调用次数。"""
    import core.llm

    calls = []

    def fake_completion(**kwargs):
        calls.append(kwargs)
        text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        return _FakeResponse(text)

    monkeypatch.setattr(core.llm, "completion", fake_completion)
    return calls


MODEL_CFG = ("gpt-test", "sk-test", "https://example.invalid/v1")


# ---------------------------------------------------------------- 1) 节流
def test_second_turn_learning_is_throttled(ycode_dir, monkeypatch):
    _patch_completion(monkeypatch, GOOD_PAYLOAD)
    manager = _FakeManager(_history(user_messages=6))

    assert learning.run_turn_learning("sess-1", manager, MODEL_CFG) is True
    # 第一次学习已写入节流状态：全局间隔未过，第二次必须被拒
    assert learning.run_turn_learning("sess-1", manager, MODEL_CFG) is False

    state = json.loads((ycode_dir / "self" / "learning_state.json").read_text("utf-8"))
    assert state["last_global_at"] > 0
    assert state["sessions"]["sess-1"]["msg_count"] == 6
    assert state["sessions"]["sess-1"]["last_at"] > 0

    # 换会话也拦得住（全局间隔是 600 秒）
    assert learning.should_learn("sess-2", 6) is False


# ---------------------------------------------------- 2) 新增用户消息不足
def test_not_enough_new_user_messages(ycode_dir, monkeypatch):
    calls = _patch_completion(monkeypatch, GOOD_PAYLOAD)
    manager = _FakeManager(_history(user_messages=3))

    assert learning.should_learn("sess-1", 3) is False
    assert learning.run_turn_learning("sess-1", manager, MODEL_CFG) is False
    assert calls == []  # 节流不通过时根本不发请求


# ---------------------------------------------------- 3) 坏 JSON 不落盘
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
    # 抽取失败不更新节流状态（下次还能重试）
    assert not (ycode_dir / "self" / "learning_state.json").exists()


# ---------------------------------------------------- 4) 无 api_key 不学习
def test_missing_api_key_returns_false(ycode_dir, monkeypatch):
    calls = _patch_completion(monkeypatch, GOOD_PAYLOAD)
    manager = _FakeManager(_history(user_messages=6))

    assert learning.extract_turn_lessons("sess-1", manager.history, ("m", "", None)) is None
    assert learning.run_turn_learning("sess-1", manager, ("m", "", None)) is False
    assert calls == []
    assert store.iter_lessons() == []


# ----------------------------------------------- 5) 同 slug 命中计数累加
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


# --------------------------------------------- 6) 自我演化无凭据即 skipped
def test_self_evolution_without_key_skipped(ycode_dir, monkeypatch):
    calls = _patch_completion(monkeypatch, {"traits": [], "narrative": "x"})
    assert learning.run_self_evolution_sync("m", "") == "skipped"
    assert learning.run_self_evolution_sync("m", None) == "skipped"
    assert calls == []


# ---------------------------------------------------- 7) 超长文本被裁剪
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
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


# ---------------------------------------- 8) 容量满后新增笔记仍然落盘
def test_apply_learning_keeps_new_note_when_growth_log_full(ycode_dir):
    """growth_log 满 100 条后，新学到的笔记必须落盘且如实计数。

    否则自我成长会静默停止，而调用方依旧收到「写入 N 条」的成功回执。
    """
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


# ------------------------------------ 9) 上下文压缩后学习不能被永久卡死
def test_learning_recovers_after_history_compression(ycode_dir, monkeypatch):
    """压缩后可见用户消息数骤降，学习基线必须重置而不是永久判定「没有新消息」。"""
    _patch_completion(monkeypatch, GOOD_PAYLOAD)
    manager = _FakeManager(_history(user_messages=8))
    assert learning.run_turn_learning("sess-1", manager, MODEL_CFG) is True

    # 模拟上下文压缩：可见历史从 8 条 user 降到 6 条；同时把节流时间拨回去
    state_path = ycode_dir / "self" / "learning_state.json"
    state = json.loads(state_path.read_text("utf-8"))
    assert state["sessions"]["sess-1"]["msg_count"] == 8
    state["last_global_at"] = 0
    state["sessions"]["sess-1"]["last_at"] = 0
    state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")

    compressed = _FakeManager(_history(user_messages=6))
    assert learning.run_turn_learning("sess-1", compressed, MODEL_CFG) is True


# ----------------------------------- 10) 关闭经验学习后不发模型请求
def test_learning_disabled_skips_llm_call(ycode_dir, monkeypatch):
    """设置里关掉「经验学习」后，闲时学习不得再调用模型、不得落盘。"""
    calls = _patch_completion(monkeypatch, GOOD_PAYLOAD)
    store.save_growth_settings(learning_enabled=False)
    manager = _FakeManager(_history(user_messages=8))

    assert learning.run_turn_learning("sess-1", manager, MODEL_CFG) is False
    assert calls == []
    assert store.iter_lessons() == []
    assert not (ycode_dir / "users").exists()
    assert not (ycode_dir / "self" / "learning_state.json").exists()


def test_self_evolution_disabled_skipped(ycode_dir, monkeypatch):
    """设置里关掉「自我演化」后，深度演化不得再调用模型、不得改写自我模型。"""
    calls = _patch_completion(monkeypatch, {"traits": ["新特质"], "narrative": "新叙事"})
    store.save_growth_settings(enabled=False)

    assert learning.run_self_evolution_sync("m", "sk-test") == "skipped"
    assert calls == []
    assert store.get_self_model()["traits"] == []


# --------------------------------------- 11) 时钟回拨不得锁死学习
def test_clock_rollback_does_not_disable_learning(ycode_dir):
    """系统时钟回拨（now < last）时，「负间隔」不能被当成「还没到时间」。

    否则 NTP 校正 / 用户改时间后，学习会被静默禁用到时钟追回为止。
    """
    state_path = ycode_dir / "self" / "learning_state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps({"last_global_at": 9999999999.0, "sessions": {}}), "utf-8"
    )
    assert learning.should_learn("sess-1", 6) is True


# ------------------------------------- 12) 节流检查与占位必须原子
def test_concurrent_turn_learning_calls_llm_once(ycode_dir, monkeypatch):
    """两个会话同时空闲时，节流只能放行一次模型调用。

    旧实现「先检查、后记账」，两个线程都能通过检查，于是并发发起两次
    60 秒的模型请求并并发写档案（既费钱也会丢更新）。
    """
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


# ------------------------------------ 13) 自传叙事必须保留段落结构
def test_clip_block_keeps_paragraphs(ycode_dir):
    """多行文本裁剪不能把段落压成一行（自传叙事靠分段读）。"""
    text = learning._clip_block("第一段\n\n第二段\n\n\n第三段")
    assert text == "第一段\n\n第二段\n\n第三段"


def test_evolution_narrative_file_keeps_paragraphs(ycode_dir, monkeypatch):
    """自我演化写出的 narrative.md 要保留段落分隔。"""
    _patch_completion(monkeypatch, {"traits": [], "narrative": "第一段\n\n第二段"})
    learning.run_self_evolution_sync("m", "sk-test")

    content = (ycode_dir / "self" / "narrative.md").read_text(encoding="utf-8")
    assert "第一段" in content and "第二段" in content
    assert "\n\n" in content

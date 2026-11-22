import os
import sys
import threading
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core.app_context as app_context  # noqa: F401
from core.memory import MemoryManager


DEEPSEEK_BASE = "https://api.deepseek.com"
OTHER_BASE = "https://api.example.com/v1"


@pytest.fixture
def stub_llm(monkeypatch):
    stub = types.ModuleType("core.llm")
    stub.estimate_tokens = lambda model, messages: sum(
        len(str(m.get("content", ""))) for m in messages
    )
    stub.session_token_usage = {
        "cumulative_total": 0, "cumulative_prompt": 0, "cumulative_cached": 0,
        "latest_total": 0, "latest_prompt": 0, "latest_cached": 0,
        "max_window": 128000,
    }
    stub.token_usage_lock = threading.Lock()
    stub.resolve_turn_computer_capability = lambda *args, **kwargs: None
    stub.is_deepseek_ledger_route = (
        lambda model_name, api_base: str(api_base or "").startswith(DEEPSEEK_BASE)
    )
    stub._rotations = {}
    stub.mark_deepseek_generation_rotation = (
        lambda session_name: stub._rotations.__setitem__(str(session_name), True)
    )
    monkeypatch.setitem(sys.modules, "core.llm", stub)
    return stub


@pytest.fixture
def manager(tmp_path):
    return MemoryManager(str(tmp_path / "speedup.db"))


@pytest.fixture(autouse=True)
def _reset_precompress_state():
    import core.agent_service as agent_service

    agent_service._PRECOMPRESS_LAST_START.clear()
    yield
    agent_service._PRECOMPRESS_LAST_START.clear()


def test_inter_chunk_delay_default_is_zero():
    import core.memory_compressor as memory_compressor

    assert memory_compressor.COMPRESSION_INTER_CHUNK_DELAY_SECONDS == 0.0


def test_precompress_ratio_default_and_config_override(monkeypatch, tmp_path):
    import core.memory_compressor as memory_compressor

    assert memory_compressor.get_precompress_ratio() == pytest.approx(0.85)

    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(
        '{"memory_compression": {"precompress_ratio": 0.7}}', encoding="utf-8"
    )
    monkeypatch.setattr(
        "core.model_manager.load_config",
        lambda: {"memory_compression": {"precompress_ratio": 0.7}},
    )
    assert memory_compressor.get_precompress_ratio() == pytest.approx(0.7)


def test_inter_chunk_delay_explicit_config_respected(monkeypatch):
    monkeypatch.setattr(
        "core.model_manager.load_config",
        lambda: {"memory_compression": {"inter_chunk_delay_seconds": 1.5}},
    )
    import core.memory_compressor as memory_compressor

    assert memory_compressor._load_compression_config()[
        "inter_chunk_delay_seconds"
    ] == pytest.approx(1.5)


def test_baseline_prefers_real_usage_on_ledger_route(stub_llm, manager, monkeypatch):
    import core.agent_service as agent_service

    session = "ds_ledger_session"
    manager.add_message(session, "user", "你好")
    manager.add_token_usage(
        "deepseek", "deepseek-v4-pro", 89500, 500, 0, 90000, session_id=session
    )
    monkeypatch.setattr(
        agent_service, "estimate_history_tokens_with_system", lambda *_a, **_k: 500
    )

    baseline = agent_service.get_session_token_baseline(
        manager, session, "deepseek/deepseek-v4-pro",
        history=[{"role": "user", "content": "你好"}], api_base=DEEPSEEK_BASE,
    )
    assert baseline == 90000


def test_baseline_keeps_min_on_non_ledger_route(stub_llm, manager, monkeypatch):
    import core.agent_service as agent_service

    session = "non_ledger_session"
    manager.add_message(session, "user", "你好")
    manager.add_token_usage(
        "openai", "gpt-test", 89500, 500, 0, 90000, session_id=session
    )
    monkeypatch.setattr(
        agent_service, "estimate_history_tokens_with_system", lambda *_a, **_k: 500
    )

    baseline = agent_service.get_session_token_baseline(
        manager, session, "openai/gpt-test",
        history=[{"role": "user", "content": "你好"}], api_base=OTHER_BASE,
    )
    assert baseline == 500


def test_baseline_still_discards_real_usage_from_other_model(
    stub_llm, manager, monkeypatch
):
    import core.agent_service as agent_service

    session = "ds_model_switch"
    manager.add_message(session, "user", "你好")
    manager.add_token_usage(
        "deepseek", "deepseek-old", 89500, 500, 0, 90000, session_id=session
    )
    monkeypatch.setattr(
        agent_service, "estimate_history_tokens_with_system", lambda *_a, **_k: 500
    )

    baseline = agent_service.get_session_token_baseline(
        manager, session, "deepseek/deepseek-v4-pro",
        history=[{"role": "user", "content": "你好"}], api_base=DEEPSEEK_BASE,
    )
    assert baseline == 500


def _make_precompress_stub(monkeypatch, record, release=None):
    import core.memory_compressor as memory_compressor

    def _fake_compress(manager, session, model, key, base, **kwargs):
        record.append((session, kwargs.get("cancel_event")))
        if release is not None:
            release.wait(timeout=5)
        return memory_compressor.CompressionOutcome("compressed", "ok")

    monkeypatch.setattr(memory_compressor, "compress_session_memory", _fake_compress)


def test_precompress_triggers_in_band(stub_llm, manager, monkeypatch):
    import core.agent_service as agent_service

    called = threading.Event()
    record = []

    def _fake_compress(*args, **kwargs):
        record.append(args)
        called.set()
        import core.memory_compressor as mc
        return mc.CompressionOutcome("compressed", "ok")

    monkeypatch.setattr(
        "core.memory_compressor.compress_session_memory", _fake_compress
    )
    monkeypatch.setattr(agent_service, "_get_precompress_ratio", lambda: 0.85)

    started = agent_service._trigger_background_precompress(
        manager, "s_pre", "deepseek/deepseek-v4-pro", "key", DEEPSEEK_BASE,
        cancel_event=None, notify=None,
    )
    assert started is True
    assert called.wait(timeout=5), "预压缩线程未在超时内执行"
    assert record and record[0][1] == "s_pre"


def test_precompress_skipped_below_ratio(stub_llm, manager, monkeypatch):
    import core.agent_service as agent_service

    record = []
    _make_precompress_stub(monkeypatch, record)
    monkeypatch.setattr(agent_service, "_get_precompress_ratio", lambda: 0.85)

    assert agent_service._PRECOMPRESS_COOLDOWN_SECONDS > 0
    assert record == []


def test_precompress_reentry_guard(stub_llm, manager, monkeypatch):
    import core.agent_service as agent_service
    import core.memory_compressor as memory_compressor

    record = []
    _make_precompress_stub(monkeypatch, record)
    monkeypatch.setattr(agent_service, "_get_precompress_ratio", lambda: 0.85)

    key = (id(manager), "s_reentry")
    with memory_compressor._compress_guard:
        memory_compressor._compressing_sessions.add(key)
    try:
        started = agent_service._trigger_background_precompress(
            manager, "s_reentry", "deepseek/deepseek-v4-pro", "key", DEEPSEEK_BASE,
            cancel_event=None, notify=None,
        )
        assert started is False
        assert record == []
    finally:
        with memory_compressor._compress_guard:
            memory_compressor._compressing_sessions.discard(key)


def test_precompress_cooldown(stub_llm, manager, monkeypatch):
    import core.agent_service as agent_service

    record = []
    _make_precompress_stub(monkeypatch, record)
    monkeypatch.setattr(agent_service, "_get_precompress_ratio", lambda: 0.85)

    first = agent_service._trigger_background_precompress(
        manager, "s_cool", "deepseek/deepseek-v4-pro", "key", DEEPSEEK_BASE,
        cancel_event=None, notify=None,
    )
    second = agent_service._trigger_background_precompress(
        manager, "s_cool", "deepseek/deepseek-v4-pro", "key", DEEPSEEK_BASE,
        cancel_event=None, notify=None,
    )
    assert first is True
    assert second is False, "冷却期内不应再次启动预压缩线程"


def _fake_ledger_manager():

    class _FakeLedgerManager:
        def __init__(self):
            self.force_new_calls = []
            self._gen_id = 0

        def get_or_create_deepseek_prompt_generation(
            self, session_name, model_name, api_base, cache_scope,
            messages, tools, force_new=False,
        ):
            self.force_new_calls.append(bool(force_new))
            self._gen_id += 1
            return {
                "created": True,
                "id": self._gen_id,
                "messages": messages,
                "tools": tools,
            }

    return _FakeLedgerManager()


def test_rotation_mark_and_consume_roundtrip():
    llm = pytest.importorskip("core.llm")

    session = "rotation_roundtrip_session"
    try:
        assert llm.consume_deepseek_generation_rotation(session) is False
        llm.mark_deepseek_generation_rotation(session)
        assert llm.consume_deepseek_generation_rotation(session) is True
        assert llm.consume_deepseek_generation_rotation(session) is False
    finally:
        with llm._pending_ledger_rotations_lock:
            llm._pending_ledger_rotations.pop(session, None)


def test_rotation_consumed_before_ledger_request():
    llm = pytest.importorskip("core.llm")
    session = "rotation_ledger_request_session"
    fake = _fake_ledger_manager()
    try:
        llm.mark_deepseek_generation_rotation(session)
        llm._prepare_deepseek_ledger_request(
            fake, session, "deepseek/deepseek-v4-pro", DEEPSEEK_BASE,
            "main", [{"role": "user", "content": "hi"}], [],
        )
        assert fake.force_new_calls == [True]
        llm._prepare_deepseek_ledger_request(
            fake, session, "deepseek/deepseek-v4-pro", DEEPSEEK_BASE,
            "main", [{"role": "user", "content": "hi"}], [],
        )
        assert fake.force_new_calls == [True, False]
    finally:
        with llm._pending_ledger_rotations_lock:
            llm._pending_ledger_rotations.pop(session, None)

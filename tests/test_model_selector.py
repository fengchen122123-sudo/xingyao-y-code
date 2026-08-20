import threading

import core.llm as llm
import core.model_selector as model_selector
from core.model_manager import add_provider, load_config


def test_connection_uses_shared_protocol_request_path(monkeypatch):
    captured = {}

    def fake_request_stream(protocol, kwargs, *, timeout, cancel_event=None):
        captured.update(protocol=protocol, kwargs=kwargs, timeout=timeout)
        return iter([object()]), None

    monkeypatch.setattr("core.llm._request_stream", fake_request_stream)

    model_selector._test_model_connection(
        "responses", "deepseek-v4-flash", "https://api.deepseek.com", "secret"
    )

    assert captured["protocol"] == "responses"
    assert captured["kwargs"]["model"] == "openai/deepseek-v4-flash"
    assert captured["kwargs"]["stream"] is True
    assert captured["timeout"] == 15


def test_kimi_for_coding_has_parallel_openai_and_anthropic_presets():
    from core.model_selector import PRESET_PROVIDERS

    kimi_coding = [
        preset for preset in PRESET_PROVIDERS
        if "kimi-for-coding" in preset["name"]
    ]

    assert {(preset["protocol"], preset["base_url"]) for preset in kimi_coding} == {
        ("anthropic", "https://api.kimi.com/coding"),
        ("openai", "https://api.kimi.com/coding/v1"),
    }
    openai_preset = next(preset for preset in kimi_coding if preset["protocol"] == "openai")
    assert openai_preset["models"][0]["name"] == "kimi-for-coding"
from core.model_selector import _latest_total_tokens


def _run_selector_headless(monkeypatch):
    """用假 Application 捕获布局，不真正进入全屏 CLI"""
    captured = {}

    class FakeApp:
        def __init__(self, layout=None, key_bindings=None, style=None,
                     full_screen=False, mouse_support=False):
            captured["layout"] = layout
            captured["key_bindings"] = key_bindings

        def run(self):
            pass

    monkeypatch.setattr(model_selector, "Application", FakeApp)
    model_selector.open_model_selector()
    return captured


def test_list_shows_add_model_row_per_provider(monkeypatch, tmp_path):
    """每个厂商的模型列表下方应有一行「＋ 添加模型」入口"""
    monkeypatch.setattr(
        model_selector, "load_config",
        lambda: {
            "active_model_id": None,
            "providers": [
                {"id": "p1", "name": "厂商一", "protocol": "openai", "models": [
                    {"id": "m1", "name": "m1", "context_window": 1000},
                ]},
                {"id": "p2", "name": "厂商二", "protocol": "openai", "models": []},
            ],
        },
    )
    captured = _run_selector_headless(monkeypatch)
    main_win = captured["layout"].container.children[0]
    text = "".join(t for _, t in main_win.content.text())

    assert text.count("＋ 添加模型") == 2, "每个厂商（含无模型的）都应有一行添加模型入口"
    assert text.count("＋ 添加新厂商") == 1


def _press(captured, key, monkeypatch):
    """在捕获的 KeyBindings 中找到当前 filter 通过的目标按键 handler 并触发"""
    from types import SimpleNamespace
    kb = captured["key_bindings"]
    for binding in kb.bindings:
        if key in binding.keys:
            try:
                if binding.filter():
                    event = SimpleNamespace(
                        app=SimpleNamespace(exit=lambda: None, invalidate=lambda: None)
                    )
                    binding.handler(event)
                    return True
            except Exception:
                continue
    return False


def test_add_model_enter_opens_wizard(monkeypatch):
    """光标移到「＋ 添加模型」行按 Enter 应进入 addmodel 向导"""
    from prompt_toolkit.keys import Keys

    monkeypatch.setattr(
        model_selector, "load_config",
        lambda: {
            "active_model_id": None,
            "providers": [
                {"id": "p1", "name": "厂商一", "protocol": "openai", "models": []},
            ],
        },
    )
    captured = _run_selector_headless(monkeypatch)
    main_win = captured["layout"].container.children[0]

    # 初始光标已自动跳过不可选中的 provider 标题行，直接落在 add_model 行
    assert _press(captured, Keys.Enter, monkeypatch), "enter 键绑定应存在"

    # 应已进入 addmodel 向导模式，渲染包含厂商名与步骤提示
    text = "".join(t for _, t in main_win.content.text())
    assert "为「厂商一」添加模型" in text
    assert "步骤 1/3" in text


def test_latest_total_tokens_reads_current_value(monkeypatch):
    monkeypatch.setitem(llm.session_token_usage, "latest_total", 123)

    assert _latest_total_tokens() == 123


def test_latest_total_tokens_reads_under_lock():
    """读取必须持 token_usage_lock：锁被占用时读取阻塞，释放后才完成"""
    with llm.token_usage_lock:
        done = threading.Event()
        result = []

        def reader():
            result.append(_latest_total_tokens())
            done.set()

        t = threading.Thread(target=reader, daemon=True)
        t.start()
        assert not done.wait(0.3), "持锁期间读取应阻塞（未持锁读取则测试失败）"

    assert done.wait(2), "锁释放后读取应完成"


def test_add_model_to_provider_persists_supports_vision(monkeypatch, tmp_path):
    """add_model_to_provider 应将 supports_vision 写入 config.json"""
    from core.model_manager import add_model_to_provider, add_provider, load_config
    import config as config_mod

    config_file = tmp_path / "vision.json"
    monkeypatch.setattr(config_mod, "_CONFIG_FILE", str(config_file))
    monkeypatch.setattr("core.model_manager._CONFIG_FILE", str(config_file))

    pid = add_provider("Test", "https://test.com", "key", "openai")
    mid = add_model_to_provider(pid, "vision-model", 128000, supports_vision=True)

    cfg = load_config()
    model = next(m for p in cfg["providers"] for m in p["models"] if m["id"] == mid)
    assert model["supports_vision"] is True


def test_update_provider_and_model_persists_supports_vision(monkeypatch, tmp_path):
    """update_provider_and_model 应能更新模型的 supports_vision 字段"""
    from core.model_manager import (
        add_model_to_provider, add_provider, load_config, update_provider_and_model,
    )
    import config as config_mod

    config_file = tmp_path / "vision2.json"
    monkeypatch.setattr(config_mod, "_CONFIG_FILE", str(config_file))
    monkeypatch.setattr("core.model_manager._CONFIG_FILE", str(config_file))

    pid = add_provider("Test", "https://test.com", "key", "openai")
    mid = add_model_to_provider(pid, "vision-model", 128000, supports_vision=False)

    update_provider_and_model(pid, mid, "https://test.com", "key", "vision-model", 128000, supports_vision=True)

    cfg = load_config()
    model = next(m for p in cfg["providers"] for m in p["models"] if m["id"] == mid)
    assert model["supports_vision"] is True

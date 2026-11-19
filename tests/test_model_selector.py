import threading

import pytest

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


def test_kimi_for_coding_preset_is_openai_format():
    from core.model_selector import PRESET_PROVIDERS

    kimi_coding = [
        preset for preset in PRESET_PROVIDERS
        if "kimi-for-coding" in preset["name"]
    ]

    assert {(preset["protocol"], preset["base_url"]) for preset in kimi_coding} == {
        ("openai", "https://api.kimi.com/coding/v1"),
    }
    openai_preset = next(preset for preset in kimi_coding if preset["protocol"] == "openai")
    # 预置厂商只含厂商信息，不携带模型
    assert "models" not in openai_preset
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


def test_preset_add_via_selector_path_adds_provider_without_models(monkeypatch, tmp_path):
    """selector 的预置厂商添加入口只建厂商条目（模型由用户经「获取模型列表」添加）。"""
    import config as config_mod
    from core.model_manager import add_preset_provider_with_models, load_config

    config_file = tmp_path / "cap.json"
    monkeypatch.setattr(config_mod, "_CONFIG_FILE", str(config_file))
    monkeypatch.setattr("core.model_manager._CONFIG_FILE", str(config_file))

    preset = next(p for p in model_selector.PRESET_PROVIDERS if p["id"] == "kimi-coding-openai")
    pid = add_preset_provider_with_models(
        preset["name"], preset["base_url"], "key", preset["protocol"],
        preset.get("models", []), preset_id=preset["id"],
    )

    cfg = load_config()
    provider = next(p for p in cfg["providers"] if p["id"] == pid)
    assert provider["preset_id"] == "kimi-coding-openai"
    assert provider["models"] == []


# ---------------------------------------------------------------------------
# 多 Key 连接测试（厂商多 Key 一致性适配）
# ---------------------------------------------------------------------------


def test_connection_falls_back_across_multiple_keys(monkeypatch):
    """多 Key（换行分隔）连接测试：首 Key 失败后自动尝试下一个，任一成功即通过。"""
    calls = []

    def fake_request_stream(protocol, kwargs, *, timeout, cancel_event=None):
        calls.append(kwargs.get("api_key"))
        if kwargs.get("api_key") == "sk-bad":
            raise RuntimeError("401 invalid api key")
        return iter([object()]), None

    monkeypatch.setattr("core.llm._request_stream", fake_request_stream)

    model_selector._test_model_connection(
        "openai", "model-a", "https://api.example.com", "sk-bad\nsk-good"
    )

    assert calls == ["sk-bad", "sk-good"]


def test_connection_raises_last_error_when_all_keys_fail(monkeypatch):
    """多 Key 全部失败：抛出最后一个 Key 的错误（调用方错误展示逻辑不变）。

    逗号分隔串也按逐 Key 解析，证明 split_provider_keys 的分隔口径生效。
    """
    errors = iter([RuntimeError("first-key-error"), RuntimeError("second-key-error")])

    def fake_request_stream(protocol, kwargs, *, timeout, cancel_event=None):
        raise next(errors)

    monkeypatch.setattr("core.llm._request_stream", fake_request_stream)

    with pytest.raises(RuntimeError, match="second-key-error"):
        model_selector._test_model_connection(
            "openai", "model-a", "https://api.example.com", "sk-1,sk-2"
        )


# ---------------------------------------------------------------------------
# 「拉取可用模型」选择列表（CLI /models 的 fetch → 列表 → 选用）
# ---------------------------------------------------------------------------


def _press_last(captured, key):
    """按 prompt_toolkit 的分发顺序触发按键：调用最后一个 filter 通过的处理函数。

    key_processor 实际执行的是 matches[-1]（后注册优先），与这里的口径一致。
    """
    from types import SimpleNamespace

    class _Loop:
        def is_closed(self):
            return False

        def call_soon_threadsafe(self, callback):
            callback()

    kb = captured["key_bindings"]
    handlers = []
    for binding in kb.bindings:
        if key not in binding.keys:
            continue
        try:
            if binding.filter():
                handlers.append(binding.handler)
        except Exception:
            continue
    if not handlers:
        return False
    event = SimpleNamespace(
        app=SimpleNamespace(
            exit=lambda: None,
            invalidate=lambda: None,
            loop=_Loop(),
        )
    )
    handlers[-1](event)
    return True


def _find_input_buffer(container):
    """在布局里找到输入框 BufferControl 的 Buffer（用于模拟用户键入）。"""
    from prompt_toolkit.layout.controls import BufferControl

    if isinstance(container, BufferControl):
        return container.buffer
    children = getattr(container, "children", None)
    if children:
        for child in children:
            found = _find_input_buffer(child)
            if found is not None:
                return found
        return None
    content = getattr(container, "content", None)
    if content is not None:
        return _find_input_buffer(content)
    return None


def _wait_text(main_win, needle, timeout=3.0):
    """轮询主区域文本（拉取在后台线程里回投，需要等它落地）。"""
    import time

    def current():
        return "".join(text for _, text in main_win.content.text())

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if needle in current():
            return current()
        time.sleep(0.02)
    return current()


def _footer_text(layout):
    """列表模式的底部栏文本（只读 state["message"]，不读取配置文件）。"""
    footer_win = layout.container.children[1].content
    return "".join(text for _, text in footer_win.content.text())


def _wait_footer(layout, needle, timeout=3.0):
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if needle in _footer_text(layout):
            return True
        time.sleep(0.02)
    return False


def _one_provider_config():
    return {
        "active_model_id": None,
        "providers": [{
            "id": "p1",
            "name": "厂商一",
            "protocol": "openai",
            "base_url": "https://api.example.com",
            "api_key": "sk-test",
            "models": [],
        }],
    }


def _fake_fetch(models):
    def fake(base_url, api_keys=None, timeout=15.0):
        return {"ok": True, "models": models, "count": len(models), "base_url": base_url}

    return fake


def test_empty_model_name_enter_lists_fetched_models(monkeypatch):
    """给已有厂商添加模型时，名称留空回车即拉取并在下方列出可用模型。"""
    from prompt_toolkit.keys import Keys

    from core import model_discovery

    monkeypatch.setattr(model_selector, "load_config", _one_provider_config)
    monkeypatch.setattr(
        model_discovery, "fetch_model_list",
        _fake_fetch([{
            "id": "m-alpha",
            "model_type": "",
            "context_window": 200000,
            "supports_vision": True,
            "max_tokens": None,
        }]),
    )

    captured = _run_selector_headless(monkeypatch)
    main_win = captured["layout"].container.children[0]

    assert _press_last(captured, Keys.Enter)  # 光标默认在「＋ 添加模型」行
    assert "为「厂商一」添加模型" in "".join(t for _, t in main_win.content.text())

    assert _press_last(captured, Keys.Enter)  # 名称留空 → 拉取
    text = _wait_text(main_win, "共 1 个可用模型")
    assert "共 1 个可用模型" in text
    assert "m-alpha" in text
    assert "200k" in text

    assert _press_last(captured, Keys.Enter)  # 选用光标所在模型
    text = _wait_text(main_win, "模型名称: m-alpha")
    assert "模型名称: m-alpha" in text
    assert "步骤 3/3" in text  # 直接跳到多模态确认，上下文已由拉取结果填入


def test_fetch_failure_keeps_manual_input_available(monkeypatch):
    """拉取失败只提示错误，Esc 可回到手动输入模型名称。"""
    from prompt_toolkit.keys import Keys

    from core import model_discovery

    monkeypatch.setattr(model_selector, "load_config", _one_provider_config)
    monkeypatch.setattr(
        model_discovery, "fetch_model_list",
        lambda base_url, api_keys=None, timeout=15.0: {"ok": False, "msg": "获取失败 (HTTP 401): denied"},
    )

    captured = _run_selector_headless(monkeypatch)
    main_win = captured["layout"].container.children[0]

    assert _press_last(captured, Keys.Enter)  # 进入 addmodel 向导
    assert _press_last(captured, Keys.Enter)  # 拉取
    text = _wait_text(main_win, "HTTP 401")
    assert "按 r 重新拉取" in text

    assert _press_last(captured, Keys.Escape)  # 退回手动输入
    text = "".join(t for _, t in main_win.content.text())
    assert "步骤 1/3" in text
    assert "HTTP 401" not in text


def test_preset_flow_fetches_models_instead_of_reading_preset_models(monkeypatch, tmp_path):
    """预设厂商目录不再带模型：输入 Key 后拉取、勾选，保存时不再抛 KeyError: 'models'。"""
    from prompt_toolkit.document import Document
    from prompt_toolkit.keys import Keys

    import json

    import config as config_mod
    from core import model_discovery
    from core.model_manager import load_config as real_load_config

    config_file = tmp_path / "preset_fetch.json"
    # 必须先落一份「无厂商」的真实配置：文件不存在时 load_config 会返回
    # DeepSeek 默认结构，光标会落到默认模型行而不是「＋ 添加新厂商」
    config_file.write_text(
        json.dumps({"active_model_id": None, "providers": []}), encoding="utf-8"
    )
    monkeypatch.setattr(config_mod, "_CONFIG_FILE", str(config_file))
    monkeypatch.setattr("core.model_manager._CONFIG_FILE", str(config_file))
    monkeypatch.setattr(
        model_discovery, "fetch_model_list",
        _fake_fetch([
            {"id": "kimi-k3", "model_type": "", "context_window": 256000,
             "supports_vision": False, "max_tokens": None},
            {"id": "kimi-k3-vision", "model_type": "", "context_window": 256000,
             "supports_vision": True, "max_tokens": None},
        ]),
    )
    # 只关心保存链路，不真的发请求
    monkeypatch.setattr(model_selector, "_test_model_connection", lambda *a, **k: None)

    captured = _run_selector_headless(monkeypatch)
    main_win = captured["layout"].container.children[0]
    input_buffer = _find_input_buffer(captured["layout"].container)
    assert input_buffer is not None

    assert _press_last(captured, Keys.Enter)  # 「＋ 添加新厂商」
    assert _press_last(captured, Keys.Enter)  # 选中第一个预设厂商
    input_buffer.set_document(Document("sk-preset"))

    assert _press_last(captured, Keys.Enter)  # 提交 Key → 拉取
    text = _wait_text(main_win, "共 2 个可用模型")
    assert "共 2 个可用模型" in text

    assert _press_last(captured, " ")  # 勾选第一个
    assert _press_last(captured, Keys.Down)  # 移到第二个
    assert _press_last(captured, " ")  # 勾选第二个
    assert _press_last(captured, Keys.Enter)  # 保存所选模型

    # 等底部栏确认保存完成再读配置：并发读会让 Windows 的 os.replace 原子写
    # 撞上共享冲突（WinError 5）
    assert _wait_footer(captured["layout"], "已成功验证保存")

    provider = real_load_config()["providers"][0]
    assert provider["preset_id"] == "kimi-coding-openai"
    assert [m["name"] for m in provider["models"]] == ["kimi-k3", "kimi-k3-vision"]
    assert all(m["context_window"] == 256000 for m in provider["models"])
    assert provider["models"][1]["supports_vision"] is True


def test_custom_provider_flow_can_pick_fetched_model(monkeypatch, tmp_path):
    """自定义厂商：模型名称留空回车拉取，选中后直接带出上下文与多模态并保存。"""
    from prompt_toolkit.document import Document
    from prompt_toolkit.keys import Keys

    import json
    import time

    import config as config_mod
    from core import model_discovery
    from core.model_manager import load_config as real_load_config

    config_file = tmp_path / "custom_fetch.json"
    config_file.write_text(
        json.dumps({"active_model_id": None, "providers": []}), encoding="utf-8"
    )
    monkeypatch.setattr(config_mod, "_CONFIG_FILE", str(config_file))
    monkeypatch.setattr("core.model_manager._CONFIG_FILE", str(config_file))
    monkeypatch.setattr(
        model_discovery, "fetch_model_list",
        _fake_fetch([{
            "id": "gateway-model",
            "model_type": "",
            "context_window": 32000,
            "supports_vision": False,
            "max_tokens": None,
        }]),
    )
    monkeypatch.setattr(model_selector, "_test_model_connection", lambda *a, **k: None)
    errors = []
    monkeypatch.setattr(
        model_selector,
        "_format_test_error",
        lambda exc, model: errors.append(f"{type(exc).__name__}: {exc}") or "测试错误",
    )

    captured = _run_selector_headless(monkeypatch)
    main_win = captured["layout"].container.children[0]
    input_buffer = _find_input_buffer(captured["layout"].container)

    assert _press_last(captured, Keys.Enter)  # 「＋ 添加新厂商」
    for _ in range(len(model_selector.PRESET_PROVIDERS)):
        assert _press_last(captured, Keys.Down)  # 越过全部预设，落到「自定义手动添加厂商」
    assert _press_last(captured, Keys.Enter)  # 进入自定义流程

    for text in ("自建网关", "http://127.0.0.1:8000/v1", "sk-local"):
        input_buffer.set_document(Document(text))
        assert _press_last(captured, Keys.Enter)
    assert _press_last(captured, Keys.Enter)  # 协议选择（默认 OpenAI）

    assert _press_last(captured, Keys.Enter)  # 模型名称留空 → 拉取
    text = _wait_text(main_win, "共 1 个可用模型")
    assert "gateway-model" in text
    assert "32k" in text

    assert _press_last(captured, Keys.Enter)  # 选用
    text = _wait_text(main_win, "步骤 5c")
    assert "步骤 5c" in text

    assert _press_last(captured, Keys.Enter)  # 确认多模态开关（默认否）→ 验证保存

    # 等 UI 侧确认保存完成再读配置（并发读会撞上 os.replace 的 WinError 5）
    assert "模型连接测试成功" in _wait_text(main_win, "模型连接测试成功"), repr(errors)

    provider = real_load_config()["providers"][0]
    assert provider["name"] == "自建网关"
    model = provider["models"][0]
    assert model["name"] == "gateway-model"
    assert model["context_window"] == 32000
    assert model["supports_vision"] is False

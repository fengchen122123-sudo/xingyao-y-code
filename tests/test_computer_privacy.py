"""电脑操控隐私审计与统一退出（设计 22、23.2，Task 16 brief Step 1 逐字样例）。

全部 fake 注入：Provider 流、controller/backend 均为测试替身，绝不触发真实
键鼠、截屏、热键、浏览器或网络。语料扫描覆盖四类持久化面——DB messages、
Harness Journal（harness_* 表）、公开回合事件与运行日志；brief 两样例逐字，
另测 URL query token、Cookie、API key、profile 文件内容、preview token 与
response id 冲突错误消息的脱敏（11b 用户裁决项）。
"""

import contextlib
import json
import logging
import sqlite3
import threading
import unittest.mock as mock
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

import core.llm as llm_mod
import core.model_manager as model_manager
from core.computer.models import (
    ComputerAction,
    ComputerControlError,
    ComputerOwner,
)
from tests.computer_fakes import (
    GENERIC_CAPABILITY,
    NATIVE_BROWSER_CAPABILITY,
    FakeComputerBackend,
    FakeController,
    FakeHotkeyApi,
    fake_png_bytes,
    http_status,
    use_temp_config,
)
from tests.request_payload_snapshot import ALPHA_TOOL, ZETA_TOOL

GENERIC_SESSION = "privacy-generic"
NATIVE_SESSION = "privacy-native"
GENERIC_TURN = "turn-privacy-generic"
NATIVE_TURN = "turn-privacy-native"


# ---------------------------------------------------------------------------
# brief Step 1 逐字样例的 Recorder 与统一关闭入口
# ---------------------------------------------------------------------------


class Recorder:
    """brief 逐字样例的最小记录器：stop/close 按名追加调用序列。"""

    def __init__(self, calls, name):
        self._calls = calls
        self._name = name

    def stop(self, *args, **kwargs):
        self._calls.append(f"{self._name}.stop")

    def close(self, *args, **kwargs):
        self._calls.append(f"{self._name}.close")


def test_application_shutdown_stops_controller_before_preview_and_hotkey():
    calls = []
    shutdown_computer_runtime = _import_shutdown_computer_runtime()
    shutdown_computer_runtime(
        controller=Recorder(calls, "controller"),
        preview=Recorder(calls, "preview"),
        hotkey=Recorder(calls, "hotkey"),
    )
    assert calls == ["controller.stop", "preview.stop", "hotkey.close"]


def _import_shutdown_computer_runtime():
    from core.computer.runtime import shutdown_computer_runtime

    return shutdown_computer_runtime


# ---------------------------------------------------------------------------
# 语料：generic 与 native 投影链路真实执行 + 四类持久化面扫描
# ---------------------------------------------------------------------------


class _Corpus:
    """隐私扫描语料：__contains__ 扫全部持久化面；native_rows 单独暴露。"""

    def __init__(self, parts: dict):
        self._parts = dict(parts)

    def __contains__(self, needle) -> bool:
        return any(str(needle) in text for text in self._parts.values())

    @property
    def native_rows(self) -> str:
        return self._parts.get("native_rows", "")

    @property
    def parts(self) -> dict:
        return dict(self._parts)


class _FileLogHandler(logging.Handler):
    """把全部运行日志（含 DEBUG）落到 tmp_path/captured.log 供扫描。"""

    def __init__(self, path: Path):
        super().__init__(level=1)
        self._path = path
        self._path.touch(exist_ok=True)  # 无日志记录时文件也存在，扫描方零防御
        self._formatter = logging.Formatter(
            "%(asctime)s %(levelname)s [%(name)s] %(message)s"
        )

    def emit(self, record):
        try:
            with open(self._path, "a", encoding="utf-8") as handle:
                handle.write(self._formatter.format(record) + "\n")
        except Exception:
            pass


def _chunk(content="", tool_calls=None):
    delta = SimpleNamespace(
        content=content,
        reasoning_content=None,
        tool_calls=tool_calls,
        provider_specific_fields=None,
    )
    return SimpleNamespace(
        choices=[SimpleNamespace(delta=delta, finish_reason="stop")],
        usage=None,
    )


def _tool_call(index, call_id, name, arguments):
    return SimpleNamespace(
        index=index,
        id=call_id,
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def _patch_request_env(stack, *, protocol):
    stack.enter_context(
        mock.patch.object(llm_mod, "get_tools_schema", lambda: [ZETA_TOOL, ALPHA_TOOL])
    )
    stack.enter_context(
        mock.patch.object(
            model_manager, "get_model_protocol", lambda *_a, **_k: protocol
        )
    )
    stack.enter_context(
        mock.patch("core.memory_kairos.build_memory_system_section", new=lambda: "")
    )
    stack.enter_context(
        mock.patch.object(
            llm_mod, "_describe_images_with_secondary", lambda *_a, **_k: None
        )
    )
    stack.enter_context(
        mock.patch.object(llm_mod, "estimate_tokens", lambda *_a, **_k: 0)
    )
    stack.enter_context(
        mock.patch.object(llm_mod, "_supports_vision", lambda _model: True)
    )


def _png_path(tmp_path, name) -> Path:
    path = tmp_path / name
    path.write_bytes(fake_png_bytes(11))
    return path


def _owner(session, turn):
    return ComputerOwner(
        session_id=session,
        turn_id=turn,
        session_label=session,
        cancel_event=threading.Event(),
    )


def _run_generic_turn(stack, manager, controller, *, secret, navigate_url=""):
    """真实 generic 链路：type_text(secret)（+ 可选 navigate）→ 最终文本，persist=True。"""
    steps = [
        [_chunk(tool_calls=[_tool_call(
            0, "call-privacy-generic", "computer_action",
            json.dumps(
                {"kind": "type_text", "target": "browser", "text": secret},
                ensure_ascii=False,
            ),
        )])],
    ]
    if navigate_url:
        steps.append([_chunk(tool_calls=[_tool_call(
            0, "call-privacy-nav", "computer_action",
            json.dumps(
                {"kind": "navigate", "target": "browser", "url": navigate_url},
                ensure_ascii=False,
            ),
        )])])
    steps.append([_chunk("操控完成")])
    requests = []

    def fake_completion(**kwargs):
        requests.append(deepcopy(kwargs))
        return iter(steps[len(requests) - 1])

    stack.enter_context(mock.patch.object(llm_mod, "completion", fake_completion))
    stack.enter_context(
        mock.patch.object(llm_mod, "get_computer_control_service", lambda: controller)
    )
    stack.enter_context(
        mock.patch.object(
            core_app_context(), "get_manager", return_value=manager
        )
    )
    return list(llm_mod.chat_with_model_events(
        "openai/generic-vision",
        "fake-key",
        [{"role": "user", "content": "把内容敲进页面"}],
        None,
        persist=True,
        turn_id=GENERIC_TURN,
        parent_session=GENERIC_SESSION,
        parent_turn_id=GENERIC_TURN,
        permission_owner=GENERIC_TURN,
        consume_background_notifications=False,
        computer_capability=GENERIC_CAPABILITY,
    ))


def _native_call_events(secret, response_id):
    return [
        {"type": "response.created", "response": {"id": response_id}},
        {"type": "response.output_item.done", "item": {
            "type": "computer_call",
            "id": "cc_privacy",
            "call_id": "call_privacy",
            "status": "completed",
            "actions": [{"type": "type", "text": secret}],
        }},
        {"type": "response.completed", "response": {"id": response_id, "output": []}},
    ]


_NATIVE_FINAL_TEXT = [
    {"type": "response.output_text.delta", "delta": "操作完成"},
    {"type": "response.completed", "response": {"id": "resp_final", "output": []}},
]


def _run_native_turn(stack, manager, controller, *, secret, response_id):
    """真实 native 链路：computer_call(type secret) → 续传 → 最终文本，persist=True。"""
    responses = [_native_call_events(secret, response_id), _NATIVE_FINAL_TEXT]
    requests = []

    def fake_responses(**kwargs):
        requests.append(deepcopy(kwargs))
        return iter(responses[len(requests) - 1])

    stack.enter_context(
        mock.patch.object(llm_mod, "_litellm_responses", fake_responses)
    )
    stack.enter_context(
        mock.patch.object(llm_mod, "get_computer_control_service", lambda: controller)
    )
    stack.enter_context(
        mock.patch.object(
            core_app_context(), "get_manager", return_value=manager
        )
    )
    return list(llm_mod.chat_with_model_events(
        "openai/gpt-native-fake",
        "fake-key",
        [
            {"role": "system", "content": "原生回合系统指令"},
            {"role": "user", "content": "操作电脑"},
        ],
        "https://api.openai.com/v1",
        persist=True,
        turn_id=NATIVE_TURN,
        parent_session=NATIVE_SESSION,
        parent_turn_id=NATIVE_TURN,
        permission_owner=NATIVE_TURN,
        consume_background_notifications=False,
        computer_capability=NATIVE_BROWSER_CAPABILITY,
    ))


def core_app_context():
    import core.app_context as app_context_module

    return app_context_module


def exercise_generic_and_native_projection(tmp_path, secret, response_id, *, navigate_url=""):
    """真实走 generic 与 native 投影链路：DB/Journal/事件/日志全部落到 tmp_path。

    generic：computer_action type_text（输入正文含 secret）+ 可选 navigate
    （URL 含 query token）→ followup 多模态观察消息 → 最终文本；
    native：computer_call type(secret, response_id) → 脱敏投影行 → 续传 →
    最终文本。两回合均 persist=True（真实 Harness Journal + messages 表）。
    """
    from core.memory import MemoryManager

    tmp_path = Path(tmp_path)
    manager = MemoryManager(str(tmp_path / "runtime.db"))
    manager.create_session_explicit(GENERIC_SESSION)
    manager.create_session_explicit(NATIVE_SESSION)

    generic_controller = FakeController(
        owner=_owner(GENERIC_SESSION, GENERIC_TURN),
        capability=GENERIC_CAPABILITY,
        run_id="run-privacy-generic",
        png_path=str(_png_path(tmp_path, "generic-obs.png")),
    )
    native_controller = FakeController(
        owner=_owner(NATIVE_SESSION, NATIVE_TURN),
        capability=NATIVE_BROWSER_CAPABILITY,
        run_id="run-privacy-native",
        png_path=str(_png_path(tmp_path, "native-obs.png")),
    )

    handler = _FileLogHandler(tmp_path / "captured.log")
    root = logging.getLogger()
    root.addHandler(handler)
    events = []
    try:
        with contextlib.ExitStack() as stack:
            _patch_request_env(stack, protocol="openai")
            events.extend(_run_generic_turn(
                stack, manager, generic_controller,
                secret=secret, navigate_url=navigate_url,
            ))
        with contextlib.ExitStack() as stack:
            _patch_request_env(stack, protocol="responses")
            events.extend(_run_native_turn(
                stack, manager, native_controller,
                secret=secret, response_id=response_id,
            ))
    finally:
        root.removeHandler(handler)
        handler.close()
        manager.close()

    (tmp_path / "events.json").write_text(
        json.dumps(
            [{"kind": event.kind, "payload": event.payload} for event in events],
            ensure_ascii=False,
            default=str,
        ),
        encoding="utf-8",
    )


def read_text_database_journal_events_and_logs(tmp_path):
    """汇总四类持久化面的可扫描文本（DB messages + Harness Journal + 事件 + 日志）。"""
    tmp_path = Path(tmp_path)
    parts = {}
    db_path = tmp_path / "runtime.db"
    if db_path.exists():
        conn = sqlite3.connect(str(db_path))
        try:
            messages = conn.execute("SELECT role, content FROM messages").fetchall()
            parts["db_messages"] = "\n".join(
                f"{role}: {content}" for role, content in messages
            )
            journal_rows = []
            for table in ("harness_turns", "harness_steps", "harness_events"):
                try:
                    journal_rows.extend(
                        conn.execute(f"SELECT * FROM {table}").fetchall()
                    )
                except sqlite3.OperationalError:
                    pass
            parts["journal"] = str(journal_rows)
            native_rows = []
            for _role, content in messages:
                try:
                    parsed = json.loads(content)
                except (TypeError, ValueError):
                    continue
                projection = (
                    parsed.get("_computer_v1") if isinstance(parsed, dict) else None
                )
                if (
                    isinstance(projection, dict)
                    and projection.get("kind") == "native_call"
                ):
                    native_rows.append(content)
            parts["native_rows"] = "\n".join(native_rows)
        finally:
            conn.close()
    for name in ("events.json", "captured.log"):
        path = tmp_path / name
        if path.exists():
            parts[name.split(".")[0]] = path.read_text(encoding="utf-8")
    return _Corpus(parts)


def test_persisted_and_logged_computer_records_exclude_secrets(tmp_path):
    secret = "S3CRET-TYPED-TEXT"
    response_id = "resp_secret_123"
    exercise_generic_and_native_projection(tmp_path, secret, response_id)
    corpus = read_text_database_journal_events_and_logs(tmp_path)
    assert secret not in corpus
    assert response_id not in corpus
    assert "data:image/png;base64" not in corpus.native_rows


def test_url_query_token_cookie_and_api_key_never_reach_persistence(tmp_path):
    """URL query token、Cookie 值、API key 形态的输入绝不进任何持久化面。"""
    url_token = "URITOKEN-query-987654"
    cookie_value = "SID=cookie-secret-654321"
    api_key = "sk-priv-0123456789abcdefXYZ"
    secret = f"{cookie_value} {api_key}"
    exercise_generic_and_native_projection(
        tmp_path, secret, "resp_query_1",
        navigate_url=f"https://site.example/path?token={url_token}&x=1",
    )
    corpus = read_text_database_journal_events_and_logs(tmp_path)
    assert url_token not in corpus
    assert cookie_value not in corpus
    assert api_key not in corpus


def test_response_id_conflict_messages_redact_both_ids():
    """11b 用户裁决：冲突错误消息不得内嵌任一 response id 原文（可经 turn_error 外泄）。"""
    from core.computer.provider_openai import parse_openai_computer_events

    events = [
        {"type": "response.created", "response": {"id": "resp_secret_a"}},
        {"type": "response.completed", "response": {"id": "resp_secret_b", "output": []}},
    ]
    with pytest.raises(ComputerControlError) as raised:
        parse_openai_computer_events(events)
    assert raised.value.code == "computer_provider_protocol_error"
    assert "resp_secret_a" not in raised.value.message
    assert "resp_secret_b" not in raised.value.message


def test_llm_stream_response_id_conflict_message_redacts_both_ids():
    from core.llm import _merge_stream_response_id

    with pytest.raises(RuntimeError) as raised:
        _merge_stream_response_id("resp_secret_a", "resp_secret_b")
    message = str(raised.value)
    assert "resp_secret_a" not in message
    assert "resp_secret_b" not in message


# ---------------------------------------------------------------------------
# 统一退出的健壮性与真实 controller 语义
# ---------------------------------------------------------------------------


def _noop_abort(_cancel_event):
    return None


def _service_with_backends(backends):
    from core.computer.controller import ComputerControlService

    return ComputerControlService(
        backend_factory=lambda target: backends[target],
        generation_provider=lambda: 1,
        abort_streams=_noop_abort,
    )


def test_shutdown_computer_runtime_survives_cleanup_errors():
    """任一清理步骤抛错都不得阻断后续清理或向上传播。"""
    shutdown_computer_runtime = _import_shutdown_computer_runtime()

    class Boom:
        def stop(self, *args, **kwargs):
            raise RuntimeError("boom-controller")

    class BoomPreview:
        def stop(self):
            raise RuntimeError("boom-preview")

    class BoomHotkey:
        def close(self):
            raise RuntimeError("boom-hotkey")

    shutdown_computer_runtime(
        controller=Boom(), preview=BoomPreview(), hotkey=BoomHotkey()
    )


def test_shutdown_stops_active_run_and_closes_retained_browser(tmp_path):
    """统一退出：活动 run 停止（backend close）+ retained browser 一并清理。"""
    from core.computer.controller import ComputerControlService

    browser_backend = FakeComputerBackend("browser")
    desktop_backend = FakeComputerBackend("desktop")
    service = ComputerControlService(
        backend_factory=lambda target: (
            browser_backend if target == "browser" else desktop_backend
        ),
        generation_provider=lambda: 1,
        abort_streams=_noop_abort,
    )
    owner_one = _owner("session-a", "turn-a")
    owner_two = _owner("session-b", "turn-b")
    first = service.start_run(owner_one, GENERIC_CAPABILITY, target="browser")
    service.execute(
        first.run_id, owner_one, ComputerAction(kind="observe", target="browser")
    )
    service.complete_run(first.run_id, owner_one, retain_browser_session=True)
    assert browser_backend.close_count == 0  # retained：complete 不关闭

    second = service.start_run(owner_two, GENERIC_CAPABILITY, target="desktop")
    service.execute(
        second.run_id, owner_two, ComputerAction(kind="observe", target="desktop")
    )

    _import_shutdown_computer_runtime()(
        controller=service, reason="application_exit"
    )
    final = service.snapshot()
    assert final.run_id == second.run_id
    assert final.status == "stopped"
    assert final.stop_reason == "application_exit"
    assert desktop_backend.closed  # 活动 run backend 已关闭
    assert browser_backend.closed  # retained browser 一并清理


# ---------------------------------------------------------------------------
# 进程级轻量运行时：启动接线与紧急停止
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_computer_runtime():
    def _reset():
        try:
            from core.computer import runtime as runtime_module

            runtime_module._reset_runtime_for_tests()
        except ImportError:
            pass

    _reset()
    yield
    _reset()


def _disabled_settings():
    from core.computer.settings import ComputerSettingsSnapshot

    return ComputerSettingsSnapshot(
        browser_control_enabled=False,
        computer_control_enabled=False,
        computer_browser="edge",
        browser_options=(),
        profile_display_path="",
        platform_supported=True,
        generation=1,
    )


def _enabled_settings():
    from core.computer.settings import ComputerSettingsSnapshot

    return ComputerSettingsSnapshot(
        browser_control_enabled=True,
        computer_control_enabled=True,
        computer_browser="edge",
        browser_options=(),
        profile_display_path="",
        platform_supported=True,
        generation=1,
    )


def test_start_computer_runtime_with_defaults_is_lightweight():
    """两开关全关：不注册热键、不导入 Playwright、不触碰 backend/截图。"""
    import sys

    from core.computer.runtime import start_computer_runtime

    api = FakeHotkeyApi()
    before = {name for name in sys.modules if "playwright" in name}
    runtime = start_computer_runtime(
        settings_loader=lambda: _disabled_settings(),
        hotkey_factory=lambda: api,
    )
    try:
        assert runtime.hotkey is None  # 全关：不注册
        assert api.register_calls == 0
        after = {name for name in sys.modules if "playwright" in name}
        assert after == before  # 绝不导入 Playwright
    finally:
        from core.computer.runtime import shutdown_computer_runtime

        shutdown_computer_runtime(hotkey=runtime.hotkey)


def test_start_computer_runtime_registers_hotkey_when_enabled():
    from core.computer.hotkey import get_emergency_hotkey_snapshot
    from core.computer.runtime import (
        get_computer_runtime,
        start_computer_runtime,
    )

    api = FakeHotkeyApi()
    runtime = start_computer_runtime(
        settings_loader=lambda: _enabled_settings(),
        hotkey_factory=lambda: api,
    )
    try:
        assert runtime.hotkey is not None
        assert api.register_calls == 1
        assert get_emergency_hotkey_snapshot().available is True
        assert get_computer_runtime() is runtime
    finally:
        from core.computer.runtime import shutdown_computer_runtime

        shutdown_computer_runtime(hotkey=runtime.hotkey)
    assert get_emergency_hotkey_snapshot().available is False


def test_hotkey_registration_failure_keeps_config_untouched(tmp_path, monkeypatch):
    """注册失败：能力不可用，但绝不改写已开启的配置。"""
    use_temp_config(
        tmp_path,
        monkeypatch,
        {"browser_control_enabled": True, "computer_browser": "edge"},
    )
    from core.computer.runtime import start_computer_runtime

    api = FakeHotkeyApi(register_result=False)
    runtime = start_computer_runtime(hotkey_factory=lambda: api)
    assert runtime.hotkey is None
    config = json.loads(
        (tmp_path / "config.json").read_text(encoding="utf-8")
    )
    assert config["browser_control_enabled"] is True


def test_emergency_hotkey_stops_active_run(tmp_path):
    """Ctrl+Alt+Esc 回调：停止活动 run（reason=emergency_hotkey）。"""
    from core.computer.runtime import start_computer_runtime

    service = _service_with_backends({"desktop": FakeComputerBackend("desktop")})
    owner = _owner("session-esc", "turn-esc")
    snapshot = service.start_run(owner, GENERIC_CAPABILITY, target="desktop")
    service.execute(
        snapshot.run_id, owner, ComputerAction(kind="observe", target="desktop")
    )
    api = FakeHotkeyApi()
    runtime = start_computer_runtime(
        settings_loader=lambda: _enabled_settings(),
        hotkey_factory=lambda: api,
        controller_provider=lambda: service,
    )
    try:
        api.on_hotkey()  # 模拟 Ctrl+Alt+Esc
        final = service.snapshot()
        assert final.run_id == snapshot.run_id
        assert final.status == "stopped"
        assert final.stop_reason == "emergency_hotkey"
    finally:
        from core.computer.runtime import shutdown_computer_runtime

        shutdown_computer_runtime(controller=service, hotkey=runtime.hotkey)


def test_shutdown_process_computer_runtime_gathers_singletons(monkeypatch):
    from core.computer import runtime as runtime_module
    from core.computer.runtime import start_computer_runtime

    recorded = {}

    class FakeService:
        def stop(self, *args, **kwargs):
            recorded["stop_kwargs"] = kwargs

    monkeypatch.setattr(
        "core.computer.controller.get_computer_control_service",
        lambda: FakeService(),
    )
    monkeypatch.setattr(
        runtime_module,
        "shutdown_computer_runtime",
        lambda **kwargs: recorded.update(kwargs),
    )
    api = FakeHotkeyApi()
    runtime = start_computer_runtime(
        settings_loader=lambda: _enabled_settings(),
        hotkey_factory=lambda: api,
    )
    try:
        runtime_module.shutdown_process_computer_runtime("test_exit")
        assert isinstance(recorded.get("controller"), FakeService)
        assert recorded.get("hotkey") is runtime.hotkey
        assert recorded.get("reason") == "test_exit"
        assert runtime_module.get_computer_runtime() is None  # 单例已清空
    finally:
        # shutdown_computer_runtime 被 fake 替身接管：真实热键由测试显式注销，
        # 绝不让 _active_hotkey 注册表泄漏到后续测试。
        runtime.hotkey.close()


# ---------------------------------------------------------------------------
# 热键生命周期接线（Task 5 移交 → Task 16）：用户开启设置时首次注册、
# 活动能力存在时保持、两开关全关且无活动 run 时注销、退出统一注销
# ---------------------------------------------------------------------------


def test_refresh_hotkey_registers_on_enable_and_unregisters_when_idle():
    from core.computer.hotkey import get_emergency_hotkey_snapshot
    from core.computer.runtime import (
        refresh_computer_runtime_hotkey,
        shutdown_computer_runtime,
        start_computer_runtime,
    )

    state = {"enabled": False}

    def _settings():
        return _enabled_settings() if state["enabled"] else _disabled_settings()

    api = FakeHotkeyApi()
    runtime = start_computer_runtime(
        settings_loader=_settings, hotkey_factory=lambda: api
    )
    try:
        assert runtime.hotkey is None  # 全关启动：未注册
        state["enabled"] = True
        refresh_computer_runtime_hotkey(
            settings_loader=_settings, hotkey_factory=lambda: api
        )
        assert runtime.hotkey is not None  # 用户开启设置：首次注册
        assert api.register_calls == 1
        assert get_emergency_hotkey_snapshot().available is True

        state["enabled"] = False
        refresh_computer_runtime_hotkey(
            settings_loader=_settings, hotkey_factory=lambda: api
        )
        assert runtime.hotkey is None  # 两开关全关且无活动 run：注销
        assert api.unregister_calls == 1
        assert get_emergency_hotkey_snapshot().available is False
    finally:
        shutdown_computer_runtime(hotkey=runtime.hotkey)


def test_refresh_hotkey_keeps_hotkey_while_run_active():
    """活动能力存在时保持：两开关全关但活动 run 尚在收尾，热键不得注销。"""
    from core.computer.runtime import (
        refresh_computer_runtime_hotkey,
        shutdown_computer_runtime,
        start_computer_runtime,
    )

    service = _service_with_backends({"desktop": FakeComputerBackend("desktop")})
    owner = _owner("session-keep", "turn-keep")
    started = service.start_run(owner, GENERIC_CAPABILITY, target="desktop")
    service.execute(
        started.run_id, owner, ComputerAction(kind="observe", target="desktop")
    )
    api = FakeHotkeyApi()
    runtime = start_computer_runtime(
        settings_loader=lambda: _enabled_settings(),
        hotkey_factory=lambda: api,
        controller_provider=lambda: service,
    )
    try:
        assert runtime.hotkey is not None
        refresh_computer_runtime_hotkey(
            settings_loader=lambda: _disabled_settings(),
            hotkey_factory=lambda: api,
            controller_provider=lambda: service,
        )
        assert runtime.hotkey is not None  # 活动 run：保持注册
        assert api.unregister_calls == 0
    finally:
        shutdown_computer_runtime(controller=service, hotkey=runtime.hotkey)


def test_refresh_hotkey_without_runtime_is_noop():
    """运行时容器未启动：refresh 不自动启动、不读设置、零副作用。"""
    from core.computer.runtime import (
        get_computer_runtime,
        refresh_computer_runtime_hotkey,
    )

    refresh_computer_runtime_hotkey()
    assert get_computer_runtime() is None


def test_refresh_hotkey_settings_failure_keeps_current_registration():
    from core.computer.runtime import (
        refresh_computer_runtime_hotkey,
        shutdown_computer_runtime,
        start_computer_runtime,
    )

    api = FakeHotkeyApi()
    runtime = start_computer_runtime(
        settings_loader=lambda: _enabled_settings(),
        hotkey_factory=lambda: api,
    )
    try:
        def _boom():
            raise OSError("disk")

        refresh_computer_runtime_hotkey(
            settings_loader=_boom, hotkey_factory=lambda: api
        )
        assert runtime.hotkey is not None  # 读取失败：保持现状
    finally:
        shutdown_computer_runtime(hotkey=runtime.hotkey)


# ---------------------------------------------------------------------------
# 预览发布接线与 preview token / profile 内容审计
# ---------------------------------------------------------------------------


def test_controller_publishes_observation_png_to_lazy_preview(tmp_path):
    """observation 产生时发布 PNG 到惰性预览服务；URL 可访问并进事件/快照。"""
    from core.computer.preview_server import ComputerPreviewServer  # noqa: F401
    from core.computer.runtime import ComputerRuntime, shutdown_computer_runtime

    png_path = _png_path(tmp_path, "preview-obs.png")
    backend = FakeComputerBackend("browser", png_path=str(png_path))
    service = _service_with_backends({"browser": backend})
    runtime = ComputerRuntime(preview_root=tmp_path / "preview")
    service.set_preview_publisher(runtime.publish_observation)
    events = []
    service.add_listener(events.append)
    owner = _owner("session-preview", "turn-preview")
    snapshot = service.start_run(owner, GENERIC_CAPABILITY, target="browser")
    observation = service.execute(
        snapshot.run_id, owner, ComputerAction(kind="observe", target="browser")
    )

    assert observation.preview_url.startswith("http://127.0.0.1:")
    assert http_status(observation.preview_url) == 200
    observation_events = [
        event for event in events if event["type"] == "computer_observation"
    ]
    assert observation_events
    assert observation_events[-1]["preview_url"] == observation.preview_url
    assert service.snapshot().preview_url == observation.preview_url

    shutdown_computer_runtime(controller=service, preview=runtime.preview)
    assert not any((tmp_path / "preview").iterdir())  # 临时 PNG 已删除


def test_preview_token_never_reaches_logs_or_journal(tmp_path):
    """preview token 只进 GUI 事件 URL，绝不进 DB/Journal/运行日志。"""
    from core.computer.runtime import ComputerRuntime, shutdown_computer_runtime
    from core.memory import MemoryManager

    png_path = _png_path(tmp_path, "token-obs.png")
    backend = FakeComputerBackend("browser", png_path=str(png_path))
    service = _service_with_backends({"browser": backend})
    runtime = ComputerRuntime(preview_root=tmp_path / "preview")
    service.set_preview_publisher(runtime.publish_observation)

    manager = MemoryManager(str(tmp_path / "runtime.db"))
    manager.create_session_explicit(GENERIC_SESSION)
    handler = _FileLogHandler(tmp_path / "captured.log")
    root = logging.getLogger()
    root.addHandler(handler)
    token = ""
    try:
        with contextlib.ExitStack() as stack:
            _patch_request_env(stack, protocol="openai")
            steps = [
                [_chunk(tool_calls=[_tool_call(
                    0, "call-preview-token", "computer_action",
                    json.dumps({"kind": "observe", "target": "browser"}),
                )])],
                [_chunk("完成")],
            ]
            requests = []

            def fake_completion(**kwargs):
                requests.append(deepcopy(kwargs))
                return iter(steps[len(requests) - 1])

            stack.enter_context(
                mock.patch.object(llm_mod, "completion", fake_completion)
            )
            stack.enter_context(
                mock.patch.object(
                    llm_mod, "get_computer_control_service", lambda: service
                )
            )
            stack.enter_context(
                mock.patch.object(
                    core_app_context(), "get_manager", return_value=manager
                )
            )
            list(llm_mod.chat_with_model_events(
                "openai/generic-vision",
                "fake-key",
                [{"role": "user", "content": "看看页面"}],
                None,
                persist=True,
                turn_id=GENERIC_TURN,
                parent_session=GENERIC_SESSION,
                parent_turn_id=GENERIC_TURN,
                permission_owner=GENERIC_TURN,
                consume_background_notifications=False,
                computer_capability=GENERIC_CAPABILITY,
            ))
        token = runtime.preview.token  # 观察已发布，预览服务已惰性启动
        assert token
    finally:
        root.removeHandler(handler)
        handler.close()
        shutdown_computer_runtime(
            controller=service, preview=runtime.preview
        )  # 清理 retained backend 与预览临时 PNG
        manager.close()

    corpus = read_text_database_journal_events_and_logs(tmp_path)
    for surface in ("db_messages", "journal", "logs"):
        assert token not in corpus.parts.get(surface, ""), surface


def test_browser_profile_contents_stay_local(tmp_path):
    """浏览器 profile 文件内容绝不进入观察投影或运行日志（设计 22.2）。"""
    from core.computer.actions import observation_provider_projection
    from core.computer.browser_backend import BrowserComputerBackend
    from tests.computer_fakes import FakePlaywrightRuntime

    profile_dir = tmp_path / "edge"
    profile_dir.mkdir()
    sentinel = "PROFILE-COOKIE-DB-SENTINEL-xyz"
    (profile_dir / "Cookies").write_text(sentinel, encoding="utf-8")

    handler = _FileLogHandler(tmp_path / "captured.log")
    root = logging.getLogger()
    root.addHandler(handler)
    runtime = FakePlaywrightRuntime()
    backend = BrowserComputerBackend(
        "edge", profile_dir, playwright_factory=lambda: runtime
    )
    try:
        backend.start()
        observation = backend.observe("run-profile-audit")
    finally:
        backend.close()
        root.removeHandler(handler)
        handler.close()

    assert sentinel not in json.dumps(
        observation_provider_projection(observation), ensure_ascii=False
    )
    assert sentinel not in str(observation)
    assert sentinel not in (tmp_path / "captured.log").read_text(encoding="utf-8")

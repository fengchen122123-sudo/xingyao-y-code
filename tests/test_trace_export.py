"""T5 审计/排障轨迹导出测试。

需求（待开发/Codex与ZCode增量改进/实施计划与验收清单.md · T5）：
- `export_diagnostics_bundle(*, dest_dir, session_name="", full=False) -> str` 返回 zip 路径；
  包结构：manifest.json + journal.jsonl（turns/steps/events 逐行 JSON，含回合 ID）
  + doctor.json（复用 core.doctor.run_checks 结果）+ logs/（GUI 运行日志尾部，存在才打包）；
- 默认精简模式不含消息正文；full=True 需显式确认参数（confirm=True）且消息经脱敏；
- 单项缺失（无日志 / 无 manager / doctor 失败）优雅降级为"继续导出 + manifest 记录"；
- `core.memory.export_harness_journal(session_name="", since_ms=0) -> list[dict]`
  只读、仅 ID 与元数据（Harness 纪律：journal 不落正文）；
- CLI `/doctor export [--full y]` 注册（帮助列表 + 分派链 + 命令补全）。

隔离策略：conftest.py 已在收集阶段把 YCODE_DATA_DIR 指到临时目录；本文件用
tmp_path 构造独立 MemoryManager，对日志路径与 doctor 结果做定点打桩，
不触碰真实 ~/.ycode，也不发起真实网络请求。
"""

import json
import zipfile
from pathlib import Path

import pytest

import core.app_context as app_context
import core.doctor as doctor
import core.trace_export as trace_export
from core.harness.journal import HarnessJournal
from core.harness.models import HarnessEventKind, JournalEventDraft
from core.memory import MemoryManager


# ---------------------------------------------------------------------------
# 公共工具
# ---------------------------------------------------------------------------

def _patch_main_print(monkeypatch):
    """捕获 main.handle_slash_command 的 print_formatted_text 输出。"""
    import main

    printed = []
    monkeypatch.setattr(
        main, "print_formatted_text", lambda *args, **kwargs: printed.append(str(args[0]))
    )
    return printed


def _stub_doctor(monkeypatch):
    """doctor 检查打桩：不发网络、不碰真实数据目录。"""
    rows = [
        doctor.CheckResult(name="环境与版本", status="ok", detail="星瑶Code v-test", hint=""),
        doctor.CheckResult(name="模型端点", status="skip", detail="已跳过", hint=""),
    ]
    monkeypatch.setattr(doctor, "run_checks", lambda *, skip_network=False: rows)
    return rows


def _stub_missing_log(monkeypatch, tmp_path):
    monkeypatch.setattr(
        trace_export, "_gui_runtime_log_path", lambda: tmp_path / "no-such-gui-runtime.log"
    )


def _read_bundle_texts(zip_path):
    """解包 zip 并返回 {成员名: 文本}（日志等二进制成员按替换解码）。"""
    texts = {}
    with zipfile.ZipFile(zip_path) as bundle:
        for name in bundle.namelist():
            texts[name] = bundle.read(name).decode("utf-8", errors="replace")
    return texts


def _journal_rows(zip_path):
    with zipfile.ZipFile(zip_path) as bundle:
        lines = bundle.read("journal.jsonl").decode("utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


@pytest.fixture
def manager(tmp_path):
    """独立 MemoryManager，含一个会话与一条 turn/step/event 轨迹。"""
    manager = MemoryManager(str(tmp_path / "trace.db"))
    manager.create_session_explicit("s1")
    journal = HarnessJournal(manager, "owner-trace", clock_ms=lambda: 10_000)
    journal.create_turn("turn-1", "s1")
    journal.create_step("step-1", "turn-1", 1)
    journal.append_event(
        "turn-1",
        JournalEventDraft(
            kind=HarnessEventKind.TOOL_STARTED,
            step_id="step-1",
            tool_name="read_file",
            call_ordinal=0,
        ),
    )
    app_context.set_manager(manager)
    yield manager
    manager.close()


# ---------------------------------------------------------------------------
# ① zip 结构与 manifest
# ---------------------------------------------------------------------------

def test_bundle_zip_structure_and_manifest(manager, tmp_path, monkeypatch):
    _stub_doctor(monkeypatch)
    log_path = tmp_path / "gui-runtime.log"
    log_path.write_text("2026-09-22 GUI 启动日志\n", encoding="utf-8")
    monkeypatch.setattr(trace_export, "_gui_runtime_log_path", lambda: log_path)

    zip_path = trace_export.export_diagnostics_bundle(
        dest_dir=str(tmp_path / "out"), session_name="s1"
    )

    assert zip_path.endswith(".zip")
    assert Path(zip_path).is_file()
    assert Path(zip_path).name.startswith("ycode-diagnostics-")

    with zipfile.ZipFile(zip_path) as bundle:
        names = set(bundle.namelist())
        assert {"manifest.json", "journal.jsonl", "doctor.json"} <= names
        assert "logs/gui-runtime.log" in names

        manifest = json.loads(bundle.read("manifest.json").decode("utf-8"))
        assert manifest["bundle_format"] == "ycode-diagnostics"
        assert manifest["ycode_version"]
        assert manifest["platform"]["system"]
        assert manifest["platform"]["python"]
        assert manifest["session_name"] == "s1"
        assert manifest["mode"] == "slim"
        assert manifest["counts"]["turns"] == 1
        assert manifest["counts"]["steps"] == 1
        assert manifest["counts"]["events"] == 1
        assert manifest["time_range"]["started_at_ms"] == 10_000
        assert "logs/gui-runtime.log" in manifest["files"]
        assert manifest["errors"] == []

        rows = [json.loads(line) for line in bundle.read("journal.jsonl").decode("utf-8").splitlines() if line.strip()]
        assert {row["type"] for row in rows} == {"turn", "step", "event"}
        # T4 回合关联：每条 journal 记录都带回合 ID
        assert all(row.get("turn_id") for row in rows)

        doctor_payload = json.loads(bundle.read("doctor.json").decode("utf-8"))
        assert [row["name"] for row in doctor_payload] == ["环境与版本", "模型端点"]
        assert list(doctor_payload[0].keys()) == ["name", "status", "detail", "hint"]


# ---------------------------------------------------------------------------
# ② 精简模式（默认）不含消息正文
# ---------------------------------------------------------------------------

def test_slim_bundle_excludes_message_bodies(manager, tmp_path, monkeypatch):
    _stub_doctor(monkeypatch)
    _stub_missing_log(monkeypatch, tmp_path)
    manager.add_message("s1", "user", "TOP-SECRET-BODY：请帮我看看这段代码")

    zip_path = trace_export.export_diagnostics_bundle(
        dest_dir=str(tmp_path / "out"), session_name="s1"
    )

    texts = _read_bundle_texts(zip_path)
    assert "messages.jsonl" not in texts
    for name, content in texts.items():
        assert "TOP-SECRET-BODY" not in content, f"精简模式泄漏正文: {name}"


# ---------------------------------------------------------------------------
# ③ full=True 需显式确认参数；确认后经 _redact_export_text 脱敏
# ---------------------------------------------------------------------------

def test_full_mode_requires_explicit_confirmation(manager, tmp_path, monkeypatch):
    _stub_doctor(monkeypatch)
    _stub_missing_log(monkeypatch, tmp_path)

    with pytest.raises(ValueError, match="confirm"):
        trace_export.export_diagnostics_bundle(
            dest_dir=str(tmp_path / "out"), session_name="s1", full=True
        )


def test_full_mode_writes_redacted_messages(manager, tmp_path, monkeypatch):
    _stub_doctor(monkeypatch)
    _stub_missing_log(monkeypatch, tmp_path)
    manager.add_message("s1", "user", "请帮我检查 token=abcdef123456 这个配置")

    zip_path = trace_export.export_diagnostics_bundle(
        dest_dir=str(tmp_path / "out"), session_name="s1", full=True, confirm=True
    )

    with zipfile.ZipFile(zip_path) as bundle:
        assert "messages.jsonl" in bundle.namelist()
        text = bundle.read("messages.jsonl").decode("utf-8")
        manifest = json.loads(bundle.read("manifest.json").decode("utf-8"))

    rows = [json.loads(line) for line in text.splitlines() if line.strip()]
    assert rows and rows[0]["role"] == "user"
    assert "abcdef123456" not in text  # 密钥值已脱敏
    assert "***" in text
    assert manifest["mode"] == "full"
    assert manifest["counts"]["messages"] == 1


# ---------------------------------------------------------------------------
# ④ 单项缺失时的优雅降级
# ---------------------------------------------------------------------------

def test_missing_log_file_degrades_gracefully(manager, tmp_path, monkeypatch):
    _stub_doctor(monkeypatch)
    _stub_missing_log(monkeypatch, tmp_path)

    zip_path = trace_export.export_diagnostics_bundle(
        dest_dir=str(tmp_path / "out"), session_name="s1"
    )

    with zipfile.ZipFile(zip_path) as bundle:
        names = set(bundle.namelist())
        assert {"manifest.json", "journal.jsonl", "doctor.json"} <= names
        assert not any(name.startswith("logs/") for name in names)
        manifest = json.loads(bundle.read("manifest.json").decode("utf-8"))
    assert "logs/gui-runtime.log" not in manifest["files"]
    # 缺日志不算错误，但 manifest 明确记录该项未包含
    assert manifest["errors"] == []


def test_missing_manager_degrades_gracefully(tmp_path, monkeypatch):
    _stub_doctor(monkeypatch)
    _stub_missing_log(monkeypatch, tmp_path)
    monkeypatch.setattr(app_context, "_context", None)
    monkeypatch.setattr(app_context, "_manager_fallback", None)

    zip_path = trace_export.export_diagnostics_bundle(
        dest_dir=str(tmp_path / "out"), session_name="s1"
    )

    rows = _journal_rows(zip_path)
    assert rows == []
    with zipfile.ZipFile(zip_path) as bundle:
        manifest = json.loads(bundle.read("manifest.json").decode("utf-8"))
    assert manifest["counts"]["turns"] == 0
    assert any("journal" in item.lower() for item in manifest["errors"])


def test_doctor_failure_is_isolated(manager, tmp_path, monkeypatch):
    _stub_missing_log(monkeypatch, tmp_path)

    def boom(*, skip_network=False):
        raise RuntimeError("doctor boom")

    monkeypatch.setattr(doctor, "run_checks", boom)

    zip_path = trace_export.export_diagnostics_bundle(
        dest_dir=str(tmp_path / "out"), session_name="s1"
    )

    with zipfile.ZipFile(zip_path) as bundle:
        assert json.loads(bundle.read("doctor.json").decode("utf-8")) == []
        manifest = json.loads(bundle.read("manifest.json").decode("utf-8"))
    assert any("doctor" in item.lower() for item in manifest["errors"])
    # journal 仍完整导出，单项失败不拖垮整包
    assert {row["type"] for row in _journal_rows(zip_path)} == {"turn", "step", "event"}


# ---------------------------------------------------------------------------
# ⑤ memory.export_harness_journal：只读、元数据、过滤
# ---------------------------------------------------------------------------

def test_export_harness_journal_filters_by_session_and_since(tmp_path):
    manager = MemoryManager(str(tmp_path / "j.db"))
    manager.create_session_explicit("s1")
    manager.create_session_explicit("s2")
    journal = HarnessJournal(manager, "owner-j", clock_ms=lambda: 10_000)
    journal.create_turn("turn-a", "s1")
    journal.create_step("step-a", "turn-a", 1)
    manager.create_harness_turn("turn-b", "s1", "owner-j", 20_000)
    manager.create_harness_turn("turn-c", "s2", "owner-j", 30_000)

    rows = manager.export_harness_journal(session_name="s1")
    turns = [row for row in rows if row["type"] == "turn"]
    assert {row["turn_id"] for row in turns} == {"turn-a", "turn-b"}
    assert all(row["session_name"] == "s1" for row in turns)
    assert any(row["type"] == "step" and row["turn_id"] == "turn-a" for row in rows)

    since_rows = manager.export_harness_journal(session_name="s1", since_ms=15_000)
    assert {row["turn_id"] for row in since_rows if row["type"] == "turn"} == {"turn-b"}
    assert not any(row["type"] == "step" for row in since_rows)  # turn-a 的步进不越界带出

    all_rows = manager.export_harness_journal()
    assert {row["turn_id"] for row in all_rows if row["type"] == "turn"} == {
        "turn-a", "turn-b", "turn-c",
    }
    assert manager.export_harness_journal(session_name="missing") == []
    manager.close()


def test_export_harness_journal_records_metadata_only(tmp_path):
    manager = MemoryManager(str(tmp_path / "j.db"))
    manager.create_session_explicit("s1")
    journal = HarnessJournal(manager, "owner-j", clock_ms=lambda: 10_000)
    journal.create_turn("turn-1", "s1")
    journal.create_step("step-1", "turn-1", 1)
    journal.append_event(
        "turn-1",
        JournalEventDraft(
            kind=HarnessEventKind.TOOL_FINISHED,
            step_id="step-1",
            tool_name="execute_command",
            call_ordinal=3,
            arguments_sha256="a" * 64,
            result_sha256="b" * 64,
            duration_ms=12,
            result_status="ok",
        ),
    )

    rows = manager.export_harness_journal(session_name="s1")

    forbidden = {"content", "body", "text", "payload", "messages", "arguments", "result", "reasoning"}
    for row in rows:
        assert row["turn_id"]
        assert row["type"] in {"turn", "step", "event"}
        assert forbidden.isdisjoint(row.keys()), f"出现正文类字段: {row.keys()}"
    event = next(row for row in rows if row["type"] == "event")
    assert event["kind"] == "tool_finished"
    assert event["sequence"] == 1
    assert event["tool_name"] == "execute_command"
    assert event["arguments_sha256"] == "a" * 64
    step = next(row for row in rows if row["type"] == "step")
    assert step["step_id"] == "step-1"
    assert step["ordinal"] == 1
    manager.close()


def test_export_harness_journal_is_read_only(tmp_path):
    manager = MemoryManager(str(tmp_path / "j.db"))
    manager.create_session_explicit("s1")
    journal = HarnessJournal(manager, "owner-j", clock_ms=lambda: 10_000)
    journal.create_turn("turn-1", "s1")
    journal.create_step("step-1", "turn-1", 1)
    manager.add_message("s1", "user", "正文不应进入 journal 导出")

    def table_counts():
        return tuple(
            manager.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("harness_turns", "harness_steps", "harness_events")
        )

    before = table_counts()
    manager.export_harness_journal(session_name="s1")
    manager.export_harness_journal()
    assert table_counts() == before
    manager.close()


# ---------------------------------------------------------------------------
# ⑥ CLI `/doctor export [--full y]`
# ---------------------------------------------------------------------------

def test_doctor_export_cli_slim(monkeypatch, tmp_path):
    import main

    printed = _patch_main_print(monkeypatch)
    monkeypatch.setattr(main, "current_session", "session-cli-test")
    calls = []

    def fake_export(*, dest_dir, session_name="", full=False, confirm=False):
        calls.append(
            {"dest_dir": dest_dir, "session_name": session_name, "full": full, "confirm": confirm}
        )
        return str(tmp_path / "out" / "diag.zip")

    monkeypatch.setattr(trace_export, "export_diagnostics_bundle", fake_export)

    main.handle_slash_command("/doctor export")

    assert len(calls) == 1
    assert calls[0]["dest_dir"]
    assert calls[0]["session_name"] == "session-cli-test"
    assert calls[0]["full"] is False
    assert calls[0]["confirm"] is False
    assert any("diag.zip" in line for line in printed)


def test_doctor_export_cli_full_requires_confirmation(monkeypatch, tmp_path):
    import main

    printed = _patch_main_print(monkeypatch)
    calls = []
    monkeypatch.setattr(
        trace_export,
        "export_diagnostics_bundle",
        lambda **kwargs: calls.append(kwargs) or str(tmp_path / "diag.zip"),
    )

    main.handle_slash_command("/doctor export --full")

    assert calls == []
    text = "\n".join(printed)
    assert "确认" in text
    assert "/doctor export --full y" in text


def test_doctor_export_cli_full_confirmed(monkeypatch, tmp_path):
    import main

    _patch_main_print(monkeypatch)
    monkeypatch.setattr(main, "current_session", "session-cli-test")
    calls = []
    monkeypatch.setattr(
        trace_export,
        "export_diagnostics_bundle",
        lambda **kwargs: calls.append(kwargs) or str(tmp_path / "diag.zip"),
    )

    main.handle_slash_command("/doctor export --full y")

    assert len(calls) == 1
    assert calls[0]["full"] is True
    assert calls[0]["confirm"] is True


def test_doctor_export_cli_rejects_unknown_flags(monkeypatch, tmp_path):
    import main

    printed = _patch_main_print(monkeypatch)
    calls = []
    monkeypatch.setattr(
        trace_export,
        "export_diagnostics_bundle",
        lambda **kwargs: calls.append(kwargs) or str(tmp_path / "diag.zip"),
    )

    main.handle_slash_command("/doctor export --force")

    assert calls == []
    assert any("用法" in line for line in printed)


def test_doctor_export_cli_failure_is_reported(monkeypatch, tmp_path):
    import main

    printed = _patch_main_print(monkeypatch)

    def boom(**kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(trace_export, "export_diagnostics_bundle", boom)

    main.handle_slash_command("/doctor export")

    assert any("disk full" in line for line in printed)


def test_doctor_export_registered_in_help_and_completions(monkeypatch):
    import main

    printed = _patch_main_print(monkeypatch)
    main.handle_slash_command("/help")

    assert any("/doctor export" in line for line in printed)
    doctor_desc = next(desc for cmd, desc in main.COMMAND_COMPLETIONS if cmd == "/doctor")
    assert "export" in doctor_desc
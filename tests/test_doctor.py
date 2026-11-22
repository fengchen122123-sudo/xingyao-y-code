import inspect
import json
import sqlite3
import sys


import config
import core.doctor as doctor


def _patch_main_print(monkeypatch):
    import main

    printed = []
    monkeypatch.setattr(
        main, "print_formatted_text", lambda *args, **kwargs: printed.append(str(args[0]))
    )
    return printed


def _fake_results():
    return [
        doctor.CheckResult(name="环境与版本", status="ok", detail="星瑶Code v1.3.1", hint=""),
        doctor.CheckResult(name="配置", status="warn", detail="存在 1 个未知配置键", hint="删除拼错的键"),
        doctor.CheckResult(name="数据库", status="fail", detail="ycode.db 损坏", hint="备份后删除重建"),
        doctor.CheckResult(name="模型端点", status="skip", detail="已跳过网络探测", hint=""),
    ]


def test_run_checks_returns_eight_ordered_items(monkeypatch):
    monkeypatch.setattr(doctor, "_active_base_url", lambda: "https://api.example.com")
    monkeypatch.setattr(doctor, "_probe_endpoint", lambda url, timeout: (True, "HTTP 200"))

    results = doctor.run_checks()

    assert len(results) == 8
    assert [r.name for r in results] == [
        "环境与版本", "配置", "凭据", "数据目录", "数据库", "MCP 服务", "模型端点", "GUI 运行时",
    ]
    assert all(isinstance(r, doctor.CheckResult) for r in results)
    assert all(r.status in {"ok", "warn", "fail", "skip"} for r in results)


def test_run_checks_signature_is_keyword_only():
    params = inspect.signature(doctor.run_checks).parameters
    assert list(params) == ["skip_network"]
    assert params["skip_network"].kind is inspect.Parameter.KEYWORD_ONLY


def test_run_checks_isolates_item_exceptions(monkeypatch):
    def _boom():
        raise RuntimeError("检查内部故障")

    monkeypatch.setattr(doctor, "check_data_dir", _boom)
    monkeypatch.setattr(doctor, "_active_base_url", lambda: "https://api.example.com")
    monkeypatch.setattr(doctor, "_probe_endpoint", lambda url, timeout: (True, "HTTP 200"))

    results = doctor.run_checks()

    assert len(results) == 8
    row = next(r for r in results if r.name == "数据目录")
    assert row.status == "fail"
    assert "检查执行异常" in row.detail
    assert row.hint
    assert next(r for r in results if r.name == "环境与版本").status in {"ok", "warn"}


def test_run_checks_skip_network_skips_probe(monkeypatch):
    calls = []
    monkeypatch.setattr(
        doctor, "_probe_endpoint", lambda url, timeout: calls.append(url) or (True, "HTTP 200")
    )
    monkeypatch.setattr(doctor, "_active_base_url", lambda: "https://api.example.com")

    results = doctor.run_checks(skip_network=True)

    row = next(r for r in results if r.name == "模型端点")
    assert row.status == "skip"
    assert calls == []


def test_check_environment_reports_version_and_runtime():
    result = doctor.check_environment()

    assert result.status in {"ok", "warn"}
    assert result.name == "环境与版本"
    assert "星瑶Code" in result.detail
    assert "Python" in result.detail
    assert "打包版" in result.detail or "源码运行" in result.detail


def test_check_config_flags_unknown_keys(monkeypatch):
    monkeypatch.setattr(
        doctor, "_load_config", lambda: {"memory_compression": {"retain_cout": 3}}
    )

    result = doctor.check_config()

    assert result.status == "warn"
    assert "retain_cout" in result.detail
    assert result.hint


def test_check_config_flags_invalid_permission_mode(monkeypatch):
    monkeypatch.setattr(doctor, "_load_config", lambda: {"permission_mode": "yolo-unknown"})

    result = doctor.check_config()

    assert result.status == "warn"
    assert "权限模式" in result.detail
    assert "confirm" in result.hint


def test_check_config_ok_for_known_values(monkeypatch):
    monkeypatch.setattr(
        doctor, "_load_config", lambda: {"permission_mode": "accept_edits"}
    )

    assert doctor.check_config().status == "ok"


def test_check_credentials_flags_undecryptable_key(monkeypatch):
    monkeypatch.setattr(
        doctor,
        "_load_config",
        lambda: {"providers": [{"name": "深度求索", "api_key": "dpapi:bogus"}]},
    )

    def _boom(value):
        raise RuntimeError("DPAPI 解密失败")

    monkeypatch.setattr(doctor.credentials, "unprotect_secret", _boom)

    result = doctor.check_credentials()

    assert result.status == "fail"
    assert "深度求索" in result.detail
    assert result.hint


def test_check_credentials_ok_when_decryptable(monkeypatch):
    monkeypatch.setattr(
        doctor,
        "_load_config",
        lambda: {"providers": [{"name": "P", "api_key": "dpapi:good"}]},
    )
    monkeypatch.setattr(doctor.credentials, "unprotect_secret", lambda value: "sk-live")

    assert doctor.check_credentials().status == "ok"


def test_check_credentials_warns_when_no_key(monkeypatch):
    monkeypatch.setattr(
        doctor,
        "_load_config",
        lambda: {
            "active_model_id": "m1",
            "providers": [{"id": "p1", "name": "P", "api_key": ""}],
        },
    )

    result = doctor.check_credentials()

    assert result.status == "warn"
    assert "API Key" in result.detail
    assert result.hint


def test_check_data_dir_ok_and_writable(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "YCODE_DIR", str(tmp_path / "data"))

    assert doctor.check_data_dir().status == "ok"


def test_check_data_dir_fails_on_unwritable_path(monkeypatch, tmp_path):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setattr(config, "YCODE_DIR", str(blocker))

    result = doctor.check_data_dir()

    assert result.status == "fail"
    assert result.hint


def test_check_databases_ok_with_valid_db_and_missing_gui_db(monkeypatch, tmp_path):
    db = tmp_path / "ycode.db"
    sqlite3.connect(db).close()
    monkeypatch.setattr(config, "DB_FILE", str(db))
    monkeypatch.setattr(config, "GUI_DB_FILE", str(tmp_path / "ycode_gui.db"))

    result = doctor.check_databases()

    assert result.status == "ok"
    assert "ycode_gui.db" in result.detail


def test_check_databases_fail_on_corrupt_db(monkeypatch, tmp_path):
    bad = tmp_path / "ycode.db"
    bad.write_bytes(b"this is not a sqlite database" * 10)
    monkeypatch.setattr(config, "DB_FILE", str(bad))
    monkeypatch.setattr(config, "GUI_DB_FILE", "")

    result = doctor.check_databases()

    assert result.status == "fail"
    assert "ycode.db" in result.detail
    assert result.hint


def test_check_mcp_ok_when_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "MCP_FILE", str(tmp_path / "mcp.json"))

    assert doctor.check_mcp().status == "ok"


def test_check_mcp_fail_on_invalid_json(monkeypatch, tmp_path):
    path = tmp_path / "mcp.json"
    path.write_text("{invalid", encoding="utf-8")
    monkeypatch.setattr(config, "MCP_FILE", str(path))

    result = doctor.check_mcp()

    assert result.status == "fail"
    assert result.hint


def test_check_mcp_fail_on_unsafe_command(monkeypatch, tmp_path):
    path = tmp_path / "mcp.json"
    path.write_text(
        json.dumps({"mcpServers": {"evil": {"command": "C:/tmp/evil.exe"}}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "MCP_FILE", str(path))

    result = doctor.check_mcp()

    assert result.status == "fail"
    assert "evil" in result.detail


def test_check_mcp_ok_on_valid_server(monkeypatch, tmp_path):
    path = tmp_path / "mcp.json"
    path.write_text(
        json.dumps({"mcpServers": {"fs": {"command": "npx", "args": ["-y", "x"]}}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "MCP_FILE", str(path))

    assert doctor.check_mcp().status == "ok"


def test_check_model_endpoint_skips_network_when_requested(monkeypatch):
    called = []
    monkeypatch.setattr(
        doctor, "_probe_endpoint", lambda url, timeout: called.append(url) or (True, "HTTP 200")
    )

    result = doctor.check_model_endpoint(skip_network=True)

    assert result.status == "skip"
    assert called == []


def test_check_model_endpoint_skips_without_model(monkeypatch):
    monkeypatch.setattr(doctor, "_active_base_url", lambda: "")

    assert doctor.check_model_endpoint().status == "skip"


def test_check_model_endpoint_ok_when_reachable(monkeypatch):
    monkeypatch.setattr(doctor, "_active_base_url", lambda: "https://api.example.com")
    monkeypatch.setattr(doctor, "_probe_endpoint", lambda url, timeout: (True, "HTTP 401"))

    result = doctor.check_model_endpoint()

    assert result.status == "ok"
    assert "api.example.com" in result.detail


def test_check_model_endpoint_warns_on_unreachable(monkeypatch):
    monkeypatch.setattr(doctor, "_active_base_url", lambda: "https://api.example.com")
    monkeypatch.setattr(
        doctor, "_probe_endpoint", lambda url, timeout: (False, "URLError: timed out")
    )

    result = doctor.check_model_endpoint()

    assert result.status == "warn"
    assert "api.example.com" in result.detail
    assert result.hint


def _request_header(request, name):
    return next(
        (value for key, value in request.headers.items() if key.lower() == name.lower()),
        None,
    )


def _capture_probe_headers(monkeypatch, url):
    import urllib.request as urllib_request

    captured = {}

    class _FakeResp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

    def _fake_urlopen(request, timeout):
        captured["request"] = request
        return _FakeResp()

    monkeypatch.setattr(urllib_request, "urlopen", _fake_urlopen)
    ok, _detail = doctor._probe_endpoint(url, timeout=1)
    return ok, captured["request"]


def test_probe_endpoint_carries_tokenrhythm_agent_header_for_tokenrhythm(monkeypatch):
    from core.llm_policy import TOKENRHYTHM_AGENT_NAME

    ok, request = _capture_probe_headers(monkeypatch, "https://tokenrhythm.studio/v1")

    assert ok is True
    assert _request_header(request, "X-TokenRhythm-Agent") == TOKENRHYTHM_AGENT_NAME


def test_probe_endpoint_omits_tokenrhythm_agent_header_for_other_hosts(monkeypatch):
    ok, request = _capture_probe_headers(monkeypatch, "https://api.example.com")

    assert ok is True
    assert _request_header(request, "X-TokenRhythm-Agent") is None


def test_check_gui_runtime_skips_off_windows(monkeypatch):
    import core.gui_runtime_check as grc

    called = []
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(grc, "detect_webview2_runtime", lambda: called.append(1) or "1.0")

    result = doctor.check_gui_runtime()

    assert result.status == "skip"
    assert called == []


def test_check_gui_runtime_warns_without_webview2(monkeypatch):
    import core.gui_runtime_check as grc

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(grc, "detect_webview2_runtime", lambda: None)

    result = doctor.check_gui_runtime()

    assert result.status == "warn"
    assert grc.WEBVIEW2_DOWNLOAD_URL in result.hint


def test_check_gui_runtime_ok_with_version(monkeypatch):
    import core.gui_runtime_check as grc

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(grc, "detect_webview2_runtime", lambda: "131.0.2903.86")

    result = doctor.check_gui_runtime()

    assert result.status == "ok"
    assert "131.0.2903.86" in result.detail


def test_check_result_to_dict_schema_is_stable():
    result = doctor.CheckResult(name="X", status="ok", detail="d", hint="h")

    assert list(result.to_dict().keys()) == ["name", "status", "detail", "hint"]
    assert result.to_dict() == {"name": "X", "status": "ok", "detail": "d", "hint": "h"}


def test_doctor_registered_in_command_completions():
    import main

    assert any(cmd == "/doctor" for cmd, _desc in main.COMMAND_COMPLETIONS)


def test_doctor_listed_in_help(monkeypatch):
    import main

    printed = _patch_main_print(monkeypatch)
    main.handle_slash_command("/help")

    assert any("/doctor" in line for line in printed)


def test_doctor_command_human_readable_output(monkeypatch):
    import main

    printed = _patch_main_print(monkeypatch)
    monkeypatch.setattr(
        doctor, "run_checks", lambda *, skip_network=False: _fake_results()
    )

    main.handle_slash_command("/doctor")

    text = "\n".join(printed)
    assert "聚合自检" in text
    assert "环境与版本" in text
    assert "警告" in text
    assert "失败" in text
    assert "跳过" in text
    assert "删除拼错的键" in text
    assert "未知指令" not in text


def test_doctor_command_json_output_schema(monkeypatch):
    import main

    printed = _patch_main_print(monkeypatch)
    monkeypatch.setattr(
        doctor, "run_checks", lambda *, skip_network=False: _fake_results()
    )

    main.handle_slash_command("/doctor --json")

    payload = json.loads("\n".join(printed))
    assert isinstance(payload, list)
    assert len(payload) == 4
    for row in payload:
        assert list(row.keys()) == ["name", "status", "detail", "hint"]
    assert payload[1]["status"] == "warn"
    assert payload[1]["name"] == "配置"


def test_doctor_command_rejects_unknown_flag(monkeypatch):
    import main

    printed = _patch_main_print(monkeypatch)
    calls = []
    monkeypatch.setattr(
        doctor, "run_checks", lambda *, skip_network=False: calls.append(1) or _fake_results()
    )

    main.handle_slash_command("/doctor --full")

    assert calls == []
    assert any("用法" in line for line in printed)
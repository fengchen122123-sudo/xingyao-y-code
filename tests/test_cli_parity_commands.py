import os


import main


def _capture_print(monkeypatch):
    printed = []
    monkeypatch.setattr(
        main, "print_formatted_text", lambda *args, **kwargs: printed.append(str(args[0]))
    )
    return printed


def _quiet_ui(monkeypatch):
    monkeypatch.setattr(main, "app_instance", None)


def test_clear_history_requires_confirmation(monkeypatch):
    printed = _capture_print(monkeypatch)
    _quiet_ui(monkeypatch)
    calls = []
    monkeypatch.setattr(main, "current_session", "s1")

    class _FakeManager:
        def clear_session_messages(self, session):
            calls.append(session)
            return 5

    monkeypatch.setattr(main, "manager", _FakeManager())

    main.handle_slash_command("/clear-history")

    assert calls == []
    assert any("确认" in line for line in printed)


def test_clear_history_confirmed_resets_token_usage(monkeypatch):
    printed = _capture_print(monkeypatch)
    _quiet_ui(monkeypatch)
    calls = []
    monkeypatch.setattr(main, "current_session", "s1")

    class _FakeManager:
        def clear_session_messages(self, session):
            calls.append(session)
            return 7

    monkeypatch.setattr(main, "manager", _FakeManager())

    import core.llm as llm
    monkeypatch.setattr(
        llm, "session_token_usage", {"latest_total": 99, "latest_prompt": 50, "latest_cached": 10}
    )

    main.handle_slash_command("/clear-history y")

    assert calls == ["s1"]
    assert llm.session_token_usage["latest_total"] == 0
    assert llm.session_token_usage["latest_prompt"] == 0
    assert llm.session_token_usage["latest_cached"] == 0
    assert any("已清空 7 条" in line for line in printed)


def test_persona_show_displays_current_settings(monkeypatch):
    printed = _capture_print(monkeypatch)
    _quiet_ui(monkeypatch)

    import core.persona_settings as ps
    monkeypatch.setattr(
        ps,
        "get_persona_settings",
        lambda: {"agent_name": "星瑶", "persona": "测试人设", "user_title": "老板"},
    )

    main.handle_slash_command("/persona")

    assert any("星瑶" in line for line in printed)
    assert any("老板" in line for line in printed)
    assert any("测试人设" in line for line in printed)


def test_persona_name_saves_merged_snapshot(monkeypatch):
    printed = _capture_print(monkeypatch)
    _quiet_ui(monkeypatch)
    calls = []

    import core.persona_settings as ps
    monkeypatch.setattr(
        ps,
        "get_persona_settings",
        lambda: {"agent_name": "星瑶", "persona": "人设内容", "user_title": "老板"},
    )
    monkeypatch.setattr(ps, "save_persona_settings", lambda *args: calls.append(args))

    main.handle_slash_command("/persona name 小瑶")

    assert calls == [("小瑶", "人设内容", "老板")]
    assert any("小瑶" in line for line in printed)


def test_persona_text_over_limit_rejected(monkeypatch):
    printed = _capture_print(monkeypatch)
    _quiet_ui(monkeypatch)
    calls = []

    import core.persona_settings as ps
    monkeypatch.setattr(ps, "get_persona_settings", lambda: {"agent_name": "星瑶", "persona": "", "user_title": ""})

    def _fake_save(agent_name, persona, user_title=""):
        if len(persona) > ps.MAX_PERSONA_LEN:
            raise ps.PersonaSettingsError(f"人设内容过长（最多 {ps.MAX_PERSONA_LEN} 字符）")
        calls.append((agent_name, persona, user_title))

    monkeypatch.setattr(ps, "save_persona_settings", _fake_save)

    main.handle_slash_command(f"/persona text {'字' * 20001}")

    assert calls == []
    assert any("过长" in line for line in printed)


def test_persona_reset_restores_defaults(monkeypatch):
    printed = _capture_print(monkeypatch)
    _quiet_ui(monkeypatch)
    calls = []

    import core.persona_settings as ps
    monkeypatch.setattr(ps, "get_persona_settings", lambda: {"agent_name": "自定义", "persona": "x", "user_title": "y"})
    monkeypatch.setattr(ps, "save_persona_settings", lambda *args: calls.append(args))

    main.handle_slash_command("/persona reset")

    assert calls == [(ps.DEFAULT_AGENT_NAME, "", "")]


def _fake_shell_options():
    return [
        {"id": "cmd", "label": "命令提示符", "description": "Windows Cmd", "available": True,
         "executable": "C:/Windows/System32/cmd.exe", "unavailable_reason": ""},
        {"id": "pwsh", "label": "PowerShell", "description": "PowerShell 7", "available": False,
         "executable": "", "unavailable_reason": "未安装"},
    ]


def test_shell_list_marks_current_and_unavailable(monkeypatch):
    printed = _capture_print(monkeypatch)
    _quiet_ui(monkeypatch)

    import core.command_shell as cs
    monkeypatch.setattr(cs, "list_command_shell_options", _fake_shell_options)
    monkeypatch.setattr(cs, "get_selected_command_shell", lambda: "cmd")

    main.handle_slash_command("/shell")

    assert any("cmd" in line for line in printed)
    assert any("（当前）" in line for line in printed)
    assert any("未安装" in line for line in printed)


def test_shell_set_success_and_failures(monkeypatch):
    printed = _capture_print(monkeypatch)
    _quiet_ui(monkeypatch)
    calls = []

    import core.command_shell as cs
    monkeypatch.setattr(cs, "list_command_shell_options", _fake_shell_options)
    monkeypatch.setattr(cs, "set_selected_command_shell", lambda shell_id: calls.append(shell_id))

    main.handle_slash_command("/shell pwsh")
    assert calls == []
    assert any("未安装" in line for line in printed)

    main.handle_slash_command("/shell nope")
    assert calls == []
    assert any("不支持" in line for line in printed)

    main.handle_slash_command("/shell cmd")
    assert calls == ["cmd"]
    assert any("命令提示符" in line for line in printed)


def test_open_default_workdir_and_relative_path(monkeypatch, tmp_path):
    opened = []
    printed = _capture_print(monkeypatch)
    _quiet_ui(monkeypatch)
    monkeypatch.setattr(os, "startfile", lambda path: opened.append(path))

    import core.workspace_context as wc
    monkeypatch.setattr(wc, "get_workdir", lambda: str(tmp_path))

    (tmp_path / "note.txt").write_text("hi", encoding="utf-8")

    main.handle_slash_command("/open")
    assert opened == [os.path.abspath(str(tmp_path))]

    main.handle_slash_command("/open note.txt")
    assert opened[-1] == os.path.abspath(str(tmp_path / "note.txt"))


def test_open_missing_path_reports_error(monkeypatch, tmp_path):
    opened = []
    printed = _capture_print(monkeypatch)
    _quiet_ui(monkeypatch)
    monkeypatch.setattr(os, "startfile", lambda path: opened.append(path))

    import core.workspace_context as wc
    monkeypatch.setattr(wc, "get_workdir", lambda: str(tmp_path))

    main.handle_slash_command("/open 不存在.png")

    assert opened == []
    assert any("路径不存在" in line for line in printed)


def test_skills_refresh_reports_counts(monkeypatch):
    printed = _capture_print(monkeypatch)
    _quiet_ui(monkeypatch)

    import core.skills as skills_mod
    fake_skills = [
        {"id": "builtin:alpha", "name": "a", "description": "d"},
        {"id": "builtin:beta", "name": "b", "description": "d"},
        {"id": "user:gamma", "name": "g", "description": "d"},
    ]
    monkeypatch.setattr(skills_mod, "scan_all_skills", lambda: fake_skills)
    monkeypatch.setattr(skills_mod, "get_active_skill_ids", lambda: ["builtin:alpha", "user:gamma"])

    main.handle_slash_command("/skills refresh")

    assert any("共 3 个技能" in line for line in printed)
    assert any("已启用 2 个" in line for line in printed)


def _drain_main_queue():
    import queue as queue_mod

    items = []
    while True:
        try:
            items.append(main.msg_queue.get_nowait())
        except queue_mod.Empty:
            break
    return items


def _run_threads_inline(monkeypatch):

    class _InlineThread:
        def __init__(self, target=None, daemon=None, args=None, kwargs=None):
            self._target = target

        def start(self):
            if self._target is not None:
                self._target()

    monkeypatch.setattr(main.threading, "Thread", _InlineThread)


def test_queue_list_preserves_order(monkeypatch):
    printed = _capture_print(monkeypatch)
    _quiet_ui(monkeypatch)
    _drain_main_queue()
    for text, session in [("first", "s1"), ("second", "s1"), ("third", "s2")]:
        main.msg_queue.put({"text": text, "was_queued": True, "session": session})

    main.handle_slash_command("/queue")

    assert main.msg_queue.qsize() == 3
    assert [i["text"] for i in _drain_main_queue()] == ["first", "second", "third"]
    assert any("first" in line for line in printed)
    assert any("3 条" in line for line in printed)


def test_queue_cancel_removes_by_index(monkeypatch):
    printed = _capture_print(monkeypatch)
    _quiet_ui(monkeypatch)
    _drain_main_queue()
    for text in ["first", "second", "third"]:
        main.msg_queue.put({"text": text, "was_queued": True, "session": "s1"})

    main.handle_slash_command("/queue cancel 2")

    assert [i["text"] for i in _drain_main_queue()] == ["first", "third"]
    assert any("已取消排队消息 2" in line for line in printed)


def test_queue_cancel_out_of_range_keeps_queue(monkeypatch):
    printed = _capture_print(monkeypatch)
    _quiet_ui(monkeypatch)
    _drain_main_queue()
    main.msg_queue.put({"text": "only", "was_queued": True, "session": "s1"})

    main.handle_slash_command("/queue cancel 5")

    assert [i["text"] for i in _drain_main_queue()] == ["only"]
    assert any("超出范围" in line for line in printed)


def test_queue_clear_discards_all(monkeypatch):
    printed = _capture_print(monkeypatch)
    _quiet_ui(monkeypatch)
    _drain_main_queue()
    main.msg_queue.put({"text": "a", "was_queued": True, "session": "s1"})
    main.msg_queue.put({"text": "b", "was_queued": True, "session": "s1"})

    main.handle_slash_command("/queue clear")

    assert main.msg_queue.qsize() == 0
    assert any("已清空 2 条" in line for line in printed)


class _FakeClipboardImage:
    def __init__(self):
        self.saved_path = ""

    def save(self, path, fmt):
        self.saved_path = path
        with open(path, "wb") as fh:
            fh.write(b"\x89PNG fake")


def _patch_image_grab(monkeypatch, grab_result):
    from PIL import ImageGrab

    monkeypatch.setattr(ImageGrab, "grabclipboard", lambda: grab_result)


def test_paste_image_saves_temp_and_enqueues(monkeypatch):
    printed = _capture_print(monkeypatch)
    _quiet_ui(monkeypatch)
    monkeypatch.setattr(main, "is_thinking", False)
    monkeypatch.setattr(main, "current_session", "s1")
    _drain_main_queue()
    image = _FakeClipboardImage()
    _patch_image_grab(monkeypatch, image)

    main.handle_slash_command("/paste-image 这里有个bug")

    item = main.msg_queue.get_nowait()
    assert main.msg_queue.qsize() == 0
    assert item["text"] == "这里有个bug"
    assert len(item["explicit_paths"]) == 1
    assert "ycode_clip_" in item["explicit_paths"][0] and item["explicit_paths"][0].endswith(".png")
    assert item["session"] == "s1"
    assert image.saved_path
    assert any("[剪贴板图片]" in line for line in printed)


def test_paste_image_with_file_path_list_uses_paths_directly(monkeypatch, tmp_path):
    printed = _capture_print(monkeypatch)
    _quiet_ui(monkeypatch)
    monkeypatch.setattr(main, "is_thinking", False)
    monkeypatch.setattr(main, "current_session", "s1")
    _drain_main_queue()
    source = tmp_path / "新建 文件夹 (2)" / "shot.png"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"\x89PNG fake")
    _patch_image_grab(monkeypatch, [str(source)])

    main.handle_slash_command("/paste-image")

    item = main.msg_queue.get_nowait()
    assert item["explicit_paths"] == [str(source)]
    assert str(source) not in item["text"]


def test_paste_image_without_image_reports_empty(monkeypatch):
    printed = _capture_print(monkeypatch)
    _quiet_ui(monkeypatch)
    _drain_main_queue()
    _patch_image_grab(monkeypatch, None)

    main.handle_slash_command("/paste-image")

    assert main.msg_queue.qsize() == 0
    assert any("剪贴板中没有图片" in line for line in printed)


class _FakeDownloader:
    def __init__(self, downloading=False, started=True):
        self._downloading = downloading
        self._started = started
        self.calls = []
        self.cancelled = False

    def is_downloading(self):
        return self._downloading

    def start_download(self, **kwargs):
        self.calls.append(kwargs)
        return self._started

    def cancel(self):
        self.cancelled = True


def test_update_check_latest_and_new_version(monkeypatch):
    printed = _capture_print(monkeypatch)
    _quiet_ui(monkeypatch)
    _run_threads_inline(monkeypatch)

    import core.updater as upd
    monkeypatch.setattr(upd, "fetch_latest_release", lambda: {
        "ok": True, "has_update": False, "current_version": "1.2.26",
    })
    main.handle_slash_command("/update")
    assert any("已是最新版本" in line for line in printed)

    printed.clear()
    monkeypatch.setattr(upd, "fetch_latest_release", lambda: {
        "ok": True, "has_update": True, "latest_version": "1.3.0",
        "current_version": "1.2.26", "release_notes": "修复若干问题",
        "asset_size": 120 * 1024 * 1024, "sha256": "abc", "part_urls": [],
        "is_split": False, "part_count": 0, "download_url": "https://x/y.exe",
    })
    monkeypatch.setattr(upd, "find_cached_installer", lambda *a: "")
    main.handle_slash_command("/update check")
    assert any("发现新版本 v1.3.0" in line for line in printed)
    assert any("修复若干问题" in line for line in printed)
    assert any("/update download" in line for line in printed)


def test_update_check_fetch_error_is_not_reported_as_latest(monkeypatch):
    printed = _capture_print(monkeypatch)
    _quiet_ui(monkeypatch)
    _run_threads_inline(monkeypatch)

    import core.updater as upd
    monkeypatch.setattr(upd, "fetch_latest_release", lambda: {
        "ok": False, "has_update": False, "msg": "网络连接失败",
    })

    main.handle_slash_command("/update check")

    assert any("网络连接失败" in line for line in printed)
    assert not any("已是最新版本" in line for line in printed)


def test_update_download_starts_with_release_params(monkeypatch):
    printed = _capture_print(monkeypatch)
    _quiet_ui(monkeypatch)
    _run_threads_inline(monkeypatch)
    downloader = _FakeDownloader()
    monkeypatch.setattr(main, "_get_update_downloader", lambda: downloader)

    import core.updater as upd
    monkeypatch.setattr(upd, "fetch_latest_release", lambda: {
        "ok": True, "has_update": True, "latest_version": "1.3.0",
        "current_version": "1.2.26", "release_notes": "", "asset_size": 1000,
        "sha256": "ABC", "part_urls": ["https://x/y.part1"], "is_split": True,
        "part_count": 1, "download_url": "https://x/tag",
    })

    main.handle_slash_command("/update download")

    assert len(downloader.calls) == 1
    call = downloader.calls[0]
    assert call["target_version"] == "1.3.0"
    assert call["part_urls"] == ["https://x/y.part1"]
    assert call["expected_sha256"] == "abc"
    assert any("已开始下载 v1.3.0" in line for line in printed)


def test_update_download_fetch_error_reports_failure(monkeypatch):
    printed = _capture_print(monkeypatch)
    _quiet_ui(monkeypatch)
    _run_threads_inline(monkeypatch)
    downloader = _FakeDownloader()
    monkeypatch.setattr(main, "_get_update_downloader", lambda: downloader)

    import core.updater as upd
    monkeypatch.setattr(upd, "fetch_latest_release", lambda: {
        "ok": False, "has_update": False, "msg": "Gitee 连接超时",
    })

    main.handle_slash_command("/update download")

    assert downloader.calls == []
    assert any("Gitee 连接超时" in line for line in printed)
    assert not any("已是最新版本" in line for line in printed)


def test_update_download_rejects_when_busy(monkeypatch):
    printed = _capture_print(monkeypatch)
    _quiet_ui(monkeypatch)
    downloader = _FakeDownloader(downloading=True)
    monkeypatch.setattr(main, "_get_update_downloader", lambda: downloader)

    main.handle_slash_command("/update download")

    assert downloader.calls == []
    assert any("已有下载任务" in line for line in printed)


def test_update_cancel_calls_downloader(monkeypatch):
    printed = _capture_print(monkeypatch)
    _quiet_ui(monkeypatch)
    downloader = _FakeDownloader()
    monkeypatch.setattr(main, "_get_update_downloader", lambda: downloader)

    main.handle_slash_command("/update cancel")

    assert downloader.cancelled is True


def test_update_install_gates_on_turn_and_tasks(monkeypatch, tmp_path):
    printed = _capture_print(monkeypatch)
    _quiet_ui(monkeypatch)
    installer = tmp_path / "setup.exe"
    installer.write_bytes(b"x")
    launched = []
    import core.updater as upd
    monkeypatch.setattr(upd, "launch_installer", lambda path: launched.append(path) or True)

    monkeypatch.setattr(main, "is_thinking", True)
    main.handle_slash_command(f"/update install {installer} y")
    assert launched == []
    assert any("正在执行的回合" in line for line in printed)

    monkeypatch.setattr(main, "is_thinking", False)
    import core.task_manager as tm
    monkeypatch.setattr(tm, "list_active_tasks", lambda: [{"id": "t1"}])
    main.handle_slash_command(f"/update install {installer} y")
    assert launched == []
    assert any("后台任务" in line for line in printed)

    monkeypatch.setattr(tm, "list_active_tasks", lambda: [])

    class _NoopThread:
        def __init__(self, **kwargs):
            _NoopThread.last_kwargs = kwargs

        def start(self):
            pass

    _NoopThread.last_kwargs = None
    monkeypatch.setattr(main.threading, "Thread", lambda *a, **kw: _NoopThread(**kw))
    main.handle_slash_command(f"/update install {installer} y")

    assert launched == [str(installer)]
    assert _NoopThread.last_kwargs is not None
    assert any("安装向导已启动" in line for line in printed)


def test_update_install_requires_confirmation(monkeypatch, tmp_path):
    printed = _capture_print(monkeypatch)
    _quiet_ui(monkeypatch)
    installer = tmp_path / "setup.exe"
    installer.write_bytes(b"x")
    launched = []
    import core.updater as upd
    monkeypatch.setattr(upd, "launch_installer", lambda path: launched.append(path) or True)

    main.handle_slash_command(f"/update install {installer}")

    assert launched == []
    assert any("确认请输入" in line for line in printed)


class _FakeTurnUndoService:
    def __init__(self, states, details=None):
        self._states = states
        self._details = details or {}

    def list_turn_undo_states(self, session_id, limit=100):
        return self._states

    def get_turn_undo_status(self, session_id, turn_id):
        return self._details.get(turn_id)


def test_changes_lists_recent_turns_with_file_rows(monkeypatch):
    printed = _capture_print(monkeypatch)
    _quiet_ui(monkeypatch)
    monkeypatch.setattr(main, "current_session", "s1")
    monkeypatch.setattr(main, "_undo_session_id", lambda session: 1)
    service = _FakeTurnUndoService(
        states=[
            {"turn_id": "turn-111122223333", "status": "ready",
             "snapshot_availability": "available", "file_count": 2,
             "net_added": 10, "net_removed": 3},
            {"turn_id": "turn-444455556666", "status": "reverted",
             "snapshot_availability": "available", "file_count": 1,
             "net_added": 2, "net_removed": 2},
        ],
        details={
            "turn-111122223333": {"files": [
                {"display_path": "a.py", "net_added": 6, "net_removed": 2},
                {"display_path": "b.py", "net_added": 4, "net_removed": 1},
            ]},
            "turn-444455556666": {"files": [
                {"display_path": "c.py", "net_added": 2, "net_removed": 2},
            ]},
        },
    )
    monkeypatch.setattr(main, "_undo_service", lambda: service)

    main.handle_slash_command("/changes 2")

    assert any("turn-111" in line for line in printed)
    assert any("可撤销" in line for line in printed)
    assert any("a.py" in line and "+6" in line for line in printed)
    assert any("c.py" in line for line in printed)
    assert any("/undo" in line for line in printed)


def test_changes_empty_session_reports(monkeypatch):
    printed = _capture_print(monkeypatch)
    _quiet_ui(monkeypatch)
    monkeypatch.setattr(main, "current_session", "s1")
    monkeypatch.setattr(main, "_undo_session_id", lambda session: 1)
    monkeypatch.setattr(main, "_undo_service", lambda: _FakeTurnUndoService(states=[]))

    main.handle_slash_command("/changes")

    assert any("还没有文件修改记录" in line for line in printed)


def test_persona_title_dash_clears_title(monkeypatch):
    printed = _capture_print(monkeypatch)
    _quiet_ui(monkeypatch)
    calls = []

    import core.persona_settings as ps
    monkeypatch.setattr(
        ps,
        "get_persona_settings",
        lambda: {"agent_name": "星瑶", "persona": "p", "user_title": "老板"},
    )
    monkeypatch.setattr(ps, "save_persona_settings", lambda *args: calls.append(args))

    main.handle_slash_command("/persona title -")

    assert calls == [("星瑶", "p", "")]


def test_print_threadsafe_closed_loop_protection(monkeypatch):
    # Important #2：loop 已关闭时静默丢弃；call_soon_threadsafe 抛 RuntimeError 时吞掉
    printed = _capture_print(monkeypatch)
    _quiet_ui(monkeypatch)

    class _ClosedLoop:
        def is_closed(self):
            return True

        def call_soon_threadsafe(self, *args, **kwargs):
            raise AssertionError("closed loop 不应被调度")

    class _RacyLoop:
        def is_closed(self):
            return False

        def call_soon_threadsafe(self, *args, **kwargs):
            raise RuntimeError("loop closed during schedule")

    monkeypatch.setattr(main, "app_instance", type("App", (), {"loop": _ClosedLoop()})())
    main._print_threadsafe("<b>x</b>")

    monkeypatch.setattr(main, "app_instance", type("App", (), {"loop": _RacyLoop()})())
    main._print_threadsafe("<b>x</b>")


def test_queue_internal_messages_are_preserved_and_hidden(monkeypatch):
    printed = _capture_print(monkeypatch)
    _quiet_ui(monkeypatch)
    _drain_main_queue()
    internal = {"text": "__ycode_internal__:summarize_background_tasks",
                "was_queued": False, "is_internal": True, "session": "s1"}
    main.msg_queue.put({"text": "用户一", "was_queued": True, "session": "s1"})
    main.msg_queue.put(internal)
    main.msg_queue.put({"text": "用户二", "was_queued": True, "session": "s1"})

    main.handle_slash_command("/queue")
    all_items = _drain_main_queue()
    assert [i["text"] for i in all_items] == ["用户一", "__ycode_internal__:summarize_background_tasks", "用户二"]
    assert any("排队中的消息（2 条）" in line for line in printed)
    assert not any("__ycode_internal__" in line for line in printed)
    for i in all_items:
        main.msg_queue.put(i)

    printed.clear()
    main.handle_slash_command("/queue cancel 1")
    remaining = _drain_main_queue()
    assert [i["text"] for i in remaining] == ["__ycode_internal__:summarize_background_tasks", "用户二"]
    for i in remaining:
        main.msg_queue.put(i)

    printed.clear()
    main.handle_slash_command("/queue clear")
    leftover = _drain_main_queue()
    assert [i["text"] for i in leftover] == ["__ycode_internal__:summarize_background_tasks"]
    assert any("已清空 1 条" in line for line in printed)
    assert any("系统内部消息已保留" in line for line in printed)


def test_effort_missing_model_reports_not_found(monkeypatch):
    printed = _capture_print(monkeypatch)
    _quiet_ui(monkeypatch)

    import core.model_manager as mm
    monkeypatch.setattr(mm, "load_config", lambda: {"active_model_id": "ghost", "providers": []})

    main.handle_slash_command("/effort")

    assert any("未在配置中找到激活模型" in line for line in printed)


def _fake_hook_entries():
    return [
        {
            "event": "PostToolUse",
            "matcher": "*",
            "command": "echo user-level",
            "timeout_ms": 10000,
            "status_message": None,
            "source_path": "~/.ycode/hooks.json",
            "scope": "user",
            "digest": "digest-1",
            "status": "trusted",
        },
        {
            "event": "PreToolUse",
            "matcher": "Bash",
            "command": "echo project-level",
            "timeout_ms": 10000,
            "status_message": None,
            "source_path": ".ycode/hooks.json",
            "scope": "project",
            "digest": "digest-2",
            "status": "pending",
        },
    ]


def _patch_hooks(monkeypatch, entries, calls=None):
    import core.hooks.trust as hook_trust
    import core.workspace_context as wsc

    monkeypatch.setattr(wsc, "get_workdir", lambda: "E:/fixed-ws")
    monkeypatch.setattr(hook_trust, "list_hooks_with_status", lambda **kwargs: list(entries))
    if calls is not None:
        def _apply(action, digest, **kwargs):
            calls.append((action, digest, kwargs))
            return True, "已信任"

        monkeypatch.setattr(hook_trust, "apply_hook_trust_action", _apply)
    return calls


def test_hooks_list_shows_numbered_entries_with_status(monkeypatch):
    printed = _capture_print(monkeypatch)
    _quiet_ui(monkeypatch)
    _patch_hooks(monkeypatch, _fake_hook_entries())

    main.handle_slash_command("/hooks")

    assert any("[1]" in line for line in printed)
    assert any("[2]" in line for line in printed)
    assert any("PostToolUse" in line for line in printed)
    assert any("PreToolUse" in line for line in printed)
    assert any("已信任" in line for line in printed)
    assert any("待确认" in line for line in printed)


def test_hooks_trust_passes_digest_and_workspace_root(monkeypatch):
    printed = _capture_print(monkeypatch)
    _quiet_ui(monkeypatch)
    calls = _patch_hooks(monkeypatch, _fake_hook_entries(), calls=[])

    main.handle_slash_command("/hooks trust 2")

    assert calls == [("grant", "digest-2", {"workspace_root": "E:/fixed-ws"})]
    assert any("已信任" in line for line in printed)
    assert any("运行 /hooks 查看最新状态" in line for line in printed)


def test_hooks_trust_rejects_out_of_range_and_non_numeric_index(monkeypatch):
    printed = _capture_print(monkeypatch)
    _quiet_ui(monkeypatch)
    calls = _patch_hooks(monkeypatch, _fake_hook_entries(), calls=[])

    main.handle_slash_command("/hooks trust 9")
    main.handle_slash_command("/hooks trust abc")

    assert calls == []
    assert any("序号" in line and "查看编号" in line for line in printed)


def test_hooks_list_empty_reports_not_configured(monkeypatch):
    printed = _capture_print(monkeypatch)
    _quiet_ui(monkeypatch)
    _patch_hooks(monkeypatch, [])

    main.handle_slash_command("/hooks")

    assert any("未配置任何 Hook" in line for line in printed)


def test_hooks_unknown_subcommand_shows_usage(monkeypatch):
    printed = _capture_print(monkeypatch)
    _quiet_ui(monkeypatch)
    _patch_hooks(monkeypatch, _fake_hook_entries())

    main.handle_slash_command("/hooks foo")

    assert any("未知子命令" in line and "/hooks" in line for line in printed)

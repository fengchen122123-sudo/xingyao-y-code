"""回归测试：代码审查修复的关键 bug（压缩重绑定、thinking 双发、turn_error 等）。"""

import threading
from types import SimpleNamespace

import pytest

from core.llm import chat_with_model_events


def _chunk(content="", tool_calls=None, reasoning="", provider_fields=None, usage=None):
    delta = SimpleNamespace(
        content=content,
        reasoning_content=reasoning,
        tool_calls=tool_calls,
        provider_specific_fields=provider_fields,
    )
    return SimpleNamespace(
        choices=[SimpleNamespace(delta=delta)],
        usage=usage,
    )


def _usage_chunk(total, prompt):
    usage = SimpleNamespace(
        total_tokens=total,
        prompt_tokens=prompt,
        prompt_tokens_details=None,
    )
    return SimpleNamespace(choices=[], usage=usage)


def _tool_call(index, call_id, name, arguments):
    return SimpleNamespace(
        index=index,
        id=call_id,
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def test_compression_replaces_messages_in_place(mocker, monkeypatch):
    """上下文压缩后必须原地替换 messages，调用方持有的列表不失联。"""
    import main

    monkeypatch.setattr(main, "current_session", "compress-session")
    compressed = [{"role": "user", "content": "压缩后的历史"}]

    class FakeManager:
        def get_history(self, session):
            assert session == "compress-session"
            return list(compressed)

    monkeypatch.setattr(main, "manager", FakeManager())

    compress_calls = []

    def fake_compress(manager, session, api_model, api_key, api_url, force=False, output_callback=None):
        compress_calls.append((session, force))
        assert session == "compress-session"

    monkeypatch.setattr("core.memory_compressor.compress_session_memory", fake_compress)
    mocker.patch("core.llm.completion", side_effect=[
        RuntimeError("prompt is too long: context window exceeded"),
        iter([_chunk("完成")]),
    ])

    messages = [{"role": "user", "content": "原始问题"}]
    original_id = id(messages)

    events = list(chat_with_model_events(
        "fake-model",
        "fake-key",
        messages,
        persist=False,
        turn_id="turn-compress-in-place",
        consume_background_notifications=False,
    ))

    assert id(messages) == original_id
    assert messages[0] == {"role": "user", "content": "压缩后的历史"}
    assert messages[-1]["role"] == "assistant"
    assert events[-1].kind == "turn_finished"


def test_rate_limit_error_does_not_trigger_compression(mocker, monkeypatch):
    """rate limit 等错误不触发压缩路径。"""
    import main

    monkeypatch.setattr(main, "current_session", "rate-limit-session")
    compress_called = []

    class FakeManager:
        def get_history(self, session):
            return []

    monkeypatch.setattr(main, "manager", FakeManager())

    def fake_compress(manager, session, api_model, api_key, api_url, force=False, output_callback=None):
        compress_called.append(session)

    monkeypatch.setattr("core.memory_compressor.compress_session_memory", fake_compress)
    mocker.patch("time.sleep")
    mocker.patch(
        "core.llm.completion",
        side_effect=RuntimeError("rate limit exceeded for this model"),
    )

    events = list(chat_with_model_events(
        "fake-model",
        "fake-key",
        [{"role": "user", "content": "提问"}],
        persist=False,
        turn_id="turn-rate-limit",
        consume_background_notifications=False,
    ))

    assert compress_called == []
    assert "turn_error" in [event.kind for event in events]


def test_native_reasoning_emits_exactly_one_thinking_event_group(mocker):
    """原生 reasoning_content 只产生一组 thinking_started/finished 事件。"""
    mocker.patch("core.llm.completion", return_value=iter([
        _chunk(reasoning="分析过程"),
        _chunk("最终回答"),
    ]))

    events = list(chat_with_model_events(
        "fake-model",
        "fake-key",
        [{"role": "user", "content": "分析"}],
        persist=False,
        turn_id="turn-single-thinking",
    ))

    kinds = [event.kind for event in events]
    assert kinds.count("thinking_started") == 1
    assert kinds.count("thinking_finished") == 1
    thinking = "".join(
        event.payload["text"] for event in events if event.kind == "thinking_delta"
    )
    assert thinking == "分析过程"


def test_exception_emits_turn_error_without_turn_finished_and_rolls_back(mocker):
    """异常结束的回合发 turn_error 而非 turn_finished，且内存消息回滚。"""
    mocker.patch("time.sleep")
    mocker.patch(
        "core.llm.completion",
        side_effect=RuntimeError("provider exploded"),
    )
    messages = [{"role": "user", "content": "提问"}]
    original = list(messages)

    events = list(chat_with_model_events(
        "fake-model",
        "fake-key",
        messages,
        persist=False,
        turn_id="turn-error-event",
        consume_background_notifications=False,
    ))

    kinds = [event.kind for event in events]
    assert "turn_error" in kinds
    assert "turn_finished" not in kinds
    assert "error" in kinds
    assert messages == original


def test_repeated_tool_call_name_is_not_concatenated(mocker):
    """provider 重复发送 function.name 时不得拼接成坏名字。"""
    first_response = iter([
        _chunk(tool_calls=[
            _tool_call(0, "call-1", "glob_files", '{"pattern":"*.py"}'),
        ]),
        _chunk(tool_calls=[
            _tool_call(0, "call-1", "glob_files", ''),
        ]),
    ])
    final_response = iter([_chunk("完成")])
    mocker.patch(
        "core.llm.completion",
        side_effect=[first_response, final_response],
    )
    messages = [{"role": "user", "content": "检查"}]

    list(chat_with_model_events(
        "fake-model",
        "fake-key",
        messages,
        persist=False,
        turn_id="turn-tool-name-dedup",
    ))

    assistant = next(
        message for message in messages if message.get("tool_calls")
    )
    assert assistant["tool_calls"][0]["function"]["name"] == "glob_files"


def test_multiple_usage_chunks_are_counted_once(mocker):
    """同一流中多个 usage chunk 只按最终块累计一次。"""
    import core.llm as llm

    with llm.token_usage_lock:
        baseline_total = llm.session_token_usage["cumulative_total"]
        baseline_prompt = llm.session_token_usage["cumulative_prompt"]

    mocker.patch("core.llm.completion", return_value=iter([
        _usage_chunk(10, 6),
        _chunk("回答"),
        _usage_chunk(20, 12),
    ]))

    list(chat_with_model_events(
        "fake-model",
        "fake-key",
        [{"role": "user", "content": "提问"}],
        persist=False,
        turn_id="turn-usage-once",
    ))

    with llm.token_usage_lock:
        assert llm.session_token_usage["latest_total"] == 20
        assert llm.session_token_usage["cumulative_total"] == baseline_total + 20
        assert llm.session_token_usage["cumulative_prompt"] == baseline_prompt + 12


def test_cancel_rollback_preserves_drained_notifications(mocker):
    """取消回滚不丢失已 drain 的后台通知。"""
    notif = "<task-notification>后台结果</task-notification>"
    drain = mocker.patch(
        "core.tools.subagent_manager.drain_notifications",
        return_value=[notif],
    )
    cancel_event = threading.Event()

    def cancelling_stream():
        yield _chunk("部分输出")
        cancel_event.set()
        yield _chunk("被忽略")

    mocker.patch("core.llm.completion", return_value=cancelling_stream())
    messages = [{"role": "user", "content": "原始问题"}]

    try:
        list(chat_with_model_events(
            "fake-model",
            "fake-key",
            messages,
            persist=False,
            turn_id="turn-cancel-keep-notif",
            cancel_event=cancel_event,
            parent_session="session-notif",
        ))
    except KeyboardInterrupt:
        pass

    drain.assert_called_once_with("session-notif")
    assert messages == [
        {"role": "user", "content": "原始问题"},
        {"role": "user", "content": notif},
    ]


def test_reasoning_fields_stripped_for_unsupported_providers(mocker):
    """非白名单 provider 不接收 reasoning_content / thinking_blocks 回传。"""
    captured = {}

    def fake_completion(**kwargs):
        captured.update(kwargs)
        return iter([_chunk("完成")])

    mocker.patch("core.llm.completion", side_effect=fake_completion)

    list(chat_with_model_events(
        "openai/gpt-4o",
        "fake-key",
        [
            {"role": "user", "content": "提问"},
            {
                "role": "assistant",
                "content": "历史回答",
                "reasoning_content": "历史推理",
                "thinking_blocks": [
                    {"type": "thinking", "thinking": "历史推理", "signature": "sig"}
                ],
            },
            {"role": "user", "content": "继续"},
        ],
        persist=False,
        turn_id="turn-reasoning-strip",
        consume_background_notifications=False,
    ))

    assistant = next(
        message
        for message in captured["messages"]
        if message["role"] == "assistant"
    )
    assert "reasoning_content" not in assistant
    assert "thinking_blocks" not in assistant


def test_compression_uses_parent_session_not_main_session(mocker, monkeypatch):
    """子代理回合触发压缩时，压缩与取历史都必须作用于子会话而非主 UI 会话。"""
    import main

    monkeypatch.setattr(main, "current_session", "main-ui-session")
    calls = []
    compressed = [{"role": "user", "content": "压缩后的子代理历史"}]

    class FakeManager:
        def get_history(self, session):
            calls.append(("history", session))
            return list(compressed)

    monkeypatch.setattr(main, "manager", FakeManager())

    def fake_compress(manager, session, api_model, api_key, api_url, force=False, output_callback=None):
        calls.append(("compress", session))

    monkeypatch.setattr("core.memory_compressor.compress_session_memory", fake_compress)
    mocker.patch("core.llm.completion", side_effect=[
        RuntimeError("prompt is too long: context window exceeded"),
        iter([_chunk("完成")]),
    ])

    messages = [{"role": "user", "content": "子代理原始问题"}]
    events = list(chat_with_model_events(
        "fake-model",
        "fake-key",
        messages,
        persist=False,
        turn_id="turn-child-compress",
        parent_session="child-session",
        consume_background_notifications=False,
    ))

    assert calls == [
        ("compress", "child-session"),
        ("history", "child-session"),
    ]
    assert messages[0] == {"role": "user", "content": "压缩后的子代理历史"}
    assert events[-1].kind == "turn_finished"


def test_rollback_after_successful_compression_keeps_compressed_history(mocker, monkeypatch):
    """压缩成功后本轮再异常：回滚不得把内存恢复成未压缩快照（DB 已被压缩）。"""
    import main

    monkeypatch.setattr(main, "current_session", "compress-then-fail")
    compressed = [{"role": "user", "content": "压缩后的历史"}]

    class FakeManager:
        def get_history(self, session):
            return list(compressed)

    monkeypatch.setattr(main, "manager", FakeManager())

    def fake_compress(manager, session, api_model, api_key, api_url, force=False, output_callback=None):
        pass

    monkeypatch.setattr("core.memory_compressor.compress_session_memory", fake_compress)
    mocker.patch("time.sleep")
    mocker.patch("core.llm.completion", side_effect=[
        RuntimeError("prompt is too long: context window exceeded"),
        RuntimeError("provider exploded"),
    ])

    messages = [{"role": "user", "content": "原始问题"}]
    events = list(chat_with_model_events(
        "fake-model",
        "fake-key",
        messages,
        persist=False,
        turn_id="turn-compress-then-fail",
        consume_background_notifications=False,
    ))

    assert "turn_error" in [event.kind for event in events]
    # 回滚后的内存历史仍是压缩后的版本，与 DB 保持一致
    assert messages == [{"role": "user", "content": "压缩后的历史"}]


def test_edit_file_dispatch_passes_replace_all(mocker, tmp_path):
    """调度层必须把 arguments['replace_all'] 透传给 edit_file。"""
    import core.llm as llm

    target = tmp_path / "data.txt"
    target.write_text("aaa bbb aaa", encoding="utf-8")
    captured = {}

    def fake_edit_file(
        file_path,
        target_content,
        replacement_content,
        replace_all=False,
        start_line=None,
        end_line=None,
    ):
        captured["replace_all"] = replace_all
        return f"{llm.TOOL_ERROR_PREFIX} 模拟失败，跳过后续 diff 读取"

    mocker.patch.object(llm, "edit_file", fake_edit_file)
    mocker.patch.object(llm, "check_permission_and_ask", return_value=True)

    tool_call = {
        "id": "call-replace-all",
        "function": {
            "name": "edit_file",
            "arguments": (
                '{"file_path": "%s", "target_content": "aaa", '
                '"replacement_content": "ccc", "replace_all": true}'
                % str(target).replace("\\", "\\\\")
            ),
        },
    }
    llm._execute_single_tool_call(
        tool_call,
        event_callback=lambda *args, **kwargs: None,
    )

    assert captured["replace_all"] is True


def test_write_file_old_content_falls_back_to_gbk(mocker, tmp_path):
    """GBK 文件被 write_file 覆写时，旧内容读取回退 GBK，undo 快照不退化为空串。"""
    import core.llm as llm

    target = tmp_path / "gbk-file.txt"
    target.write_bytes("中文内容".encode("gbk"))
    snapshots = {}

    mocker.patch.object(llm, "check_permission_and_ask", return_value=True)
    mocker.patch("core.memory.set_undo_state", side_effect=lambda path, old: snapshots.update(path=path, old=old))

    tool_call = {
        "id": "call-gbk-write",
        "function": {
            "name": "write_file",
            "arguments": '{"file_path": "%s", "content": "new"}' % str(target).replace("\\", "\\\\"),
        },
    }
    result = llm._execute_single_tool_call(
        tool_call,
        event_callback=lambda *args, **kwargs: None,
    )

    assert snapshots["old"] == "中文内容"
    assert result["content"].startswith("已成功将内容写入文件")


def test_generator_preamble_exception_emits_turn_error(mocker):
    """生成器前置区（如记忆段构建）抛异常时补发 turn_error，不静默穿透。"""
    mocker.patch(
        "core.memory_kairos.build_memory_system_section",
        side_effect=RuntimeError("memory subsystem exploded"),
    )

    raised = False
    events = []
    try:
        for event in chat_with_model_events(
            "fake-model",
            "fake-key",
            [{"role": "user", "content": "提问"}],
            persist=False,
            turn_id="turn-preamble-error",
            consume_background_notifications=False,
        ):
            events.append(event)
    except RuntimeError:
        raised = True

    assert raised is True
    kinds = [event.kind for event in events]
    assert "turn_error" in kinds
    assert "turn_finished" not in kinds


def test_final_summary_retry_removes_empty_assistant_message(mocker):
    """needs_final_summary 重试时，空 assistant 消息不留内存（避免下轮 API 400）。"""
    responses = [
        iter([_chunk(tool_calls=[
            _tool_call(0, "call-1", "glob_files", '{"pattern":"*.py"}'),
        ])]),
        iter([_chunk("...")]),  # 无意义内容触发 final summary 重试
        iter([_chunk("检查结论：一切正常。")]),
    ]
    mocker.patch("core.llm.completion", side_effect=responses)
    messages = [{"role": "user", "content": "检查"}]

    list(chat_with_model_events(
        "fake-model",
        "fake-key",
        messages,
        persist=False,
        turn_id="turn-final-summary-cleanup",
        consume_background_notifications=False,
    ))

    # 内存中不存在 content 为 None/空 且无 tool_calls 的悬空 assistant 消息
    for message in messages:
        if message["role"] == "assistant" and not message.get("tool_calls"):
            assert message.get("content")


# ── 沙盒外访问询问（_sandbox_outside_guard）──

def test_sandbox_guard_passes_through_for_inside_path(tmp_path, monkeypatch):
    """沙盒内路径不触发询问，直接放行"""
    import core.llm as llm_mod

    monkeypatch.chdir(tmp_path)
    asked = []
    monkeypatch.setattr(
        llm_mod, "check_permission_and_ask",
        lambda *a, **kw: asked.append(1) or False,
    )

    cm, err = llm_mod._sandbox_outside_guard(
        "read_file", str(tmp_path / "inside.txt"),
        None, "owner", "turn", "主代理", None,
    )
    assert cm is None and err is None
    assert asked == [], "沙盒内路径不应触发权限询问"


def test_sandbox_guard_asks_and_denies_outside_path(tmp_path, monkeypatch):
    """沙盒外路径 + 用户拒绝：返回拒绝文案"""
    import core.llm as llm_mod
    import core.tools as tools_mod

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    monkeypatch.setattr(tools_mod, "_outside_workspace_allowed", lambda: False)
    monkeypatch.setattr(llm_mod, "check_permission_and_ask", lambda *a, **kw: False)

    cm, err = llm_mod._sandbox_outside_guard(
        "read_file", str(tmp_path / "outside.txt"),
        None, "owner", "turn", "主代理", None,
    )
    assert cm is None
    assert err is not None and "拒绝" in err


def test_sandbox_guard_approved_bypass_allows_read(tmp_path, monkeypatch):
    """沙盒外路径 + 用户同意本次：返回 bypass 上下文，其中可读取沙盒外文件"""
    import core.llm as llm_mod
    import core.tools as tools_mod

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "note.txt").write_text("外部内容", encoding="utf-8")
    monkeypatch.chdir(workspace)

    # 仅 bypass 上下文内放行（配置关闭、会话未放行）
    monkeypatch.setattr("core.model_manager.load_config", lambda: {})
    tools_mod.set_session_sandbox_bypass(False)
    monkeypatch.setattr(llm_mod, "check_permission_and_ask", lambda *a, **kw: True)

    cm, err = llm_mod._sandbox_outside_guard(
        "read_file", str(outside / "note.txt"),
        None, "owner", "turn", "主代理", None,
    )
    assert err is None and cm is not None
    with cm:
        assert tools_mod.read_file(str(outside / "note.txt")) == "外部内容"
    # 离开上下文后沙盒恢复拒绝
    import pytest as _pytest
    with _pytest.raises(ValueError, match="路径越界"):
        tools_mod.read_file(str(outside / "note.txt"))


def test_session_sandbox_bypass_flag(monkeypatch):
    """始终放行：会话级标志置位后 _outside_workspace_allowed 为 True"""
    import core.tools as tools_mod

    monkeypatch.setattr("core.model_manager.load_config", lambda: {})
    tools_mod.set_session_sandbox_bypass(False)
    assert tools_mod._outside_workspace_allowed() is False
    tools_mod.set_session_sandbox_bypass(True)
    try:
        assert tools_mod._outside_workspace_allowed() is True
    finally:
        tools_mod.set_session_sandbox_bypass(False)


def test_imom_active_publish_root_reads_store(tmp_path, monkeypatch):
    """回归：_get_imom_active_publish_root 必须能读出活跃项目的 publish_root

    历史上函数内重复 json.load(f) 导致第二次读取 EOF 必抛异常被静默吞掉，
    函数永远返回空串，使 imom_* 工具的沙盒守卫完全失效。
    """
    import json as _json
    import os as _os

    import core.llm as llm_mod

    store_dir = tmp_path / ".ycode"
    store_dir.mkdir()
    (store_dir / "imom_projects.json").write_text(
        _json.dumps({
            "active": "proj_a",
            "projects": {"proj_a": {"publish_root": "/data/publish"}},
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(_os.path, "expanduser", lambda p: str(tmp_path) if p == "~" else p)

    assert llm_mod._get_imom_active_publish_root() == "/data/publish"


def test_plan_mode_approved_still_asks_sandbox_outside_access(monkeypatch):
    """回归：plan 模式计划已批准时，sandbox_outside_access 不得自动放行"""
    import core.llm as llm_mod

    monkeypatch.setattr(llm_mod.app_context, "get_permission_mode", lambda: "plan")
    monkeypatch.setattr(llm_mod.app_context, "get_plan_approved", lambda: True)
    # 用权限回调充当“询问了用户”的探针：走到询问路径会调用它
    monkeypatch.setattr(
        llm_mod.app_context, "get_permission_callback", lambda: (lambda *a, **kw: "ASKED")
    )

    # sandbox_outside_access 必须继续询问，不能被计划批准自动放行
    assert llm_mod.check_permission_and_ask(
        "sandbox_outside_access", {"path": "/outside", "operation": "write_file"}
    ) == "ASKED"
    # 对照：普通写操作在计划批准后仍自动放行
    assert llm_mod.check_permission_and_ask(
        "write_file", {"file_path": "a.txt"}
    ) is True


# ── 多模态图片透传（_strip_media_and_merge / _supports_vision）──

def _image_msg(text="看图"):
    return {
        "role": "user",
        "content": [
            {"type": "text", "text": text},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAABBBB"}},
        ],
    }


def test_vision_model_keeps_latest_image_and_strips_older(monkeypatch):
    """支持视觉的模型：最近一条图片原样透传，更早的历史图片折叠为占位符"""
    import core.llm as llm_mod

    monkeypatch.setattr(llm_mod, "_supports_vision", lambda model: True)
    messages = [
        {"role": "system", "content": "sys"},
        _image_msg("第一张"),
        {"role": "assistant", "content": "好的"},
        _image_msg("第二张"),
    ]
    out = llm_mod._strip_media_and_merge(messages, "openai/vision-model")[0]

    # 较早的图片被折叠，base64 不外泄
    assert isinstance(out[1]["content"], str)
    assert "AAAABBBB" not in out[1]["content"]
    assert "[图片]" in out[1]["content"]
    # 最近一条图片完整保留多模态结构
    assert isinstance(out[3]["content"], list)
    assert out[3]["content"][1]["type"] == "image_url"
    assert "AAAABBBB" in out[3]["content"][1]["image_url"]["url"]


def test_non_vision_model_flattens_image_without_base64_leak(monkeypatch):
    """不支持视觉的模型：图片折叠为占位符，base64 绝不作为文本发送"""
    import core.llm as llm_mod

    monkeypatch.setattr(llm_mod, "_supports_vision", lambda model: False)
    out = llm_mod._strip_media_and_merge([_image_msg()], "openai/text-model")[0]
    assert isinstance(out[0]["content"], str)
    assert "AAAABBBB" not in out[0]["content"]
    assert "[图片]" in out[0]["content"]


def test_media_list_content_not_merged_with_text(monkeypatch):
    """多模态 list 内容不参与同 role 文本合并；只有最新用户消息中的图片被保留"""
    import core.llm as llm_mod

    monkeypatch.setattr(llm_mod, "_supports_vision", lambda model: True)
    out = llm_mod._strip_media_and_merge(
        [{"role": "user", "content": "补充"}, _image_msg("问题")], "openai/vision-model"
    )[0]
    assert len(out) == 2
    assert out[0]["content"] == "补充"
    assert isinstance(out[1]["content"], list)


def test_supports_vision_config_flag_takes_precedence(monkeypatch):
    """config.json 模型条目中的 supports_vision 显式标注优先于其它判定"""
    import core.llm as llm_mod

    monkeypatch.setattr(
        "core.model_manager.load_config",
        lambda: {"providers": [{"models": [
            {"litellm_model": "openai/m1", "supports_vision": False},
            {"litellm_model": "openai/m2", "supports_vision": True},
        ]}]},
    )
    # 即使协议兜底会认为 openai/ 支持视觉，显式 False 也生效
    assert llm_mod._supports_vision("openai/m1") is False
    assert llm_mod._supports_vision("openai/m2") is True


def test_supports_vision_unknown_default_false(monkeypatch):
    """未知模型默认不支持视觉，避免把 image_url 发给纯文本模型触发 400"""
    import core.llm as llm_mod

    monkeypatch.setattr("core.model_manager.load_config", lambda: {"providers": []})
    monkeypatch.setattr(
        llm_mod.litellm, "supports_vision",
        lambda model: False,
        raising=False,
    )
    assert llm_mod._supports_vision("openai/step-3.7-flash") is False
    assert llm_mod._supports_vision("anthropic/K3") is False
    assert llm_mod._supports_vision("deepseek/deepseek-chat") is False
    assert llm_mod._supports_vision(None) is False


# ── 只思考不行动的促升兜底（P0 回归）──


def test_reasoning_only_turn_is_nudged_to_act(mocker):
    """模型只输出 reasoning、不调工具也不给正文：先注入催促消息让它真正行动，
    而不是把整段内心独白当成本轮回合的交付结果。"""
    responses = [
        iter([_chunk(reasoning="让我先精确定位需要修改的 SQL，然后做修改。")]),
        iter([_chunk("检查结论：已定位问题并完成修改。")]),
    ]
    mocker.patch("core.llm.completion", side_effect=responses)
    messages = [{"role": "user", "content": "使用方案A修改一下"}]

    list(chat_with_model_events(
        "fake-model",
        "fake-key",
        messages,
        persist=False,
        turn_id="turn-reasoning-only-nudge",
        consume_background_notifications=False,
    ))

    # 催促消息已注入对话，要求模型调用工具或给出结论
    assert any(
        m["role"] == "user" and "只输出了思考过程" in str(m.get("content", ""))
        for m in messages
    )
    # 最终留下的 assistant 正文是结论，而不是思考原文
    final = [m for m in messages if m["role"] == "assistant" and m.get("content")]
    assert final and "检查结论" in final[-1]["content"]
    assert "让我先精确定位" not in final[-1]["content"]


def test_reasoning_only_promotion_is_truncated(mocker):
    """催促重试后模型仍只输出思考：促升为正文时必须截断，
    不能把数万字内心独白全量倾倒给用户。"""
    long_reasoning = "分析过程。" * 2000  # 远超预览上限
    responses = [
        iter([_chunk(reasoning=long_reasoning)]),
        iter([_chunk(reasoning=long_reasoning)]),  # 重试后仍只思考
    ]
    mocker.patch("core.llm.completion", side_effect=responses)
    messages = [{"role": "user", "content": "使用方案A修改一下"}]

    list(chat_with_model_events(
        "fake-model",
        "fake-key",
        messages,
        persist=False,
        turn_id="turn-reasoning-only-truncated",
        consume_background_notifications=False,
    ))

    final = [m for m in messages if m["role"] == "assistant" and m.get("content")]
    assert final, "促升兜底仍应留下一条可见回复"
    content = final[-1]["content"]
    assert content.rstrip().endswith("[以上为模型思考过程，未生成正式回复]")
    assert "已截断" in content
    assert len(content) < 1000


def _chunk_with_finish(content="", reasoning="", finish_reason=None):
    delta = SimpleNamespace(
        content=content,
        reasoning_content=reasoning,
        tool_calls=None,
        provider_specific_fields=None,
    )
    return SimpleNamespace(
        choices=[SimpleNamespace(delta=delta, finish_reason=finish_reason)],
        usage=None,
    )


def test_reasoning_fields_dropped_for_messages_without_tool_calls(mocker):
    """白名单 provider 下：无 tool_calls 的 assistant 消息不回传 reasoning_content /
    thinking_blocks（死思考重复上传纯属浪费）；带 tool_calls 的消息仍保留。"""
    captured = {}

    def fake_completion(**kwargs):
        captured.update(kwargs)
        return iter([_chunk("完成")])

    mocker.patch("core.llm.completion", side_effect=fake_completion)

    list(chat_with_model_events(
        "deepseek/deepseek-reasoner",
        "fake-key",
        [
            {"role": "user", "content": "提问"},
            {
                "role": "assistant",
                "content": "促升的思考预览……[以上为模型思考过程，未生成正式回复]",
                "reasoning_content": "废推理" * 5000,
                "thinking_blocks": [
                    {"type": "thinking", "thinking": "废推理", "signature": "sig"}
                ],
            },
            {
                "role": "assistant",
                "content": "工具轮",
                "reasoning_content": "签名链推理",
                "tool_calls": [{
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": "{}"},
                }],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "结果"},
            {"role": "user", "content": "继续"},
        ],
        persist=False,
        turn_id="turn-dead-reasoning-strip",
        consume_background_notifications=False,
    ))

    assistants = [m for m in captured["messages"] if m["role"] == "assistant"]
    no_tool = next(m for m in assistants if not m.get("tool_calls"))
    with_tool = next(m for m in assistants if m.get("tool_calls"))
    assert "reasoning_content" not in no_tool
    assert "thinking_blocks" not in no_tool
    assert with_tool["reasoning_content"] == "签名链推理"


def test_length_truncated_retry_bumps_max_tokens(mocker):
    """思考被 max_tokens 截断（finish_reason=length）导致无正文时，
    催促重试必须翻倍输出上限并要求压缩思考，否则同参数重试必然同样失败。"""
    captured = []
    responses = [
        iter([_chunk_with_finish(reasoning="长思考" * 100, finish_reason="length")]),
        iter([_chunk_with_finish(content="最终结论", finish_reason="stop")]),
    ]

    def fake_completion(**kwargs):
        captured.append(dict(kwargs))
        return responses.pop(0)

    mocker.patch("core.llm.completion", side_effect=fake_completion)
    messages = [{"role": "user", "content": "排查问题"}]

    list(chat_with_model_events(
        "fake-model",
        "fake-key",
        messages,
        persist=False,
        turn_id="turn-length-retry-bump",
        consume_background_notifications=False,
    ))

    assert len(captured) == 2
    first_max = captured[0].get("max_tokens") or captured[0].get("max_completion_tokens")
    second_max = captured[1].get("max_tokens") or captured[1].get("max_completion_tokens")
    assert second_max == min(first_max * 2, 32768)
    # 催促消息应包含压缩思考的提示
    assert any(
        m["role"] == "user" and "输出长度限制被截断" in str(m.get("content", ""))
        for m in messages
    )

import pytest

from core.tool_presenter import (
    build_tool_display,
    sanitize_tool_display_text,
    summarize_tool_finished,
    summarize_tool_started,
)


def test_display_text_redacts_cookie_and_secret_header_and_assignment_forms():
    cleaned = sanitize_tool_display_text(
        "cookie=browser-secret "
        "client_secret='client-secret' secret=plain-secret"
    )

    assert "browser-secret" not in cleaned
    assert "client-secret" not in cleaned
    assert "plain-secret" not in cleaned
    assert "cookie=***" in cleaned
    assert "client_secret=***" in cleaned
    assert "secret=***" in cleaned


def test_display_text_redacts_every_value_in_a_cookie_header():
    cleaned = sanitize_tool_display_text(
        "Cookie: first=first-secret; second=second-secret; theme=dark"
    )

    assert "first-secret" not in cleaned
    assert "second-secret" not in cleaned
    assert "theme=dark" not in cleaned
    assert cleaned == "Cookie: ***"


def test_unknown_tool_action_redacts_embedded_secret():
    display = build_tool_display("custom token=tool-secret", {}, ok=True)

    assert "tool-secret" not in display["action"]
    assert display["action"] == "使用工具 custom token=***"


@pytest.mark.parametrize("result", ["invalid", [], 1])
def test_build_tool_display_rejects_non_dict_result(result):
    with pytest.raises(TypeError, match="result must be a dict or None"):
        build_tool_display("read_file", {"file_path": "core/llm.py"}, result, ok=True)


def test_build_tool_display_accepts_none_result():
    display = build_tool_display("read_file", {"file_path": "core/llm.py"}, None, ok=True)

    assert display["metrics"] == {}


def test_build_tool_display_returns_read_metadata():
    display = build_tool_display(
        "read_file",
        {"file_path": "core/llm.py"},
        {"content": "one\ntwo\nthree"},
        ok=True,
    )

    assert display == {
        "category": "read",
        "action": "读取文件",
        "target": "core/llm.py",
        "command": "",
        "metrics": {"line_count": 3},
        "error_summary": "",
        "is_skill": False,
    }


def test_build_tool_display_marks_skill_read_separately():
    display = build_tool_display(
        "read_file",
        {"file_path": r"C:\Users\user\.agents\skills\writing-plans\SKILL.md"},
        {"content": "skill"},
        ok=True,
    )

    assert display["category"] == "skill"
    assert display["target"] == "writing-plans"
    assert display["is_skill"] is True


def test_display_text_removes_controls_truncates_and_redacts():
    value = (
        "\x1b[31mAuthorization: Bearer abc.def.secret\x1b[0m\n"
        'api_key="sk-1234567890abcdef"'
    )

    cleaned = sanitize_tool_display_text(value, limit=80)

    assert "abc.def.secret" not in cleaned
    assert "sk-1234567890abcdef" not in cleaned
    assert "Authorization: ***" in cleaned
    assert "api_key=***" in cleaned
    assert "\x1b" not in cleaned


def test_failed_command_keeps_safe_command_and_error_summary():
    display = build_tool_display(
        "execute_command",
        {"command": "pytest -q"},
        {"content": "🚨 命令执行失败（退出码 1）。\nModuleNotFoundError: pytest"},
        ok=False,
    )

    assert display["category"] == "command"
    assert display["command"] == "pytest -q"
    assert display["error_summary"] == "命令执行失败（退出码 1）。 ModuleNotFoundError: pytest"


def test_read_file_uses_star_yao_copy_without_source_content():
    started = summarize_tool_started("read_file", {"file_path": "core/llm.py"})
    finished = summarize_tool_finished(
        "read_file",
        {"file_path": "core/llm.py"},
        {"content": "line one\nline two\nline three"},
        ok=True,
    )

    assert started == "星瑶正在查看文件：core/llm.py"
    assert finished == "星瑶已查看文件：core/llm.py（3 行）"
    assert "line one" not in finished


def test_skill_file_uses_skill_activity_label():
    started = summarize_tool_started(
        "read_file",
        {"file_path": r"C:\Users\user\.agents\skills\writing-plans\SKILL.md"},
    )

    assert started == "星瑶正在使用skill：writing-plans"


def test_write_edit_and_command_copy_is_stable():
    assert summarize_tool_started(
        "write_file", {"file_path": "a.py"}
    ) == "星瑶正在写入文件：a.py"
    assert summarize_tool_started(
        "edit_file", {"file_path": "a.py"}
    ) == "星瑶正在修改文件：a.py"
    assert summarize_tool_started(
        "execute_command", {"command": "pytest -q"}
    ) == "星瑶正在执行命令：pytest -q"
    assert summarize_tool_started(
        "fetch_url", {"url": "https://wttr.in/?format=j1"}
    ) == "星瑶正在访问网址：https://wttr.in/?format=j1"
    assert summarize_tool_started(
        "get_weather", {"city": "北京"}
    ) == "星瑶正在查询天气：北京"


def test_failed_tool_uses_neutral_copy_without_raw_error():
    # 修复后：显示真实的错误信息，而非通用的"命令暂未完成"，
    # 让用户/LLM 知道真正的原因以便修正
    text = summarize_tool_finished(
        "execute_command",
        {"command": "pytest -q"},
        {"content": "exit 1"},
        ok=False,
    )

    assert text == "pytest -q — exit 1"


def test_failed_summary_strips_error_prefix_and_shows_command():
    # 🚨 哨兵前缀只做程序化失败判定，不应出现在展示文案里；
    # 命令本身必须可见，否则用户不知道哪条命令失败了
    text = summarize_tool_finished(
        "execute_command",
        {"command": "pytest -q"},
        {"content": "🚨 命令执行失败（退出码 1）。\n（命令没有任何输出）"},
        ok=False,
    )

    assert not text.startswith("🚨")
    assert "pytest -q" in text
    assert "退出码 1" in text
    # 不能出现“命令执行失败：命令执行失败”式的重复
    assert text.count("命令执行失败") == 1


def test_failed_summary_dedupes_target_and_keeps_guidance():
    # 错误文本里已包含目标路径时不重复拼接；
    # 截断上限必须保留最关键的指导意见（"请使用 read_file 工具"）
    content = (
        "🚨 目录扫描失败：路径不存在 'D:\\proj\\missing_dir'，"
        "请检查路径是否正确，或先用 glob_files 查找目标。"
    )
    text = summarize_tool_finished(
        "list_directory",
        {"dir_path": "D:\\proj\\missing_dir"},
        {"content": content},
        ok=False,
    )

    assert not text.startswith("🚨")
    assert text.count("D:\\proj\\missing_dir") == 1
    assert "glob_files" in text


def test_subagent_copy_distinguishes_foreground_and_background():
    arguments = {"task_description": "查询天气"}

    assert summarize_tool_started("run_subagent", arguments) == "星瑶正在调度子代理：查询天气"
    assert summarize_tool_finished(
        "run_subagent",
        arguments,
        {"content": "天气晴"},
        ok=True,
    ) == "子代理已完成：查询天气"
    assert summarize_tool_finished(
        "run_subagent",
        arguments,
        {
            "content": (
                '{"task_id":"subagent-1234","status":"running",'
                '"run_in_background":true}'
            )
        },
        ok=True,
    ) == "子代理已在后台启动：查询天气"


def test_subagent_copy_exposes_terminal_failures():
    arguments = {"task_description": "查询时间"}

    assert summarize_tool_finished(
        "run_subagent",
        arguments,
        {"content": '{"status":"timed_out","error":"foreground timeout"}'},
        ok=False,
    ) == "子代理执行超时：查询时间"
    assert summarize_tool_finished(
        "run_subagent",
        arguments,
        {"content": '{"status":"cancelled","error":"parent turn cancelled"}'},
        ok=False,
    ) == "子代理已取消：查询时间"
    assert summarize_tool_finished(
        "run_subagent",
        arguments,
        {"content": '{"status":"failed","error":"boom"}'},
        ok=False,
    ) == "子代理执行失败：查询时间"
    assert summarize_tool_finished(
        "run_subagent",
        arguments,
        {"content": '{"status":"blocked","error":"计划项尚未就绪"}'},
        ok=False,
    ) == "子代理等待计划就绪：查询时间"


def test_subagent_activity_compacts_long_multiline_descriptions():
    description = (
        "查询北京当前天气。\n"
        "使用 execute_command 获取实时天气，并返回温度、湿度、风力和天气状况；"
        "不要输出工具原始内容。"
    )

    text = summarize_tool_started(
        "run_subagent",
        {"task_description": description},
    )

    assert "\n" not in text
    assert text.startswith("星瑶正在调度子代理：查询北京当前天气。")
    assert text.endswith("...")
    assert len(text.removeprefix("星瑶正在调度子代理：")) <= 48

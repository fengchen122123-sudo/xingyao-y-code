from core.tool_presenter import summarize_tool_finished, summarize_tool_started


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

    assert text == "命令执行失败：exit 1"


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

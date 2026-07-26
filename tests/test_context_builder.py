from pathlib import Path

from core.context_builder import RuntimeContext, build_system_prompt


def test_context_builder_injects_runtime_layers_once():
    runtime = RuntimeContext(
        model_name="fake-model",
        permission_mode="confirm",
        working_dir="E:/project",
        current_time="2026-07-18 12:00:00",
        active_plan="检查测试",
        memory="用户偏好小步修改",
        skills="无",
    )

    prompt = build_system_prompt("基础规则", runtime)

    assert prompt.count("fake-model") == 1
    assert prompt.count("E:/project") == 1
    assert "当前权限模式：confirm" in prompt
    assert "当前计划：检查测试" in prompt
    assert "用户偏好小步修改" in prompt


def test_context_builder_adds_decisive_action_and_lazy_skill_policy():
    runtime = RuntimeContext(
        model_name="fake-model",
        permission_mode="confirm",
        working_dir="E:/project",
        current_time="2026-07-18 12:00:00",
        skills="- demo: Demo skill | E:/skills/demo/SKILL.md",
    )

    prompt = build_system_prompt("基础规则", runtime)

    assert "明确且简单的任务只做一次判断，然后立即执行" in prompt
    assert "没有新证据时，不得反复重新讨论已经排除的方案" in prompt
    assert "只读取与当前任务直接匹配的一个或少量 SKILL.md" in prompt


def test_context_builder_always_selects_dedicated_network_tools():
    runtime = RuntimeContext(
        model_name="fake-model",
        permission_mode="confirm",
        working_dir="E:/project",
        current_time="2026-07-18 12:00:00",
    )

    prompt = build_system_prompt("旧版用户提示词", runtime)

    assert "实时天气时使用 get_weather" in prompt
    assert "读取网页或 JSON 时使用 fetch_url" in prompt
    assert "search_news 仅用于新闻" in prompt


def test_main_uses_metadata_only_skill_catalog():
    # 轮次上下文组装已下沉到 core/agent_service.py（TUI 与 GUI 共用），
    # 技能目录必须仍然是"仅元数据"版本，不能整篇注入技能正文。
    main_source = Path("main.py").read_text(encoding="utf-8")
    agent_service_source = Path("core/agent_service.py").read_text(encoding="utf-8")

    assert "get_active_skills_catalog" in agent_service_source
    assert "get_active_skills_markdown" not in agent_service_source
    assert "get_active_skills_markdown" not in main_source


def test_stable_prompt_does_not_require_private_reasoning_or_infinite_retry():
    prompt = open("system_prompt.md", encoding="utf-8").read()

    assert "思考过程用 <think>" not in prompt
    assert "绝对不要停下" not in prompt
    assert "必须立刻主动调用 `execute_command`" not in prompt


def test_default_prompt_allows_terminal_markdown_without_forcing_reasoning():
    prompt = Path("system_prompt.md").read_text(encoding="utf-8")

    assert "支持标准 Markdown" in prompt
    assert "简单表格" in prompt
    assert "不要求模型伪造思考内容" in prompt


def test_default_prompt_defines_foreground_subagent_contract():
    prompt = Path("system_prompt.md").read_text(encoding="utf-8")

    assert "一次性发出多个`run_subagent`调用" in prompt
    assert "当前回答依赖子Agent返回结果时使用默认前台模式" in prompt
    assert "只有任务可独立继续、且当前决策不需要依赖其结果时" in prompt
    assert "由主Agent在同一轮统一汇总输出" in prompt
    assert "子Agent不能启动其他子Agent" in prompt


def test_default_prompt_separates_user_input_from_executable_plan_items():
    prompt = Path("system_prompt.md").read_text(encoding="utf-8")
    runtime_prompt = build_system_prompt(
        "",
        RuntimeContext(
            model_name="fake-model",
            permission_mode="confirm",
            working_dir="E:/project",
            current_time="2026-07-18 12:00:00",
        ),
    )

    for text in (prompt, runtime_prompt):
        assert "`ask_user` 的 options 只能提供 1 到 3 个选项" in text
    for text in (prompt, runtime_prompt):
        assert "用户选择必须单独建立准备项" in text
        assert "kind=input_preparation" in text


def test_default_prompt_defines_http_and_weather_tool_selection():
    prompt = Path("system_prompt.md").read_text(encoding="utf-8")

    assert "get_weather" in prompt
    assert "fetch_url" in prompt
    assert "实时天气" in prompt
    assert "`search_news`仅用于新闻" in prompt


def test_source_and_package_versions_match():
    import re
    from core.version import __version__ as core_version

    version_source = Path("core/version.py").read_text(encoding="utf-8")
    setup_source = Path("setup.py").read_text(encoding="utf-8")
    main_source = Path("main.py").read_text(encoding="utf-8")

    # core/version.py 中的版本号应与导入值一致
    file_version = re.search(
        r'^__version__\s*=\s*"(\d+\.\d+\.\d+)"',
        version_source,
        re.MULTILINE,
    ).group(1)
    assert file_version == core_version

    # setup.py 通过 _get_version() 从 core/version.py 读取版本
    assert "version=_get_version()" in setup_source.replace(" ", "")
    assert "core/version.py" in setup_source

    # main.py 使用 core.version 中的版本
    assert "from core.version import __version__ as YCODE_VERSION" in main_source
    assert 'f"v{YCODE_VERSION}"' in main_source

    # 版本号格式合法
    assert re.fullmatch(r"\d+\.\d+\.\d+", core_version)

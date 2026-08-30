from core.tool_registry import build_default_registry


def test_default_registry_exposes_read_and_mutating_capabilities():
    registry = build_default_registry()

    assert registry.get("read_file").read_only is True
    assert registry.get("read_file").requires_permission is False
    assert registry.get("write_file").mutates_files is True
    assert registry.get("write_file").requires_permission is True


def test_registry_marks_reads_and_subagents_parallel_safe():
    registry = build_default_registry()

    assert registry.get("read_file").parallel_safe is True
    assert registry.get("glob_files").parallel_safe is True
    assert registry.get("run_subagent").parallel_safe is True
    assert registry.get("run_subagent").requires_permission is False
    assert registry.get("write_file").parallel_safe is False


def test_registry_exposes_fetch_url_and_weather_as_read_only_network_tools():
    registry = build_default_registry()

    for name in ("fetch_url", "get_weather"):
        spec = registry.get(name)
        assert spec.read_only is True
        assert spec.requires_permission is False
        assert spec.parallel_safe is True


def test_subagent_schema_defaults_to_foreground_and_hides_timeout():
    schema = build_default_registry().get("run_subagent").api_schema()
    properties = schema["function"]["parameters"]["properties"]

    assert properties["run_in_background"]["default"] is False
    # 时间预算由系统内部管理（Task 8）：模型可见 schema 不再暴露
    # timeout_seconds；内部默认值仍为 explore 240 / review 300 /
    # test 480 / implement、general 600，前台硬上限 600 秒
    assert "timeout_seconds" not in properties


def test_subagent_schema_exposes_role_and_plan_item():
    properties = build_default_registry().get("run_subagent").api_schema()["function"]["parameters"]["properties"]

    assert properties["subagent_type"]["default"] == "general"
    # subagent_type 不再限制 enum，允许传入 ~/.ycode/agents/*.md 中定义的自定义子 agent name
    assert "enum" not in properties["subagent_type"]
    assert "description" in properties["subagent_type"]
    assert "plan_item_id" in properties
    assert properties["plan_item_id"]["default"] == ""


def test_registry_generates_existing_function_names():
    registry = build_default_registry()
    names = {
        schema["function"]["name"]
        for schema in registry.api_schemas()
    }

    assert {"read_file", "write_file", "edit_file", "execute_command"} <= names
    assert "glob_files" in names


def test_unknown_tool_returns_error_result():
    registry = build_default_registry()

    result = registry.execute("missing_tool")

    assert result.is_error is True
    assert "missing_tool" in result.content


def test_registry_exposes_structured_plan_tools():
    from core.plan import get_default_plan_manager

    plan = get_default_plan_manager()
    plan.clear()

    registry = build_default_registry()
    result = registry.execute(
        "plan_write",
        items=[{"id": "1", "subject": "检查代码", "status": "pending"}],
    )

    assert result.is_error is False
    assert plan.snapshot()[0]["subject"] == "检查代码"
    plan.clear()


def test_plan_schema_exposes_orchestration_fields():
    registry = build_default_registry()
    write_properties = registry.get("plan_write").api_schema()["function"]["parameters"]["properties"]
    item_properties = write_properties["items"]["items"]["properties"]
    assert {"depends_on", "owner", "files", "verification", "verification_type", "stage", "kind"} <= set(item_properties)

    update_properties = registry.get("plan_update").api_schema()["function"]["parameters"]["properties"]
    assert {"depends_on", "owner", "files", "verification", "verification_type", "stage", "kind"} <= set(update_properties)


def test_plan_retry_schema_exposes_optional_explicit_force():
    parameters = build_default_registry().get("plan_retry").api_schema()["function"]["parameters"]

    assert parameters["required"] == ["item_id"]
    assert parameters["properties"]["force"]["type"] == "boolean"


def test_plan_resume_takeover_schema_only_exposes_item_id():
    parameters = (
        build_default_registry()
        .get("plan_resume_takeover")
        .api_schema()["function"]["parameters"]
    )

    assert parameters["required"] == ["item_id"]
    assert set(parameters["properties"]) == {"item_id"}


def test_weather_schema_requires_a_city_instead_of_guessing():
    schema = build_default_registry().get("get_weather").api_schema()["function"]["parameters"]

    assert schema["required"] == ["city"]


def test_registry_exposes_ask_user_without_permission_gate():
    registry = build_default_registry()

    spec = registry.get("ask_user")

    assert spec.requires_permission is False
    assert spec.read_only is False
    properties = spec.api_schema()["function"]["parameters"]["properties"]
    assert properties["plan_item_id"]["default"] == ""
    assert properties["options"]["minItems"] == 1
    assert properties["options"]["maxItems"] == 3


def test_execute_command_description_warns_about_powershell_quotes():
    registry = build_default_registry()
    spec = registry.get("execute_command")

    assert "PowerShell" in spec.description
    assert "$env:" in spec.description
    assert '双引号' in spec.description


def test_manage_task_only_prompts_for_kill():
    registry = build_default_registry()

    # list/status 只读操作不在 spec 层面弹确认（kill 由工具内部单独请求权限）
    assert registry.get("manage_task").requires_permission is False


def test_execute_command_schema_exposes_configurable_timeout():
    registry = build_default_registry()
    properties = registry.get("execute_command").api_schema()["function"]["parameters"]["properties"]

    assert properties["timeout_seconds"]["default"] == 30
    assert properties["timeout_seconds"]["maximum"] == 600


def test_read_file_schema_exposes_paging_and_edit_file_exposes_replace_all():
    registry = build_default_registry()

    read_properties = registry.get("read_file").api_schema()["function"]["parameters"]["properties"]
    assert "offset" in read_properties
    assert "limit" in read_properties

    edit_properties = registry.get("edit_file").api_schema()["function"]["parameters"]["properties"]
    assert edit_properties["replace_all"]["default"] is False


def test_edit_file_choice_constraint_is_moonshot_compatible():
    """函数参数根节点保持 object，选择约束不得直接把 anyOf 放在根节点。"""
    registry = build_default_registry()
    parameters = registry.get("edit_file").api_schema()["function"]["parameters"]

    assert parameters["type"] == "object"
    assert "anyOf" not in parameters
    choices = parameters["allOf"][0]["anyOf"]
    assert [choice["type"] for choice in choices] == ["object", "object"]
    assert [choice["required"] for choice in choices] == [
        ["target_content"],
        ["start_line", "end_line"],
    ]


def test_manage_task_schema_exposes_wait_parameters():
    registry = build_default_registry()
    spec = registry.get("manage_task")

    assert spec.requires_permission is False
    parameters = spec.api_schema()["function"]["parameters"]
    assert parameters["required"] == ["action"]
    properties = parameters["properties"]
    assert properties["action"]["enum"] == ["list", "status", "wait", "kill"]
    assert properties["after_revision"]["type"] == "integer"
    assert properties["after_revision"]["minimum"] == 0
    assert properties["timeout_seconds"]["type"] == "integer"
    assert properties["timeout_seconds"]["minimum"] == 1
    assert properties["timeout_seconds"]["maximum"] == 60
    assert properties["timeout_seconds"]["default"] == 30


def test_run_subagent_schema_does_not_expose_timeout_seconds():
    """模型可见 schema 不再暴露 timeout_seconds：时间预算由系统内部管理。"""
    from core.tool_registry import build_default_registry

    schema = (
        build_default_registry()
        .get("run_subagent")
        .api_schema()["function"]["parameters"]
    )
    assert "timeout_seconds" not in schema["properties"]

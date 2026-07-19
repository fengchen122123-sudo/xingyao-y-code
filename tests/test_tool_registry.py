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


def test_subagent_schema_defaults_to_foreground_with_bounded_timeout():
    schema = build_default_registry().get("run_subagent").api_schema()
    properties = schema["function"]["parameters"]["properties"]

    assert properties["run_in_background"]["default"] is False
    assert properties["timeout_seconds"]["default"] == 120
    assert properties["timeout_seconds"]["minimum"] == 30
    assert properties["timeout_seconds"]["maximum"] == 3600


def test_subagent_schema_exposes_role_and_plan_item():
    properties = build_default_registry().get("run_subagent").api_schema()["function"]["parameters"]["properties"]

    assert properties["subagent_type"]["default"] == "general"
    assert properties["subagent_type"]["enum"] == ["explore", "implement", "test", "review", "general"]
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

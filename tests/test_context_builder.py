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


def test_stable_prompt_does_not_require_private_reasoning_or_infinite_retry():
    prompt = open("system_prompt.md", encoding="utf-8").read()

    assert "思考过程用 <think>" not in prompt
    assert "绝对不要停下" not in prompt
    assert "必须立刻主动调用 `execute_command`" not in prompt

from core.context_builder import RuntimeContext, build_dynamic_prompt


def _runtime(**overrides):
    base = dict(
        model_name="m",
        permission_mode="full",
        working_dir="D:/proj",
        current_time="2026-09-20 12:00:00",
        command_shell="pwsh",
        active_plan="无",
        ai_output_dir="C:/Users/u/.ycode/tmp",
    )
    base.update(overrides)
    return RuntimeContext(**base)


def test_context_block_contains_ai_output_dir():
    prompt = build_dynamic_prompt(_runtime())
    assert "AI 生成内容目录：C:/Users/u/.ycode/tmp" in prompt


def test_context_block_declares_ai_output_dir_policy():
    prompt = build_dynamic_prompt(_runtime())
    assert "AI 产出的图片、下载文件与临时脚本一律写到该目录" in prompt
    assert "用户显式指定了保存位置时以用户指定为准" in prompt
    assert prompt.index("默认命令解释器：pwsh") < prompt.index("AI 生成内容目录：")


def test_context_block_ai_output_dir_follows_runtime_value():
    prompt = build_dynamic_prompt(_runtime(ai_output_dir="E:/ai_out"))
    assert "AI 生成内容目录：E:/ai_out" in prompt
    assert "C:/Users/u/.ycode/tmp" not in prompt


def test_dynamic_prompt_stays_free_of_time_field():
    prompt = build_dynamic_prompt(_runtime())
    assert "2026-09-20 12:00:00" not in prompt

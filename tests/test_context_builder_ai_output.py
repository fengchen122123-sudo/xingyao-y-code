"""运行时上下文块必须暴露 AI 生成内容目录（规格 6.2）。

说明：计划文档写的是 `build_turn_context`，实际入口是
`core.context_builder.build_dynamic_prompt`（agent_service 调用的就是它），
此处按真实 API 编写。
"""

from core.context_builder import RuntimeContext, build_dynamic_prompt


def _runtime(**overrides):
    """构造 RuntimeContext：前四个字段必填，其余字段用默认值/覆盖值。"""
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
    """目录行之后必须紧跟落点约定（未指定走默认、显式指定以用户为准）。"""
    prompt = build_dynamic_prompt(_runtime())
    assert "AI 产出的图片、下载文件与临时脚本一律写到该目录" in prompt
    assert "用户显式指定了保存位置时以用户指定为准" in prompt
    # 该行紧跟"默认命令解释器"说明之后，保持动态块既有顺序
    assert prompt.index("默认命令解释器：pwsh") < prompt.index("AI 生成内容目录：")


def test_context_block_ai_output_dir_follows_runtime_value():
    prompt = build_dynamic_prompt(_runtime(ai_output_dir="E:/ai_out"))
    assert "AI 生成内容目录：E:/ai_out" in prompt
    assert "C:/Users/u/.ycode/tmp" not in prompt


def test_dynamic_prompt_stays_free_of_time_field():
    """回归保护：新增目录行不得把每轮必变字段带进动态块（前缀缓存铁律）。"""
    prompt = build_dynamic_prompt(_runtime())
    assert "2026-09-20 12:00:00" not in prompt

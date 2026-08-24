"""系统提示词阶段汇报规则回归：GUI 与 CLI 均需定义公开阶段汇报并排除私有推理与逐工具播报。"""

from core.agent_service import load_system_prompt


def test_cli_prompt_defines_stage_progress_commentary():
    prompt = load_system_prompt(is_gui=False)
    assert "执行中阶段汇报" in prompt
    assert "不要逐个工具调用" in prompt
    assert "私有内部推理" in prompt
    assert "阶段切换" in prompt
    assert "未经工具结果证实的成功" in prompt


def test_gui_prompt_defines_stage_progress_commentary():
    prompt = load_system_prompt(is_gui=True)
    assert "执行中阶段汇报" in prompt
    assert "不要逐个工具调用" in prompt
    assert "私有内部推理" in prompt
    assert "阶段切换" in prompt
    assert "未经工具结果证实的成功" in prompt

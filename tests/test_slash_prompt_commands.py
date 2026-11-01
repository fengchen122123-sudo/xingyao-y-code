"""提示词类斜杠命令的测试：模板填充与注册表登记。"""

from core.slash_commands import registry
from core.slash_commands.prompts import build_prompt
from core.slash_commands.specs import register_builtin_commands

PROMPT_NAMES = ("/fix", "/review", "/init", "/test", "/explain")


def test_fix_keeps_error_text_and_mentions_root_cause():
    """报错原文必须原样进提示词，且模板要求先定位根因。"""
    prompt = build_prompt("/fix", "TypeError: NoneType")
    assert "TypeError: NoneType" in prompt
    assert "根因" in prompt


def test_review_without_args_uses_default_scope():
    """空参数被兜底成默认范围，模板非空且点明“工作区”。"""
    prompt = build_prompt("/review", "")
    assert prompt
    assert "工作区" in prompt


def test_init_mentions_agents_md():
    """必须是生成 AGENTS.md 的指令。"""
    assert "AGENTS.md" in build_prompt("/init", "")


def test_unknown_command_returns_empty_string():
    """未知命令返回空串，交给调用方走未注册分支。"""
    assert build_prompt("/nope", "") == ""


def test_register_builtin_commands_is_idempotent_and_registers_prompts():
    """重复调用不报错，且注册后能按名取到 5 条 prompt 类命令。"""
    registry.reset()
    try:
        register_builtin_commands()
        register_builtin_commands()  # 幂等：GUI 与 CLI 启动各调一次

        for name in PROMPT_NAMES:
            spec = registry.get(name)
            assert spec is not None, name
            assert spec.kind == "prompt"
        # 只统计提示词类命令：注册表还含 local / hybrid 命令，比总数会随新增命令失效。
        assert len([s for s in registry.all_specs() if s.kind == "prompt"]) == len(
            PROMPT_NAMES
        )
    finally:
        registry.reset()


def test_fix_handler_returns_ok_prompt():
    """handler 契约：返回 dict 且把参数带进 prompt。"""
    registry.reset()
    try:
        register_builtin_commands()
        result = registry.get("/fix").handler("boom", None)
        assert result["ok"] is True
        assert "boom" in result["prompt"]
    finally:
        registry.reset()

from core.slash_commands import registry
from core.slash_commands.prompts import build_prompt
from core.slash_commands.specs import register_builtin_commands

PROMPT_NAMES = ("/fix", "/review", "/init", "/test", "/explain")


def test_fix_keeps_error_text_and_mentions_root_cause():
    prompt = build_prompt("/fix", "TypeError: NoneType")
    assert "TypeError: NoneType" in prompt
    assert "根因" in prompt


def test_review_without_args_uses_default_scope():
    prompt = build_prompt("/review", "")
    assert prompt
    assert "工作区" in prompt


def test_init_mentions_agents_md():
    assert "AGENTS.md" in build_prompt("/init", "")


def test_unknown_command_returns_empty_string():
    assert build_prompt("/nope", "") == ""


def test_register_builtin_commands_is_idempotent_and_registers_prompts():
    registry.reset()
    try:
        register_builtin_commands()
        register_builtin_commands()

        for name in PROMPT_NAMES:
            spec = registry.get(name)
            assert spec is not None, name
            assert spec.kind == "prompt"
        assert len([s for s in registry.all_specs() if s.kind == "prompt"]) == len(
            PROMPT_NAMES
        )
    finally:
        registry.reset()


def test_fix_handler_returns_ok_prompt():
    registry.reset()
    try:
        register_builtin_commands()
        result = registry.get("/fix").handler("boom", None)
        assert result["ok"] is True
        assert "boom" in result["prompt"]
    finally:
        registry.reset()

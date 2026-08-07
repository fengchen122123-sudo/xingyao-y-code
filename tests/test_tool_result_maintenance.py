from core.tool_result_maintenance import (
    ELIDED_PREFIX,
    SNIPPED_PREFIX,
    elide_tool_result,
    estimate_text_tokens,
    is_elided,
    is_frozen,
    is_snipped,
    snip_tool_result,
)


def _long_lines(n):
    return "\n".join(f"line-{i} " + "x" * 40 for i in range(n))


def test_snip_truncates_long_output_keeping_head_and_tail():
    text = _long_lines(300)
    result = snip_tool_result("read_file", text)
    assert result is not None
    assert result.startswith(SNIPPED_PREFIX)
    assert "line-0" in result
    assert "line-299" in result
    assert "line-150" not in result
    assert len(result) < len(text)


def test_snip_is_idempotent_via_marker():
    text = _long_lines(300)
    once = snip_tool_result("read_file", text)
    assert snip_tool_result("read_file", once) is None  # 已冻结，绝不二次改写


def test_snip_skips_small_and_error_results():
    assert snip_tool_result("read_file", "short result") is None
    assert snip_tool_result("execute_command", "🚨 命令执行失败\n" + "y" * 5000) is None


def test_elide_replaces_whole_body_with_placeholder():
    text = _long_lines(300)
    result = elide_tool_result("execute_command", text)
    assert result.startswith(ELIDED_PREFIX)
    assert "line-0" not in result
    assert "re-run the tool" in result


def test_elide_upgrades_snipped_but_never_touches_elided():
    text = _long_lines(300)
    snipped = snip_tool_result("read_file", text)
    upgraded = elide_tool_result("read_file", snipped)
    assert upgraded is not None and upgraded.startswith(ELIDED_PREFIX)
    assert elide_tool_result("read_file", upgraded) is None  # 单调：升级后永久冻结


def test_marker_helpers():
    assert is_snipped(SNIPPED_PREFIX + " ...")
    assert is_elided(ELIDED_PREFIX + " ...")
    assert is_frozen(ELIDED_PREFIX + " ...")
    assert not is_frozen("normal output")


def test_estimate_text_tokens_cjk_aware():
    assert estimate_text_tokens("") == 0
    # CJK：字符数主导；ASCII：bytes/4 主导
    assert estimate_text_tokens("汉" * 100) >= 100
    assert estimate_text_tokens("a" * 400) == 100

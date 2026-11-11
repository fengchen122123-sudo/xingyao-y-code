from core.slash_commands.parser import ParsedCommand, parse_slash_command

KNOWN = {"/goal", "/fix", "/review"}


def test_unknown_slash_falls_back_to_text():
    """未命中白名单的 / 开头文本必须当普通消息，例如路径。"""
    assert parse_slash_command("/usr/bin 是什么", KNOWN) is None


def test_known_command_with_args():
    parsed = parse_slash_command("/fix TypeError: x is None", KNOWN)
    assert parsed.name == "/fix"
    assert parsed.args == "TypeError: x is None"


def test_case_insensitive_name_preserves_args():
    parsed = parse_slash_command("/FIX 报错原文", KNOWN)
    assert parsed.name == "/fix"
    assert parsed.args == "报错原文"


def test_double_slash_escape():
    assert parse_slash_command("//fix 这是正文", KNOWN) is None


def test_leading_whitespace_ignored():
    assert parse_slash_command("   /review", KNOWN).name == "/review"


def test_multiline_args_preserved():
    """参数跨行时整体保留，只丢掉命令名之前的空白。"""
    parsed = parse_slash_command("/review\n后续行", KNOWN)
    assert parsed.name == "/review"
    assert parsed.args == "后续行"


def test_multiple_spaces_between_name_and_args():
    """命令名与参数之间允许多个空格，参数内部空格原样保留。"""
    parsed = parse_slash_command("/fix    a  b", KNOWN)
    assert parsed.name == "/fix"
    assert parsed.args == "a  b"


def test_raw_keeps_original_text():
    text = "  /FIX  报错  "
    parsed = parse_slash_command(text, KNOWN)
    assert isinstance(parsed, ParsedCommand)
    assert parsed.raw == text
    assert (parsed.name, parsed.args) == ("/fix", "报错")


def test_plain_text_and_lone_slash_are_not_commands():
    assert parse_slash_command("你好 /fix", KNOWN) is None
    assert parse_slash_command("/", KNOWN) is None
    assert parse_slash_command("", KNOWN) is None
    assert parse_slash_command(None, KNOWN) is None

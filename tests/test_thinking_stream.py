from core.thinking_stream import ThinkingStreamBuffer


def test_complete_sentence_flushes_before_finish():
    buffer = ThinkingStreamBuffer(max_visible_chars=120)

    assert buffer.feed("先分析。后续") == ["先分析。"]
    assert buffer.finish() == ["后续"]


def test_newline_flushes_a_complete_block():
    buffer = ThinkingStreamBuffer()

    assert buffer.feed("第一行\n第二行") == ["第一行"]
    assert buffer.finish() == ["第二行"]


def test_long_unpunctuated_text_prefers_nearby_whitespace():
    buffer = ThinkingStreamBuffer(max_visible_chars=12)

    assert buffer.feed("alpha beta gamma") == ["alpha beta"]
    assert buffer.finish() == ["gamma"]


def test_long_unpunctuated_text_without_whitespace_uses_hard_limit():
    buffer = ThinkingStreamBuffer(max_visible_chars=12)

    assert buffer.feed("abcdefghijklmnop") == ["abcdefghijkl"]
    assert buffer.finish() == ["mnop"]


def test_invalid_visible_limit_is_rejected():
    try:
        ThinkingStreamBuffer(max_visible_chars=0)
    except ValueError as error:
        assert str(error) == "max_visible_chars must be positive"
    else:
        raise AssertionError("expected ValueError")

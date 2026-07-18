from core.reasoning_stream import ReasoningStreamParser, split_reasoning_text


def test_parser_separates_tags_split_across_chunks():
    parser = ReasoningStreamParser()
    parts = []
    parts.extend(parser.feed("开场 <thi"))
    parts.extend(parser.feed("nk>内部分析"))
    parts.extend(parser.feed("</thi"))
    parts.extend(parser.feed("nk>最终结论"))
    parts.extend(parser.finish())

    assert [(part.kind, part.text) for part in parts] == [
        ("assistant", "开场 "),
        ("thinking", "内部分析"),
        ("assistant", "最终结论"),
    ]


def test_unclosed_thinking_stays_reasoning_at_end_of_stream():
    reasoning, answer = split_reasoning_text("<thinking>仍在分析")

    assert reasoning == "仍在分析"
    assert answer == ""


def test_plain_text_is_public_answer():
    reasoning, answer = split_reasoning_text("直接回答")

    assert reasoning == ""
    assert answer == "直接回答"

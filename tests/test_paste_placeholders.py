from core.paste_placeholders import (
    PASTE_CHAR_THRESHOLD,
    PastedTextStore,
    normalize_pasted_text,
)


def test_short_text_is_not_placeholderized():
    store = PastedTextStore()

    assert store.maybe_placeholderize("你好") == "你好"
    assert store.maybe_placeholderize("两行\n文本") == "两行\n文本"


def test_multiline_paste_is_folded_and_expand_round_trips():
    store = PastedTextStore()
    original = "第一行\n第二行\n第三行\n第四行"

    token = store.maybe_placeholderize(original)

    assert token == "[粘贴文本 #1 · 共 4 行]"
    assert store.expand(f"帮我看看 {token}") == f"帮我看看 {original}"


def test_long_single_line_paste_is_folded_without_line_count():
    store = PastedTextStore()
    original = "x" * PASTE_CHAR_THRESHOLD

    token = store.maybe_placeholderize(original)

    assert token == "[粘贴文本 #1]"
    assert store.expand(token) == original


def test_multiple_pastes_get_incrementing_ids_and_all_expand():
    store = PastedTextStore()
    first = "a\nb\nc"
    second = "d\ne\nf\ng"
    token1 = store.maybe_placeholderize(first)
    token2 = store.maybe_placeholderize(second)

    assert token1 == "[粘贴文本 #1 · 共 3 行]"
    assert token2 == "[粘贴文本 #2 · 共 4 行]"
    assert store.expand(f"{token1} 和 {token2}") == f"{first} 和 {second}"


def test_unknown_placeholder_is_kept_literal():
    store = PastedTextStore()

    assert store.expand("[粘贴文本 #99]") == "[粘贴文本 #99]"
    assert store.expand("[粘贴文本 #99 · 共 10 行]") == "[粘贴文本 #99 · 共 10 行]"


def test_normalize_pasted_text_unifies_newlines():
    assert normalize_pasted_text("a\r\nb\rc") == "a\nb\nc"
    assert normalize_pasted_text("") == ""
    assert normalize_pasted_text(None) == ""

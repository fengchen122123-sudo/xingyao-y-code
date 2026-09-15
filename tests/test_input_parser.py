import base64
import io
import os

import pytest
from PIL import Image

import core.input_parser as input_parser
from core.input_parser import MAX_TEXT_BYTES, parse_user_input


def _data_url_bytes(content):
    image_url = next(block for block in content if block["type"] == "image_url")["image_url"]["url"]
    header, encoded = image_url.split(",", 1)
    return header, base64.b64decode(encoded)


def test_at_file_text_attachment(tmp_path, monkeypatch):
    """@文件 语法附加文本内容，并从正文中移除 @引用"""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "note.txt").write_text("hello world", encoding="utf-8")

    content, msgs = parse_user_input("看看 @note.txt")

    assert isinstance(content, str)
    assert "hello world" in content
    assert "@note.txt" not in content
    assert any("note.txt" in m for m in msgs)


def test_backslash_absolute_path_is_extracted(tmp_path, monkeypatch):
    """反斜杠盘符绝对路径 D:\\... 仍可识别（模拟 /sandbox on 放行工作目录外路径）"""
    # M38：默认沙盒下工作目录外路径会被拒绝，这里显式模拟 /sandbox on
    monkeypatch.setattr(input_parser, "_is_within_sandbox", lambda p: True)
    p = tmp_path / "back.txt"
    p.write_text("backslash-data", encoding="utf-8")

    content, msgs = parse_user_input(f"分析 {str(p)}")

    assert "backslash-data" in content


def test_forward_slash_absolute_path_is_extracted(tmp_path, monkeypatch):
    """正斜杠盘符绝对路径 D:/... 也必须识别（模拟 /sandbox on 放行工作目录外路径）"""
    monkeypatch.setattr(input_parser, "_is_within_sandbox", lambda p: True)
    p = tmp_path / "fwd.txt"
    p.write_text("forward-data", encoding="utf-8")
    fwd = str(p).replace("\\", "/")

    content, msgs = parse_user_input(f"分析 {fwd} 谢谢")

    assert "forward-data" in content


def test_outside_workspace_attachment_is_rejected(tmp_path, monkeypatch):
    """M38：默认沙盒下，工作目录外的文件不得被内联进 prompt"""
    # 强制默认沙盒语义（不受本机 /sandbox on 配置影响）
    monkeypatch.setattr("core.tools._outside_workspace_allowed", lambda: False)
    p = tmp_path / "secret.txt"
    p.write_text("top-secret", encoding="utf-8")

    content, msgs = parse_user_input(f"分析 {str(p)}")

    assert "top-secret" not in str(content)
    assert any("已跳过" in m for m in msgs)


def test_binary_file_path_is_attached(tmp_path, monkeypatch):
    """二进制文件无法内联内容，但路径会告知模型，由模型用工具解析"""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "bin.dat").write_bytes(b"\x00\x01\x02binary")

    content, msgs = parse_user_input("@bin.dat")

    assert any("二进制" in m for m in msgs)
    assert "bin.dat" in content  # 路径已告知模型
    assert "```" not in content  # 原始字节不会被内联成代码块


def test_oversized_image_is_skipped(tmp_path, monkeypatch):
    """图片超过大小上限跳过"""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "big.png").write_bytes(b"\x89PNG" + b"\x00" * 100)
    monkeypatch.setattr(input_parser, "MAX_IMAGE_BYTES", 10)

    content, msgs = parse_user_input("@big.png")

    assert any("超过" in m and "上限" in m and "已跳过" in m for m in msgs)


def test_default_image_limit_accepts_files_larger_than_old_five_mib(tmp_path, monkeypatch):
    assert input_parser.MAX_IMAGE_BYTES == 20 * 1024 * 1024
    monkeypatch.chdir(tmp_path)
    image_path = tmp_path / "large.jpg"
    image = Image.effect_noise((3600, 2400), 100).convert("RGB")
    image.save(image_path, format="JPEG", quality=100)
    assert image_path.stat().st_size > 5 * 1024 * 1024

    content, msgs = parse_user_input("@large.jpg")
    header, payload = _data_url_bytes(content)

    assert isinstance(content, list)
    assert not any("超过 20MB" in message for message in msgs)
    assert header == "data:image/jpeg;base64"
    with Image.open(io.BytesIO(payload)) as decoded:
        assert max(decoded.size) == 1536


def test_png_stays_png_and_data_url_uses_actual_mime(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    image_path = tmp_path / "screenshot.jpg"
    image = Image.new("RGB", (120, 80), "white")
    image.save(image_path, format="PNG")

    content, _ = parse_user_input("@screenshot.jpg")
    header, payload = _data_url_bytes(content)

    assert header == "data:image/png;base64"
    assert payload.startswith(b"\x89PNG\r\n\x1a\n")


def test_transparent_png_keeps_alpha_channel():
    source = io.BytesIO()
    Image.new("RGBA", (2000, 1000), (10, 20, 30, 64)).save(source, format="PNG")

    result = input_parser._compress_image_bytes(source.getvalue(), "png", max_edge=1536)

    with Image.open(io.BytesIO(result.data)) as decoded:
        assert result.mime_type == "image/png"
        assert decoded.mode == "RGBA"
        assert decoded.getextrema()[3] == (64, 64)


def test_jpeg_is_progressive_and_respects_exif_orientation():
    source = io.BytesIO()
    image = Image.effect_noise((80, 40), 80).convert("RGB")
    exif = Image.Exif()
    exif[274] = 6
    image.save(source, format="JPEG", quality=100, exif=exif)

    result = input_parser._compress_image_bytes(
        source.getvalue(), "jpeg", max_edge=30, quality=90, max_output_bytes=1024 * 1024
    )

    with Image.open(io.BytesIO(result.data)) as decoded:
        assert result.mime_type == "image/jpeg"
        assert decoded.size == (15, 30)
        assert decoded.info.get("progressive") or decoded.info.get("progression")


def test_decoded_pixel_limit_is_enforced():
    source = io.BytesIO()
    Image.new("RGB", (11, 10), "white").save(source, format="PNG")

    with pytest.raises(ValueError, match="像素"):
        input_parser._compress_image_bytes(
            source.getvalue(), "png", max_decoded_pixels=100
        )


@pytest.mark.parametrize(
    ("quality", "expected"),
    [
        (92, [92, 87, 82, 77, 72, 67, 62, 57, 52, 50]),
        (91, [91, 86, 81, 76, 71, 66, 61, 56, 51, 50]),
    ],
)
def test_quality_steps_always_reach_fifty(quality, expected):
    assert input_parser._quality_steps(quality) == expected


def test_animated_gif_is_kept_byte_for_byte():
    source = io.BytesIO()
    frames = [Image.new("P", (12, 8), color) for color in (1, 2)]
    frames[0].save(
        source,
        format="GIF",
        save_all=True,
        append_images=frames[1:],
        duration=100,
        loop=0,
    )
    raw = source.getvalue()

    result = input_parser._compress_image_bytes(raw, "gif")

    assert result.data == raw
    assert result.mime_type == "image/gif"
    with pytest.raises(ValueError, match="像素"):
        input_parser._compress_image_bytes(raw, "gif", max_decoded_pixels=95)


def test_text_file_over_size_limit_is_truncated(tmp_path, monkeypatch):
    """文本超过 200KB 截断并附截断标记"""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "big.txt").write_bytes(b"a" * (MAX_TEXT_BYTES + 100))

    content, msgs = parse_user_input("@big.txt")

    assert "已截断" in content
    # 截断后的正文不应包含完整的超长内容
    assert content.count("a") < MAX_TEXT_BYTES + 100


def test_truncated_utf8_multibyte_not_misjudged_as_binary(tmp_path, monkeypatch):
    """截断切在 UTF-8 多字节序列中间时，用 replace 解码而不是误判二进制跳过"""
    monkeypatch.chdir(tmp_path)
    # 让 raw[:MAX_TEXT_BYTES] 的最后一个字节恰为 emoji 的首字节 0xF0，
    # utf-8 与 gbk 严格解码都会在尾部失败
    payload = b"a" * (MAX_TEXT_BYTES - 1) + "\U0001F600".encode("utf-8") + b"tail"
    (tmp_path / "big.txt").write_bytes(payload)

    content, msgs = parse_user_input("@big.txt")

    assert any("已附加代码/文本" in m for m in msgs)
    assert not any("无法解码" in m for m in msgs)
    assert "已截断" in content


def test_duplicate_attachments_deduped_keep_order(tmp_path, monkeypatch):
    """重复引用保序去重"""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "x.txt").write_text("X", encoding="utf-8")
    (tmp_path / "y.txt").write_text("Y", encoding="utf-8")

    content, msgs = parse_user_input("@x.txt @y.txt @x.txt")

    assert content.count("[附带上下文: x.txt]") == 1
    assert content.index("x.txt") < content.index("y.txt")


def test_at_removal_replaces_only_first_occurrence(tmp_path, monkeypatch):
    """@xxx 只替换首个匹配，正文中相同的字串不被误删"""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "f.txt").write_text("Z", encoding="utf-8")

    content, msgs = parse_user_input("@f.txt 正文提到 f.txt 字符")

    assert "正文提到 f.txt 字符" in content


def _get_short_path(path):
    """取 Windows 8.3 短路径；系统禁用短名或非 Windows 时返回 None（调用方 skip）"""
    if os.name != "nt":
        return None
    import ctypes
    buf = ctypes.create_unicode_buffer(260)
    if ctypes.windll.kernel32.GetShortPathNameW(str(path), buf, 260) == 0:
        return None
    short = buf.value
    return short if short != str(path) else None


def test_short_path_and_explicit_dual_channel_deduped(tmp_path, monkeypatch):
    """8.3 短路径场景（手机端 upload_file 落盘 %TEMP% 为短路径的机器）：
    同一图片经文本通道（短路径原样）+ explicit 通道（resolve 为长路径）进入，
    必须只附加一次，不得出现两段标记文本与两张图片"""
    monkeypatch.setattr(input_parser, "_is_within_sandbox", lambda p: True)
    img = tmp_path / "dup_short.jpg"
    Image.new("RGB", (32, 32), (255, 0, 0)).save(img, "JPEG")
    short = _get_short_path(img)
    if not short:
        pytest.skip("本机未启用 8.3 短文件名")

    content, msgs = parse_user_input(f"看图 @{short}", explicit_paths=[short])

    assert isinstance(content, list)
    text_part = content[0]["text"]
    assert text_part.count("[附带图片文件") == 1
    assert sum(1 for b in content if isinstance(b, dict) and b.get("type") == "image_url") == 1


def test_slash_mixed_dual_channel_deduped(tmp_path, monkeypatch):
    """同一文件以正斜杠（文本通道）与反斜杠（explicit 通道）两种形态进入，
    必须只附加一次"""
    monkeypatch.setattr(input_parser, "_is_within_sandbox", lambda p: True)
    img = tmp_path / "dup_slash.jpg"
    Image.new("RGB", (32, 32), (0, 255, 0)).save(img, "JPEG")
    fwd = str(img).replace("\\", "/")

    content, msgs = parse_user_input(f"看图 {fwd}", explicit_paths=[str(img)])

    assert isinstance(content, list)
    text_part = content[0]["text"]
    assert text_part.count("[附带图片文件") == 1
    assert sum(1 for b in content if isinstance(b, dict) and b.get("type") == "image_url") == 1


def test_extract_session_refs_basic():
    from core.input_parser import extract_session_refs
    cleaned, refs = extract_session_refs("参考 @{登录bug排查} 的结论继续")
    assert refs == ["登录bug排查"]
    assert "@{" not in cleaned
    assert "参考" in cleaned and "的结论继续" in cleaned


def test_extract_session_refs_none_and_unclosed():
    from core.input_parser import extract_session_refs
    assert extract_session_refs("没有引用") == ("没有引用", [])
    # 未闭合 / 空内容 / 跨行都不提取
    assert extract_session_refs("@{没闭合") == ("@{没闭合", [])
    assert extract_session_refs("@{}") == ("@{}", [])
    assert extract_session_refs("@{跨\n行}") == ("@{跨\n行}", [])


def test_extract_session_refs_dedup_and_multi():
    from core.input_parser import extract_session_refs
    cleaned, refs = extract_session_refs("@{甲} 和 @{乙}，再说 @{甲}")
    assert refs == ["甲", "乙"]


def test_extract_session_refs_coexists_with_at_file(tmp_path):
    from core.input_parser import extract_session_refs
    f = tmp_path / "config.py"
    f.write_text("x=1", encoding="utf-8")
    text = f"@{{我的对话}} 看一下 @{f.name}"
    cleaned, refs = extract_session_refs(text)
    assert refs == ["我的对话"]
    assert f"@{f.name}" in cleaned  # @文件 标记原样保留给 parse_user_input

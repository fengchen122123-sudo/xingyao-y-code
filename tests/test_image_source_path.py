"""折叠占位符记录原图来源路径（2026-09-02 设计）的回归测试。

链路：input_parser 入库时记录 source_path → _strip_media_and_merge 发往模型前剥离
→ memory 折叠旧图时占位文本带路径。设计文档：
docs/superpowers/specs/2026-09-02-image-fold-source-path-design.md
"""

import os

from PIL import Image

from core.input_parser import parse_user_input
from core.memory import MemoryManager, _FOLDED_MEDIA_TEXT


def test_image_block_records_source_path(tmp_path, monkeypatch):
    """图片块记录来源绝对路径，折叠后的占位符据此引用原图"""
    monkeypatch.chdir(tmp_path)
    image_path = tmp_path / "shot.png"
    Image.new("RGB", (16, 16), "white").save(image_path, format="PNG")

    content, _ = parse_user_input("@shot.png")

    block = next(b for b in content if b["type"] == "image_url")
    assert block["source_path"] == os.path.abspath(str(image_path))


def test_fold_stale_media_placeholder_carries_source_path():
    """折叠旧图片时占位文本带来源路径，最新一条带图消息保持原样"""
    manager = MemoryManager(":memory:")
    session = "fold-source-path"
    manager.add_message(session, "user", [
        {"type": "text", "text": "第一张"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"},
         "source_path": "D:/pics/first.png"},
    ])
    # 写入新的含媒体消息，触发旧媒体折叠
    manager.add_message(session, "user", [
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,BBBB"},
         "source_path": "D:/pics/second.png"},
    ])

    history = manager.get_history(session)
    first_blocks = history[0]["content"]
    placeholder = next(
        b["text"] for b in first_blocks
        if isinstance(b, dict) and b.get("type") == "text" and "折叠" in b.get("text", "")
    )
    assert "D:/pics/first.png" in placeholder
    assert "AAAA" not in placeholder  # base64 已清除
    # 最新一条带图消息不折叠
    assert history[1]["content"][0]["type"] == "image_url"


def test_fold_stale_media_without_source_path_keeps_legacy_text():
    """无 source_path 的历史图片块折叠为原占位文案（向后兼容老数据）"""
    manager = MemoryManager(":memory:")
    session = "fold-legacy"
    manager.add_message(session, "user", [
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
    ])
    manager.add_message(session, "user", [
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,BBBB"}},
    ])

    history = manager.get_history(session)
    assert history[0]["content"][0] == {"type": "text", "text": _FOLDED_MEDIA_TEXT}


def test_kept_media_blocks_strip_source_path(monkeypatch):
    """保留透传给 provider 的图片块必须剥离 source_path 私有字段，避免接口 400"""
    import core.llm as llm_mod

    monkeypatch.setattr(llm_mod, "_supports_vision", lambda model: True)
    messages = [{
        "role": "user",
        "content": [
            {"type": "text", "text": "看图"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAABBBB"},
             "source_path": "D:/pics/a.png"},
        ],
    }]
    out = llm_mod._strip_media_and_merge(messages, "openai/vision-model")[0]

    block = out[0]["content"][1]
    assert block["type"] == "image_url"
    assert "AAAABBBB" in block["image_url"]["url"]
    assert "source_path" not in block

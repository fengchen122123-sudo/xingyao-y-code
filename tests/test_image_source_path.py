import os

from PIL import Image

from core.input_parser import parse_user_input
from core.memory import MemoryManager, _FOLDED_MEDIA_TEXT


def test_image_block_records_source_path(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    image_path = tmp_path / "shot.png"
    Image.new("RGB", (16, 16), "white").save(image_path, format="PNG")

    content, _ = parse_user_input("@shot.png")

    block = next(b for b in content if b["type"] == "image_url")
    assert block["source_path"] == os.path.abspath(str(image_path))


def test_fold_stale_media_placeholder_carries_source_path():
    manager = MemoryManager(":memory:")
    session = "fold-source-path"
    manager.add_message(session, "user", [
        {"type": "text", "text": "第一张"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"},
         "source_path": "D:/pics/first.png"},
    ])
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
    assert "AAAA" not in placeholder
    assert history[1]["content"][0]["type"] == "image_url"


def test_fold_stale_media_without_source_path_keeps_legacy_text():
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

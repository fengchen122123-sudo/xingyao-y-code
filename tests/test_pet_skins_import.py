"""桌宠导入、人设编辑、删除及 AI 提示词集成测试。"""

import io
import json
import zipfile
from pathlib import Path

from PIL import Image

from core import pet_chat, pet_skins


def _make_png_bytes(color=(255, 120, 80, 255), size=(64, 64)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGBA", size, color).save(buf, format="PNG")
    return buf.getvalue()


def _make_gif_bytes(frames=3) -> bytes:
    imgs = [Image.new("RGBA", (48, 48), (i * 60, 100, 200, 255)) for i in range(frames)]
    buf = io.BytesIO()
    imgs[0].save(buf, format="GIF", save_all=True, append_images=imgs[1:], duration=100, loop=0)
    return buf.getvalue()


def test_import_images_and_gif_and_persona_integration(tmp_path, monkeypatch):
    monkeypatch.setattr(pet_skins, "USER_SKIN_DIR", tmp_path)

    res = pet_skins.import_skin_from_images(
        "小橘猫",
        [_make_gif_bytes(3)],
        persona="傲娇但关心代码进度的橘猫",
    )
    assert res["ok"] is True
    skin = res["skin"]
    assert skin["name"] == "小橘猫"
    assert skin["persona"] == "傲娇但关心代码进度的橘猫"
    assert skin["source"] == "user"

    idle_dir = Path(skin["assets_dir"]) / "pet" / "idle"
    frames = sorted(idle_dir.glob("frame_*.png"))
    assert len(frames) == 3

    # 验证更新人设
    upd = pet_skins.update_skin_meta(skin["id"], name="大橘", persona="慵懒爱睡午觉的猫")
    assert upd["ok"] is True
    assert upd["skin"]["name"] == "大橘"
    assert upd["skin"]["persona"] == "慵懒爱睡午觉的猫"

    # 验证 pet_chat 提示词注入了自定义桌宠名字与人设
    monkeypatch.setattr(pet_skins, "active_skin_id", lambda: skin["id"])
    prompt = pet_chat._build_prompt()
    assert "大橘" in prompt
    assert "慵懒爱睡午觉的猫" in prompt

    # 验证删除
    monkeypatch.setattr(pet_skins, "set_active_skin", lambda sid: True)
    deleted = pet_skins.delete_skin(skin["id"])
    assert deleted["ok"] is True
    assert not Path(skin["dir"]).exists()


def test_import_zip_plain_images_and_zip_slip_guard(tmp_path, monkeypatch):
    monkeypatch.setattr(pet_skins, "USER_SKIN_DIR", tmp_path)

    # 非标准结构 ZIP：只有普通图片目录
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("my_pet/a.png", _make_png_bytes())
        zf.writestr("my_pet/b.png", _make_png_bytes((0, 200, 100, 255)))
    res = pet_skins.import_skin_from_zip(buf.getvalue(), name="压缩包宠物", persona="元气满满")
    assert res["ok"] is True
    assert res["skin"]["name"] == "压缩包宠物"
    assert res["skin"]["persona"] == "元气满满"
    assert (Path(res["skin"]["assets_dir"]) / "pet" / "idle" / "frame_000.png").is_file()
    assert (Path(res["skin"]["assets_dir"]) / "pet" / "idle" / "frame_001.png").is_file()

    # 路径穿越防护
    bad_buf = io.BytesIO()
    with zipfile.ZipFile(bad_buf, "w") as zf:
        zf.writestr("../evil.txt", b"bad")
    bad_res = pet_skins.import_skin_from_zip(bad_buf.getvalue(), name="bad")
    assert bad_res["ok"] is False
    assert "不安全路径" in bad_res["msg"]

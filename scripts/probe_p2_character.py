# -*- coding: utf-8 -*-
"""P2 角色链路真跑探针（人工受控执行，绝不由自动化无确认跑）。

复验「角色库与角色带入」的图像阶段端到端行为：桥层拼装函数与插件工具不改，
这里用同一份 compose_character_prompt + tokenrhythm_edit_image 走真实 API。
GUI 桥（pywebview）无法 headless 起，桥内编排改由单元测试覆盖；本探针只补
「拼装后的 prompt + 主参考垫图真的喂给 API，模型服从度如何」。

流程（全部最便宜档，合计预估 ¥0.4）：
    1. 文生图：写实少女半身像（tokenrhythm_generate_image，¥0.2）
    2. 建角色档案 + 存主参考（character_store + media_store 归档拿 digest，¥0）
    3. compose_character_prompt 拼装 → tokenrhythm_edit_image 垫图改图（¥0.2）
    4. 生成台账落库 + MAE(文生图, 定妆图) 参考指标
    5. 产物路径打印（定妆图给 probe_tokenrhythm_video --frame 做阶段 A/B 复验）

用法：
    venv\\Scripts\\python.exe scripts\\probe_p2_character.py --yes

判定标准：
    1. 定妆图 prompt 服从：产物与主参考是同一个人（人工核对，肉眼项）
    2. 生成台账记录齐全：prompt / final_prompt / digest 快照 / 费用 / 路径
    3. MAE(文生图, 定妆图) 只作参考——改图本来就要求内容变化，小不了，不设阈值
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import tempfile

# 项目根注入（与 probe_tokenrhythm_video 同一坑：scripts\ 下直接跑会找不到 core/plugins）
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def _load_module(rel_path: str, name: str):
    spec = importlib.util.spec_from_file_location(name, os.path.join(_ROOT, rel_path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _mae(image_a: str, image_b: str, size: int = 512) -> float | None:
    """与 probe_tokenrhythm_video 同口径的 MAE（512×512，0~255）。"""
    try:
        import numpy as np
        from PIL import Image

        def load(path):
            with Image.open(path) as img:
                return np.asarray(img.convert("RGB").resize((size, size)), dtype=np.int16)

        return float(np.abs(load(image_a) - load(image_b)).mean())
    except Exception as exc:
        print(f"[metrics] MAE 计算失败：{exc}")
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description="P2 角色链路真跑探针")
    parser.add_argument("--yes", action="store_true", help="跳过交互确认（非交互代跑用）")
    parser.add_argument(
        "--frame", default=None,
        help="复用已有的主参考图（跳过文生图，省 ¥0.2；脚本中途崩了用它续跑）",
    )
    args = parser.parse_args()
    spent_before = 0.2 if args.frame else 0.0
    if not args.yes:
        answer = input("将执行：文生图 ¥0.2 + 角色定妆图 ¥0.2，合计约 ¥0.4。确认请手敲 yes：")
        if answer.strip().lower() != "yes":
            print("已取消，一分钱没花。")
            return 0

    run_dir = tempfile.mkdtemp(prefix="probe_p2_character_")
    print(f"[run] 产物目录：{run_dir}")

    image_plugin = _load_module("plugins/tokenrhythm_image/plugin.py", "tr_image_plugin")

    # 1. 文生图：写实少女像（之后当主参考）
    portrait = str(args.frame or "").strip()
    if portrait:
        if not os.path.isfile(portrait):
            print(f"[1] --frame 指定的图不存在：{portrait}")
            return 1
        print(f"[1] 复用已有主参考图（不重复花文生图的钱）：{portrait}")
    else:
        prompt_t2i = (
            "一位 22 岁短发少女的写实摄影半身像，黑色齐肩短发，琥珀色眼睛，"
            "左脸颊有一颗小痣，米白色针织衫，柔和窗光，浅景深，85mm 镜头，自然肤色"
        )
        print("[1] 文生图（¥0.2）…")
        result = image_plugin.generate_image(prompt=prompt_t2i, size="1024x1024")
        if getattr(result, "is_error", False):
            print("[1] 失败：%s" % result.content)
            return 1
        portrait = str((getattr(result, "changed_files", None) or [])[0])
        meta = getattr(result, "metadata", None) or {}
        print(f"[1] 原始像：{portrait}")
        print(f"[1] metadata.oss_url 存在：{bool(meta.get('oss_url'))} cost={meta.get('cost_cny')}")
    if spent_before:
        print(f"[run] 本次运行预计只花角色定妆图 ¥0.2（文生图已在上一次运行花过）")

    # 2. 建角色 + 存主参考（临时 DB，不碰真实 ycode.db）。
    # 与桥层同一条铁律：先把图复制进「项目」再取相对路径——跨盘 relpath 直接
    # 抛 ValueError（C: 的临时产物对 D: 项目根），真实桥永远复制后再 relpath
    import shutil

    from core.character_store import CharacterStorage, compose_character_prompt
    from core.media_store import archive_media_file
    from core.memory import MemoryManager

    workdir = os.path.join(run_dir, "proj")
    refs_dir = os.path.join(workdir, ".ycode", "character_refs")
    os.makedirs(refs_dir, exist_ok=True)
    portrait_copy = os.path.join(refs_dir, "portrait.png")
    shutil.copy2(portrait, portrait_copy)

    storage = CharacterStorage(MemoryManager(os.path.join(run_dir, "chars.db")))
    character = storage.create_character(
        "小满",
        prompt_fragment="黑色齐肩短发，琥珀色眼睛，左脸颊有一颗小痣，米白色针织衫",
        style_tags="写实摄影, 柔和窗光, 浅景深",
        seed=4100,
        workdir=workdir,
    )
    item = archive_media_file(portrait_copy)
    out = storage.add_ref(
        character["id"], ref_type="front", media_digest=item["digest"],
        rel_path=os.path.relpath(portrait_copy, workdir), mime=item.get("mime"),
        width=item.get("width"), height=item.get("height"),
        source_node_id="probe", is_primary=True,
    )
    print(f"[2] 角色 id={character['id']} 主参考 digest={item['digest'][:16]}…"
          f"（replaced_primary={out['replaced_primary']}）")

    # 3. 拼装 + 垫图改图（走真实 edits 接口，与桥层同一份拼装函数）
    final_prompt = compose_character_prompt("正面半身定妆照，浅浅微笑", character)
    print(f"[3] final_prompt：\n{final_prompt}\n")
    print("[3] 角色定妆图（¥0.2）…")
    edit_result = image_plugin.edit_image(image=portrait, prompt=final_prompt)
    if getattr(edit_result, "is_error", False):
        print("[3] 失败：%s" % edit_result.content)
        return 1
    mugshot = str((getattr(edit_result, "changed_files", None) or [])[0])
    edit_meta = getattr(edit_result, "metadata", None) or {}
    print(f"[3] 定妆图：{mugshot}")
    print(f"[3] metadata.oss_url 存在：{bool(edit_meta.get('oss_url'))} cost={edit_meta.get('cost_cny')}")

    # 4. 生成台账落库 + 校验
    storage.record_generation({
        "kind": "image", "status": "success", "character_ids": [character["id"]],
        "prompt": "正面半身定妆照，浅浅微笑", "final_prompt": final_prompt,
        "params": {"model": "qwen-image-edit"}, "ref_snapshot": storage.ref_snapshot(character["id"]),
        "cost_cny": 0.2, "result_path": mugshot, "workdir": workdir,
    })
    records = storage.list_generation_records(workdir)
    assert records and records[0]["final_prompt"] == final_prompt
    print(f"[4] 台账：{json.dumps(records[0], ensure_ascii=False)[:220]}…")

    mae = _mae(portrait, mugshot)
    if mae is not None:
        print(f"[4] MAE(文生图, 定妆图) = {mae:.2f}（参考项：改图本来就要求内容变化，无阈值）")

    print("\n[判定] 定妆图下一步交给：")
    print(rf"    venv\Scripts\python.exe scripts\probe_tokenrhythm_video.py --frame ""{mugshot}"" --yes")
    return 0


if __name__ == "__main__":
    sys.exit(main())

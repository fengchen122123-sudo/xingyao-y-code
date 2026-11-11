# -*- coding: utf-8 -*-
"""TokenRhythm wan3.0-video 真实 API 探针（人工受控执行，绝不由自动化跑）。

用途：在烧真金白银之前，验证 tokenrhythm_video 插件实现的协议假设。
2026-11-03 的实测记录已经验证过一部分假设（role 顶层字段、content 数组、
success 终态、公网 URL 首帧、按秒计费），本探针覆盖插件依赖的完整链路：

    阶段 A（可选）  本地首帧 → /v1/images/edits 保真重绘 → data[0].url（¥0.2）
    阶段 B        文生视频（或图生视频）→ 轮询 → result.url → 下载

全部走最便宜档（480P / 2 秒 ≈ ¥0.6；带阶段 A 共 ≈ ¥0.8），每个阶段开始前
要求在终端手敲 yes 确认，Ctrl+C 可随时中止（轮询阶段中止不会退钱，但失败
任务不扣费）。

用法：
    venv\\Scripts\\python.exe scripts\\probe_tokenrhythm_video.py                    # 只测文生视频
    venv\\Scripts\\python.exe scripts\\probe_tokenrhythm_video.py --frame <图片路径>  # 先换首帧地址再测图生视频

判定标准（跑完对照）：
    1. 阶段 A 返回 data[0].url 是公网 HTTPS 地址 → 插件 _upload_frame_for_url 成立
    2. 阶段 B 创建返回 HTTP 202 + estimated_output_cost_cny → 请求体结构成立
    3. 轮询终态是 success（不是 succeeded）→ 插件终态判断成立
    4. billing.total_cost_cny 与 480P ¥0.3/秒 × 2s = ¥0.6 一致 → 计费口径成立
    5. result.url 能下载且开头字节是 ftyp → 产物落盘逻辑成立
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.request

_API_BASE = "https://tokenrhythm.studio/v1"
_POLL_INTERVAL = 5.0
_POLL_TIMEOUT = 600.0

# --yes 开关的模块级默认值：main() 里按参数覆盖（global 声明要求这里有绑定）
AUTO_YES = False

# 以 `python scripts\probe_...py` 运行时 sys.path[0] 是 scripts\，插件里的
# `import config` / `from core...` 会找不到项目根目录模块、_resolve_api_key
# 吞异常返回空串，报「未找到凭据」——这里显式注入项目根。
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def _load_plugin():
    """直接加载插件模块复用它的 _resolve_api_key / _upload_frame_for_url，
    保证探针验证的就是插件真实跑的代码，而不是又抄一份。"""
    entry = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "plugins", "tokenrhythm_video", "plugin.py",
    )
    spec = importlib.util.spec_from_file_location("_probe_tr_video_plugin", entry)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _confirm(step: str, cost: str) -> bool:
    print()
    print("=" * 60)
    print(f"下一步：{step}")
    print(f"预计花费：{cost}")
    if AUTO_YES:
        # --yes：用户已在对话里授权并划定预算，这里代替确认并留痕
        print("已按 --yes 自动确认（授权见对话记录）。")
        return True
    answer = input("确认执行请输入 yes（其它任意键跳过）：").strip().lower()
    return answer == "yes"


def _download(url: str, path: str, timeout=120.0) -> int:
    """下载到指定文件，返回字节数（用于重绘图/视频产物落盘）。"""
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        blob = resp.read()
    with open(path, "wb") as handle:
        handle.write(blob)
    return len(blob)


def _mae(image_a: str, image_b: str, size: int = 512) -> float | None:
    """两张图的 MAE（0~255，越小越像）。统一缩放到 size×size 再比，尺寸差异不进指标。

    口径与实测记录一致（导出记录里对照组 MAE 70~83、首帧锁命中 4.8~6.3），
    量级可直接对照。任何一张读不了就返回 None，不硬算。
    """
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


def _extract_first_frame(video_path: str, out_png: str) -> str | None:
    """从 mp4 抽首帧（opencv）。opencv 不在就把指标降级为「重绘图 vs 原图」。

    Windows 坑（2026-11-03 实测）：cv2.imwrite 对含中文的路径（用户目录带
    「冯晨昊」）静默失败——返回 False 不抛错，文件根本没落盘，之前探针打印
    「已存」是假成功，MAE 全部 No such file。改走 imencode 内存编码 + open()
    二进制写，任意路径都稳；写完必须回读确认存在才算成功。
    """
    try:
        import cv2

        cap = cv2.VideoCapture(video_path)
        try:
            ok, frame = cap.read()
            if not ok:
                return None
            encoded, buf = cv2.imencode(".png", frame)
            if not encoded:
                return None
            with open(out_png, "wb") as handle:
                handle.write(buf.tobytes())
        finally:
            cap.release()
        if not os.path.isfile(out_png):
            return None
        return out_png
    except Exception as exc:
        print(f"[metrics] 首帧提取失败（opencv 不可用？）：{exc}")
        return None


def _http_json(req, timeout=60.0):
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        text = resp.read().decode("utf-8")
    return json.loads(text) if text.strip() else {}


def _auth_headers(api_key, url):
    from core.llm_policy import session_id_headers, tokenrhythm_agent_headers

    return {
        "Content-Type": "application/json",
        "Authorization": "Bearer " + api_key,
        **tokenrhythm_agent_headers(url),
        **session_id_headers(),
    }


def probe(frame_path: str | None) -> int:
    plugin = _load_plugin()
    api_key = plugin._resolve_api_key()
    if not api_key:
        print("未找到 TokenRhythm 凭据：config providers 中没有 base_url 含 "
              "tokenrhythm.studio 的条目，或解密失败。先在设置页配好再跑。")
        return 2

    # 单次运行的所有产物（原图/重绘图/视频/抽帧）都收进一个目录，方便复查
    run_dir = tempfile.mkdtemp(prefix="probe_tr_video_")
    print(f"[run] 产物目录：{run_dir}")

    original_frame = os.path.abspath(frame_path) if frame_path else ""
    refit_png = os.path.join(run_dir, "refit.png")

    first_frame_url = ""
    if frame_path:
        if not _confirm("本地首帧经 /v1/images/edits 保真重绘换公网地址", "¥0.2（改图一张，qwen-image-2.0）"):
            print("跳过阶段 A，只测纯文生视频。")
        else:
            try:
                first_frame_url = plugin._upload_frame_for_url(original_frame, api_key)
                print(f"[A] 换地址成功（URL 含签名，只展示前 80 字符）：{first_frame_url[:80]}…")
            except Exception as exc:
                print(f"[A] 换地址失败：{exc}")
                return 1
            # 保真度量 1：重绘图就是视频接口实际吃的首帧，它的漂移就是链路漂移
            try:
                size = _download(first_frame_url, refit_png)
                print(f"[A] 重绘图已下载（{size} 字节）→ {refit_png}")
                mae_refit = _mae(original_frame, refit_png)
                if mae_refit is not None:
                    print(f"[A] MAE(原图, 重绘图) = {mae_refit:.2f}（0~255，"
                          f"实测记录口径：首帧锁命中 <7，对照组 70+）")
            except Exception as exc:
                print(f"[A] 重绘图下载/度量失败（不影响后续）：{exc}")

    content = []
    if first_frame_url:
        content.append({
            "type": "image_url",
            "image_url": {"url": first_frame_url},
            "role": "first_frame",
        })
    content.append({"type": "text", "text": "固定机位，画面轻微呼吸感，镜头缓缓推近"})

    payload = {
        "model": "wan3.0-video",
        "content": content,
        # 最便宜档：480P × 2s ≈ ¥0.6
        "resolution": "480P",
        "duration": 2,
        "ratio": "16:9",
    }
    mode = "图生视频" if first_frame_url else "文生视频"
    if not _confirm(f"{mode}：创建任务并轮询到完成", "¥0.6（480P ¥0.3/秒 × 2 秒，失败不扣费）"):
        print("已取消。")
        return 0

    url = _API_BASE + "/videos/generations"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=_auth_headers(api_key, url),
        method="POST",
    )
    print(f"[B] POST {url}")
    print(f"[B] 请求体：{json.dumps(payload, ensure_ascii=False)[:400]}")
    try:
        created = _http_json(req)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        print(f"[B] 创建失败 HTTP {exc.code}：{detail}")
        return 1

    print(f"[B] 创建响应：{json.dumps(created, ensure_ascii=False)}")
    task_id = str(created.get("id") or "").strip()
    if not task_id:
        print("[B] 未返回任务 id，终止。")
        return 1

    # ---- 轮询 ----
    # 实测轮询端点是 /v1/videos/{id}（第一轮真跑：拼成 /v1/videos/generations/{id}
    # 连续 404 到超时），与创建端点不是同一个前缀
    poll_url = f"{_API_BASE}/videos/{task_id}"
    deadline = time.monotonic() + _POLL_TIMEOUT
    final = None
    while time.monotonic() < deadline:
        poll_req = urllib.request.Request(
            poll_url, headers=_auth_headers(api_key, poll_url), method="GET"
        )
        try:
            doc = _http_json(poll_req)
        except Exception as exc:
            print(f"[B] 轮询异常（继续重试）：{exc}")
            time.sleep(_POLL_INTERVAL)
            continue
        status = str(doc.get("status") or "")
        print(f"[B] 轮询 status={status}")
        if status.lower() in ("success", "succeeded", "completed"):
            final = doc
            break
        if status.lower() in ("failed", "error", "cancelled", "canceled"):
            print(f"[B] 任务失败：{json.dumps(doc, ensure_ascii=False)[:500]}（失败不扣费）")
            return 1
        time.sleep(_POLL_INTERVAL)

    if not final:
        print(f"[B] 超过 {_POLL_TIMEOUT:.0f}s 未完成。任务 {task_id} 可能仍在跑，"
              f"稍后可到控制台查看，不要立刻重复提交。")
        return 1

    print(f"[B] 完成响应：{json.dumps(final, ensure_ascii=False)}")
    video_url = str((final.get("result") or {}).get("url") or "")
    if not video_url:
        print("[B] 完成 but 无 result.url，终止。")
        return 1

    # ---- 下载 ----
    out = os.path.join(run_dir, f"video_{task_id}.mp4")
    with urllib.request.urlopen(video_url, timeout=120) as resp:
        blob = resp.read()
    with open(out, "wb") as handle:
        handle.write(blob)
    head = blob[:12].hex()
    print(f"[B] 下载 {len(blob)} 字节 → {out}")
    print(f"[B] 文件头 hex：{head}")
    print(f"[B] 判定4/5：{'ftyp 在 4-8 字节 → MP4 成立' if blob[4:8] == b'ftyp' else '不是 MP4！需检查'}")

    billing = final.get("billing") or {}
    cost = billing.get("total_cost_cny") if isinstance(billing, dict) else None
    if cost is not None:
        print(f"[B] 实际费用：¥{cost}（完成响应 billing.total_cost_cny）")

    # 保真度量 2：视频首帧 vs 原图 / vs 重绘图（首帧锁是否命中的直接证据）
    if original_frame:
        frame_png = os.path.join(run_dir, "video_first_frame.png")
        if _extract_first_frame(out, frame_png):
            mae_a = _mae(original_frame, frame_png)
            mae_b = _mae(refit_png, frame_png) if os.path.isfile(refit_png) else None
            if mae_a is not None:
                print(f"[B] MAE(原图, 视频首帧) = {mae_a:.2f}")
            if mae_b is not None:
                print(f"[B] MAE(重绘图, 视频首帧) = {mae_b:.2f}（视频吃的是重绘图，这条才是首帧锁）")
            print(f"[B] 视频首帧已存 → {frame_png}")
    print()
    print("全部阶段完成。请把上面输出对照脚本头部的判定标准逐条核对。")
    return 0


def main() -> int:
    global AUTO_YES
    parser = argparse.ArgumentParser(description="TokenRhythm wan3.0-video 探针")
    parser.add_argument("--frame", default=None, help="可选：本地首帧图片路径（jpg/png，触发阶段 A）")
    parser.add_argument("--yes", action="store_true",
                        help="非交互：自动确认所有付费阶段（授权责任由调用方承担）")
    args = parser.parse_args()
    AUTO_YES = args.yes
    try:
        return probe(args.frame)
    except KeyboardInterrupt:
        print("\n用户中止。")
        return 130


if __name__ == "__main__":
    sys.exit(main())

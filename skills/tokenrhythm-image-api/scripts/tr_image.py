#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TokenRhythm（基元律动）生图 / 垫图改图 命令行工具。

零第三方依赖：只用 Python 3 标准库（urllib / json / argparse），
适合随 Skill 一起分发 —— 对方只要装了 Python 3，填个 Key 就能跑。

用法：
    python tr_image.py gen  -p "一只戴墨镜的橘猫，赛博朋克霓虹背景"
    python tr_image.py gen  -p "雪山日出" -s 1280x720 -m wan2.7-image -n 2 -o ./out
    python tr_image.py edit -p "把背景换成雪景" -i ./cat.png
    python tr_image.py gen  --help

Key 配置（优先级从高到低）：
    1) 命令行   --api-key sk_tr_xxx
    2) 环境变量 TOKENRHYTHM_API_KEY
    3) 本文件顶部 API_KEY 常量  ← 分发时让使用者填这里最省事
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime

# ============================================================================
# ★★★ 使用者唯一需要改的地方：把 Key 填进引号里 ★★★
# 获取方式：登录 tokenrhythm.studio → API Keys → 新建 → 复制完整 Key
# ============================================================================
API_KEY = ""
BASE_URL = "https://tokenrhythm.studio"
# ============================================================================

SIZES = ("1024x1024", "1280x720", "720x1280", "1280x1280", "1024x1792", "1792x1024")
MODELS = ("qwen-image-2.0", "wan2.7-image")
DEFAULT_TIMEOUT = 300  # 秒，生图偶发慢，别设太短


def _setup_stdout() -> None:
    """沿用环境自身的编码（Windows 中文环境是 GBK、Linux 是 UTF-8），
    只把编码错误降级为替换字符，避免个别特殊符号直接抛 UnicodeEncodeError。
    注意：不要强制改 encoding —— Windows 控制台/管道本来按 GBK 解码，强改 UTF-8 反而乱码。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass


def fail(msg: str, code: int = 1) -> "None":
    print(f"[ERROR] {msg}", file=sys.stderr)
    sys.exit(code)


def resolve_key(cli_key) -> str:
    key = (cli_key or os.environ.get("TOKENRHYTHM_API_KEY") or API_KEY).strip()
    if not key:
        fail(
            "未配置 API Key。三选一：\n"
            "  1) 编辑本文件，把顶部 API_KEY = \"\" 填成你的 Key\n"
            "  2) 设置环境变量 TOKENRHYTHM_API_KEY\n"
            "  3) 命令行加 --api-key sk_tr_xxx",
            code=2,
        )
    return key


def _send(req: urllib.request.Request, timeout: int):
    """发请求并统一处理 HTTP 错误，返回 (status, body_bytes)。"""
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except urllib.error.URLError as exc:
        fail(f"网络请求失败（检查网络/代理）：{exc.reason}", code=3)


def _decode(raw: bytes):
    text = raw.decode("utf-8", errors="replace")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"_raw": text[:800]}


def post_json(url: str, payload: dict, key: str, timeout: int):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "application/json",
        },
    )
    status, raw = _send(req, timeout)
    return status, _decode(raw)


def post_multipart(url: str, fields: dict, files: dict, key: str, timeout: int):
    """手搓 multipart/form-data，避免依赖 requests。"""
    boundary = "----TRBoundary" + uuid.uuid4().hex
    buf = bytearray()
    for name, value in fields.items():
        if value in (None, ""):
            continue
        buf += f"--{boundary}\r\n".encode()
        buf += f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
        buf += str(value).encode("utf-8") + b"\r\n"
    for name, (filename, content, ctype) in files.items():
        buf += f"--{boundary}\r\n".encode()
        buf += (
            f'Content-Disposition: form-data; name="{name}"; '
            f'filename="{filename}"\r\n'
        ).encode()
        buf += f"Content-Type: {ctype}\r\n\r\n".encode()
        buf += content + b"\r\n"
    buf += f"--{boundary}--\r\n".encode()

    req = urllib.request.Request(
        url,
        data=bytes(buf),
        method="POST",
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Accept": "application/json",
        },
    )
    status, raw = _send(req, timeout)
    return status, _decode(raw)


def check_response(status: int, payload, action: str):
    if status == 200:
        return payload
    detail = None
    if isinstance(payload, dict):
        detail = payload.get("message") or payload.get("error") or payload.get("_raw")
        detail = detail or json.dumps(payload, ensure_ascii=False)[:500]
    if status == 401:
        fail(f"{action} 失败 HTTP 401：Key 无效或已删除。响应：{detail}", code=4)
    if status == 429:
        fail(f"{action} 失败 HTTP 429：触发限流，稍后重试。响应：{detail}", code=5)
    fail(f"{action} 失败 HTTP {status}：{detail}", code=6)


def extract_items(payload: dict) -> list:
    """兼容几种返回形态，取出图片条目列表。"""
    node = payload.get("data", payload)
    if isinstance(node, dict):
        node = node.get("data") or node.get("images") or node
        if isinstance(node, dict):
            node = [node]
    if isinstance(node, str):
        node = [node]
    return node if isinstance(node, list) else []


def save_image(url: str, out_dir: str, prefix: str, index: int, timeout: int) -> str:
    ext = os.path.splitext(urllib.parse.urlparse(url).path)[1].lower()
    if ext not in (".png", ".jpg", ".jpeg", ".webp"):
        ext = ".png"
    dest = os.path.join(out_dir, f"{prefix}-{index}{ext}")
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp, open(dest, "wb") as fh:
            fh.write(resp.read())
    except (urllib.error.URLError, OSError) as exc:
        fail(f"图片下载失败（链接 24 小时过期，可重新生成）：{exc}", code=7)
    return os.path.abspath(dest)


def save_b64(b64: str, out_dir: str, prefix: str, index: int) -> str:
    import base64

    dest = os.path.join(out_dir, f"{prefix}-{index}.png")
    with open(dest, "wb") as fh:
        fh.write(base64.b64decode(b64))
    return os.path.abspath(dest)


def collect_files(payload: dict, out_dir: str, prefix: str, timeout: int) -> list:
    files = []
    for i, item in enumerate(extract_items(payload), start=1):
        if isinstance(item, str):
            files.append(save_image(item, out_dir, prefix, i, timeout))
        elif isinstance(item, dict):
            if item.get("url"):
                files.append(save_image(item["url"], out_dir, prefix, i, timeout))
            elif item.get("b64_json"):
                files.append(save_b64(item["b64_json"], out_dir, prefix, i))
    if not files:
        fail(
            "接口返回成功但没有图片数据，原始响应："
            + json.dumps(payload, ensure_ascii=False)[:500],
            code=8,
        )
    return files


def read_prompt(args) -> str:
    if args.prompt_file:
        try:
            with open(args.prompt_file, "r", encoding="utf-8") as fh:
                text = fh.read().strip()
        except OSError as exc:
            fail(f"读取提示词文件失败：{exc}", code=2)
    else:
        text = (args.prompt or "").strip()
    if not text:
        fail("缺少提示词：用 -p/--prompt 直接传，或 --prompt-file 指定 UTF-8 文本文件", code=2)
    return text


def report(files, model, size, payload, as_json):
    if as_json:
        print(
            json.dumps(
                {
                    "ok": True,
                    "files": files,
                    "model": model,
                    "size": size,
                    "cost_cny": payload.get("cost_cny"),
                    "trace_id": payload.get("trace_id"),
                },
                ensure_ascii=True,  # 纯 ASCII 转义：管道/重定向场景下不受 GBK/UTF-8 差异影响
                indent=2,
            )
        )
        return
    print(f"[OK] 生成成功（model={model} size={size or '默认'}）")
    for path in files:
        print(f"     {path}")
    extras = []
    if payload.get("cost_cny") is not None:
        extras.append(f"费用 {payload['cost_cny']} 元")
    if payload.get("trace_id"):
        extras.append(f"trace_id={payload['trace_id']}")
    if extras:
        print("     " + "  ".join(extras))
    print("     提示：OSS 链接 24 小时过期，图片已落盘，直接展示本地文件即可。")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="tr_image.py",
        description="TokenRhythm（基元律动）文生图 / 垫图改图（纯标准库）",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-p", "--prompt", help="提示词 / 改图指令")
    common.add_argument(
        "--prompt-file", help="从 UTF-8 文本文件读取提示词（提示词很长时更稳）"
    )
    common.add_argument(
        "-m", "--model", default=MODELS[0], choices=MODELS,
        help="模型，默认 qwen-image-2.0",
    )
    common.add_argument(
        "-s", "--size", default=None, choices=SIZES,
        help="输出尺寸；gen 默认 1024x1024，edit 不传则沿用原图",
    )
    common.add_argument("-o", "--out", default=None, help="保存目录，默认当前目录")
    common.add_argument("--api-key", default=None, help="临时覆盖 API Key")
    common.add_argument("--base-url", default=BASE_URL, help="自定义站点地址")
    common.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="超时秒数")
    common.add_argument("--json", action="store_true", help="输出机器可读 JSON")

    g = sub.add_parser("gen", parents=[common], help="文生图")
    g.add_argument("-n", "--n", type=int, default=1, help="生成张数 1-4，每张 0.20 元")

    e = sub.add_parser("edit", parents=[common], help="垫图改图")
    e.add_argument("-i", "--image", required=True, help="原图路径（≤10MB，jpg/png）")
    return p


def cmd_gen(args) -> None:
    if not 1 <= args.n <= 4:
        fail("-n 取值范围 1-4", code=2)
    key = resolve_key(args.api_key)
    prompt = read_prompt(args)
    out_dir = os.path.abspath(args.out or os.getcwd())
    os.makedirs(out_dir, exist_ok=True)

    size = args.size or "1024x1024"
    body = {
        "model": args.model,
        "prompt": prompt,
        "n": args.n,
        "size": size,
        "response_format": "url",
    }

    status, payload = post_json(
        args.base_url.rstrip("/") + "/v1/images/generations", body, key, args.timeout
    )
    payload = check_response(status, payload, "文生图")
    prefix = "tr-" + datetime.now().strftime("%Y%m%d-%H%M%S")
    files = collect_files(payload, out_dir, prefix, args.timeout)
    report(files, args.model, size, payload, args.json)


def cmd_edit(args) -> None:
    key = resolve_key(args.api_key)
    prompt = read_prompt(args)
    img_path = os.path.abspath(args.image)
    if not os.path.isfile(img_path):
        fail(f"原图不存在：{img_path}", code=2)
    size_mb = os.path.getsize(img_path) / 1024 / 1024
    if size_mb > 10:
        fail(f"原图 {size_mb:.1f}MB 超过 10MB 上限，请先压缩再试", code=2)
    out_dir = os.path.abspath(args.out or os.getcwd())
    os.makedirs(out_dir, exist_ok=True)

    ext = os.path.splitext(img_path)[1].lower()
    ctype = "image/png" if ext == ".png" else "image/jpeg"
    with open(img_path, "rb") as fh:
        content = fh.read()

    fields = {"model": args.model, "prompt": prompt}
    if args.size:
        fields["size"] = args.size
    files = {"image": (f"image{ext if ext in ('.png', '.jpg', '.jpeg') else '.png'}", content, ctype)}

    status, payload = post_multipart(
        args.base_url.rstrip("/") + "/v1/images/edits",
        fields,
        files,
        key,
        args.timeout,
    )
    payload = check_response(status, payload, "垫图改图")
    prefix = "tr-edit-" + datetime.now().strftime("%Y%m%d-%H%M%S")
    saved = collect_files(payload, out_dir, prefix, args.timeout)
    report(saved, args.model, args.size, payload, args.json)


def main() -> None:
    _setup_stdout()
    args = build_parser().parse_args()
    if args.cmd == "gen":
        cmd_gen(args)
    else:
        cmd_edit(args)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tools/build_embed.py —— 组装自包含单 HTML（SPEC §5）

流程：
  1. esbuild 打包 src/main.js → dist/bundle.js（IIFE / minify / es2019）
  2. 读 assets/poster.jpg → data:image/jpeg;base64,... ；assets/clip.mp4 → 纯 base64
  3. 生成 window.ASSETS = { poster, clip } 片段
  4. bundle 内所有 `</script` 转义成 `<\\/script`（否则 HTML 解析会在此处断裂）
  5. 用 /*__ASSETS__*/ 与 /*__BUNDLE__*/ 两个占位符替换 tools/template.html
  6. 写出 dist/pelican-bicycle-3d.html，打印最终体积与各部分占比

CSP 说明：媒体一律以 base64 内联，运行时由 main.js 用 atob → Uint8Array → Blob →
URL.createObjectURL 转成 blob URL，绝不 fetch(data:)，模板内也无任何外部域名。
"""

import base64
import os
import shutil
import subprocess
import sys

# 工程根 = 本脚本所在目录的上一级
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATE = os.path.join(ROOT, "tools", "template.html")
BUNDLE = os.path.join(ROOT, "dist", "bundle.js")
POSTER = os.path.join(ROOT, "assets", "poster.jpg")
CLIP = os.path.join(ROOT, "assets", "clip.mp4")
OUTPUT = os.path.join(ROOT, "dist", "pelican-bicycle-3d.html")

ESBUILD_ARGS = [
    "src/main.js", "--bundle", "--minify", "--format=iife",
    "--target=es2019", "--outfile=dist/bundle.js",
]


def resolve_esbuild_cmd():
    """定位 esbuild：优先 npx --no-install，其次本地 node_modules 里的可执行文件。

    Windows 下 CreateProcess 不认 .cmd，必须用 shutil.which() 拿到带扩展名的真实路径。
    """
    npx = shutil.which("npx")
    if npx:
        return [npx, "--no-install", "esbuild"] + ESBUILD_ARGS

    local = os.path.join(ROOT, "node_modules", "esbuild", "bin", "esbuild")
    if os.path.exists(local):
        node = shutil.which("node")
        if node:
            return [node, local] + ESBUILD_ARGS
        return [local] + ESBUILD_ARGS

    return None


def run_esbuild():
    """执行打包；失败时原样打印子进程输出并以非 0 退出。"""
    cmd = resolve_esbuild_cmd()
    if cmd is None:
        sys.stderr.write("[build] 未找到 esbuild（既无 npx，也无 node_modules/esbuild）\n")
        return 1

    print("[build] esbuild: " + " ".join(cmd))
    try:
        # 显式指定 utf-8 + errors=replace：Windows 中文环境下默认 GBK 解码 esbuild/npx
        # 的非 GBK 输出会在读取线程里抛 UnicodeDecodeError（有噪声且可能丢日志）
        proc = subprocess.run(cmd, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              encoding="utf-8", errors="replace")
    except OSError as exc:
        sys.stderr.write("[build] 启动 esbuild 失败：%s\n" % exc)
        return 1

    if proc.stdout:
        sys.stdout.write(proc.stdout)
    if proc.stderr:
        sys.stderr.write(proc.stderr)

    if proc.returncode != 0:
        sys.stderr.write("[build] esbuild 退出码 %d\n" % proc.returncode)
        return 1
    return 0


def read_text(path):
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def read_b64(path):
    with open(path, "rb") as fh:
        return base64.b64encode(fh.read()).decode("ascii")


def mib(n):
    return n / 1024.0 / 1024.0


def main():
    # ---- 1. 打包 ----
    code = run_esbuild()
    if code != 0:
        return code

    for path in (TEMPLATE, BUNDLE, POSTER, CLIP):
        if not os.path.exists(path):
            sys.stderr.write("[build] 缺少输入文件：%s\n" % path)
            return 1

    # ---- 2. 素材 → base64 ----
    poster_b64 = read_b64(POSTER)
    clip_b64 = read_b64(CLIP)

    # ---- 3. window.ASSETS 片段 ----
    assets_js = (
        "/* 内联素材：海报为 data: 图片（CSP 允许 data: image）；"
        "视频为纯 base64，运行时转 blob URL */\n"
        'window.ASSETS = { poster: "data:image/jpeg;base64,%s", clip: "%s" };\n'
        % (poster_b64, clip_b64)
    )

    # ---- 4. bundle：转义 </script ----
    bundle_src = read_text(BUNDLE)
    escaped = bundle_src.count("</script")
    bundle_src = bundle_src.replace("</script", "<\\/script")

    # ---- 5. 占位符替换 ----
    tpl = read_text(TEMPLATE)
    for ph in ("/*__ASSETS__*/", "/*__BUNDLE__*/"):
        if ph not in tpl:
            sys.stderr.write("[build] 模板缺少占位符 %s\n" % ph)
            return 1

    html = tpl.replace("/*__ASSETS__*/", assets_js).replace("/*__BUNDLE__*/", bundle_src)
    if "/*__ASSETS__*/" in html or "/*__BUNDLE__*/" in html:
        sys.stderr.write("[build] 占位符替换不完整\n")
        return 1

    # ---- 6. 写出 ----
    dist_dir = os.path.dirname(OUTPUT)
    if not os.path.isdir(dist_dir):
        os.makedirs(dist_dir)
    with open(OUTPUT, "w", encoding="utf-8", newline="") as fh:
        fh.write(html)

    # ---- 统计与占比 ----
    size_html = os.path.getsize(OUTPUT)
    size_poster = len(poster_b64)
    size_clip = len(clip_b64)
    size_bundle = len(bundle_src)
    size_tpl = len(html.encode("utf-8")) - size_poster - size_clip - size_bundle
    total = float(size_html) or 1.0

    def line(label, n):
        print("  %-22s %9.1f KB  %5.1f%%" % (label, n / 1024.0, n * 100.0 / total))

    print("[build] 输出：%s" % OUTPUT)
    print("[build] 最终体积：%.2f MB（%d 字节）" % (mib(size_html), size_html))
    print("[build] 体积构成：")
    line("bundle.js（three+代码）", size_bundle)
    line("clip.mp4 → base64", size_clip)
    line("poster.jpg → base64", size_poster)
    line("template（HTML/CSS）", size_tpl)
    if escaped:
        print("[build] 已转义 %d 处 `</script` → `<\\/script`" % escaped)
    print("[build] OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())

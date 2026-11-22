#!/usr/bin/env python3

import os
import sys


def main():
    if len(sys.argv) < 2:
        print("用法: wrap_preview.py <section.html> [output.html]")
        sys.exit(1)
    src = sys.argv[1]
    if not os.path.isfile(src):
        print(f"✗ 找不到文件: {src}")
        sys.exit(1)

    content = open(src, encoding="utf-8").read().strip()
    tpl_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "..", "assets", "preview-template.html")
    tpl = open(tpl_path, encoding="utf-8").read()

    title = os.path.splitext(os.path.basename(src))[0]
    out_html = tpl.replace("{{TITLE}}", title).replace("<!--GZH_CONTENT-->", content)

    out = sys.argv[2] if len(sys.argv) > 2 else os.path.splitext(src)[0] + "_预览.html"
    open(out, "w", encoding="utf-8").write(out_html)
    print(f"✓ 已生成带「复制」按钮的预览页: {out}")
    print("  用浏览器打开它，点右上角「复制到公众号」，再去公众号编辑器 Ctrl/⌘+V 粘贴。")


if __name__ == "__main__":
    main()

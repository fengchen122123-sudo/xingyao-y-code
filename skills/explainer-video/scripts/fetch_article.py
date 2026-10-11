# -*- coding: utf-8 -*-
"""Fetch a WeChat MP article and dump its main text + image list to files."""
import html
import os
import re
import sys
import urllib.request

URL = sys.argv[1] if len(sys.argv) > 1 else "https://mp.weixin.qq.com/s/RkXJcAyjE323JtIaIbPmTg"
STEM = sys.argv[2] if len(sys.argv) > 2 else r"D:\ycodesso\article"
OUT_TEXT = STEM + ".txt"
OUT_META = STEM + "_meta.txt"
OUT_IMGS = STEM + "_images.txt"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=40) as resp:
        return resp.read().decode("utf-8", "ignore")


def extract_div(doc, marker):
    """Return the whole <div ...> block whose opening tag contains `marker`."""
    pos = doc.index(marker)
    start = doc.rindex("<div", 0, pos)
    depth = 0
    for m in re.finditer(r"<div\b[^>]*>|</div\s*>", doc[start:]):
        depth += -1 if m.group(0).startswith("</") else 1
        if depth == 0:
            return doc[start:start + m.end()]
    return ""


def to_text(fragment):
    s = re.sub(r"<(script|style)\b.*?</\1>", "", fragment, flags=re.I | re.S)
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"</(p|section|h[1-6]|li|blockquote|div|figure)\s*>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s)
    s = s.replace("\u200b", "").replace("\xa0", " ").replace("\ufeff", "")
    lines = [ln.strip() for ln in s.split("\n")]
    out = []
    for ln in lines:
        if ln:
            out.append(ln)
        elif out and out[-1] != "":
            out.append("")
    return "\n".join(out).strip()


def pick(doc, pattern, default=""):
    m = re.search(pattern, doc, re.S)
    return html.unescape(m.group(1)).strip() if m else default


def main():
    doc = fetch(URL)
    title = pick(doc, r'id="activity-name"[^>]*>(.*?)</h1>')
    author = pick(doc, r'var nickname\s*=\s*htmlDecode\("(.*?)"\)')
    pub = pick(doc, r'var ct\s*=\s*"(\d+)"')
    if pub.isdigit():
        import datetime
        pub = datetime.datetime.fromtimestamp(int(pub)).strftime("%Y-%m-%d %H:%M")

    body = extract_div(doc, 'id="js_content"')
    text = to_text(body)
    imgs = re.findall(r'data-src="(https?://[^"]+)"', body)

    with open(OUT_TEXT, "w", encoding="utf-8") as f:
        f.write(text)
    with open(OUT_META, "w", encoding="utf-8") as f:
        f.write("title: %s\nauthor: %s\npublish: %s\nchars: %d\nimages: %d\n"
                % (title, author, pub, len(text), len(imgs)))
    with open(OUT_IMGS, "w", encoding="utf-8") as f:
        f.write("\n".join(imgs))

    print("fetch ok, html chars = %d" % len(doc))
    print("body chars = %d, images = %d" % (len(text), len(imgs)))
    print("text -> %s" % OUT_TEXT)


if __name__ == "__main__":
    main()
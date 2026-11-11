# -*- coding: utf-8 -*-
"""竖屏讲解视频画面渲染器。

用法: python render_video.py <工作目录> [full|bench]
  读 <工作目录>/script.json 与 timeline.json，逐帧渲染并直接管道给 ffmpeg 编码，
  不落盘中间 PNG。bench = 只渲染前 60 帧测速；full = 全量渲染出 video_silent.mp4。
  版式由每个分镜的 visual.type 决定（见 reference/script-schema.md）。
  字体为 Windows 系统字体，换平台需改 FBD/FRG/FMO 三个常量。
"""
import sys, os, json, time, math, subprocess
from bisect import bisect_right
from PIL import Image, ImageDraw, ImageFont, ImageFilter
import numpy as np

W, H = 720, 1280
MX = 56
FPS = 30
_ARGS = [a for a in sys.argv[1:] if not a.startswith('-')]
MODE = 'full' if 'full' in _ARGS else 'bench'
_DIRS = [a for a in _ARGS if a not in ('full', 'bench')]
BASE = os.path.abspath(_DIRS[0]) if _DIRS else os.getcwd()
OUT = os.path.join(BASE, 'video_silent.mp4')
SCRIPT = os.path.join(BASE, 'script.json')
TL = os.path.join(BASE, 'timeline.json')
FFMPEG_LOG = os.path.join(BASE, 'ffmpeg_render.log')

FBD = r'C:\Windows\Fonts\msyhbd.ttc'
FRG = r'C:\Windows\Fonts\msyh.ttc'
FMO = r'C:\Windows\Fonts\consola.ttf'

FG = (255, 255, 255, 255)
ACC = {'blue': (0x38, 0xBD, 0xF8), 'purple': (0xA8, 0x55, 0xF7),
       'teal': (0x2D, 0xD4, 0xBF), 'amber': (0xFB, 0xBF, 0x24)}

# 卡片内边距区
CX0, CY0, CX1, CY1 = MX, 340, W - MX, 1000
IX, IY, IW, IH = MX + 40, 380, W - 2 * (MX + 40), 580  # 96,380,528,580

_fc = {}


def fnt(path, size):
    k = (path, size)
    if k not in _fc:
        _fc[k] = ImageFont.truetype(path, size)
    return _fc[k]


_meas = ImageDraw.Draw(Image.new('RGBA', (4, 4)))


def ti(text, font, fill):
    """渲染一段文字为紧凑 RGBA 精灵。"""
    l, t, r, b = _meas.textbbox((0, 0), text, font=font)
    img = Image.new('RGBA', (max(1, r - l) + 4, max(1, b - t) + 6), (0, 0, 0, 0))
    ImageDraw.Draw(img).text((2 - l, 3 - t), text, font=font, fill=fill)
    return img


CJK_END = '，。、；：？！“”‘’（）《》—…·'


def _tokens(text):
    toks, cur = [], ''
    for ch in text:
        if ord(ch) > 0x2E7F or ch in CJK_END or ch.isspace():
            if cur:
                toks.append(cur); cur = ''
            if not ch.isspace():
                toks.append(ch)
        else:
            cur += ch
    if cur:
        toks.append(cur)
    return toks


def wrap(text, font, maxw):
    lines, line = [], ''
    for tk in _tokens(text):
        trial = line + tk
        if line and font.getlength(trial) > maxw:
            lines.append(line); line = tk
        else:
            line = trial
    if line:
        lines.append(line)
    return lines


def pack(sprite):
    """RGBA -> (RGB, L mask)"""
    r, g, b, a = sprite.split()
    return Image.merge('RGB', (r, g, b)), a


def make_bg():
    top = np.array([0x0B, 0x10, 0x20], np.float32)
    bot = np.array([0x12, 0x18, 0x2E], np.float32)
    ramp = (np.arange(H, dtype=np.float32) / (H - 1))[:, None]
    col = top[None, :] * (1 - ramp) + bot[None, :] * ramp          # (H,3)
    arr = np.repeat(col[:, None, :], W, axis=1)
    rng = np.random.default_rng(7)
    arr = np.clip(arr + rng.normal(0, 2.2, (H, W, 1)), 0, 255).astype(np.uint8)
    img = Image.fromarray(arr, 'RGB')
    ov = Image.new('RGBA', (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(ov)
    d.text((MX, 64), 'AI 小白系列 · DAY 01', font=fnt(FMO, 20), fill=(0x7C, 0x8A, 0xA5, 255))
    d.rounded_rectangle((MX, 1220, MX + 608, 1224), 2, fill=(255, 255, 255, 30))
    img.paste(ov, (0, 0), ov.split()[3])
    return img


def make_blob(acc):
    n = 480
    c = n / 2.0
    y, x = np.mgrid[0:n, 0:n]
    r = np.sqrt((x - c) ** 2 + (y - c) ** 2) / c
    a = np.clip(1.0 - r, 0, 1) ** 2.2 * 34.0
    m = Image.fromarray(a.astype(np.uint8), 'L')
    sp = Image.new('RGBA', (n, n), acc + (0,))
    sp.putalpha(m)
    return pack(sp)


def sub(card, sprite, xy):
    card.alpha_composite(sprite, xy)


def build_scene(sc):
    vis = sc['visual']
    typ = vis['type']
    lines = list(vis.get('lines') or [])
    acc = ACC[vis['meta'].get('accent', 'blue')]
    card = Image.new('RGBA', (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(card)

    # ---- 标题区（heading + accent 下划线）----
    hs, hl = 52, None
    while True:
        f = fnt(FBD, hs)
        hl = wrap(vis['heading'], f, W - 2 * MX)
        if len(hl) <= 2 or hs <= 40:
            break
        hs -= 4
    if len(hl) > 2:
        hl = hl[:2]
    y = 150
    for ln in hl:
        sub(card, ti(ln, f, FG), (MX, y))
        y += int(hs * 1.24)
    d.rounded_rectangle((MX, y + 20, MX + 72, y + 26), 3, fill=acc + (255,))

    # ---- 卡片底板 ----
    d.rounded_rectangle((CX0, CY0, CX1, CY1), 24, fill=(255, 255, 255, 15),
                        outline=(255, 255, 255, 26), width=1)

    blocks = []  # (sprite, x, y)

    def sqrow(text, font, fill, hh=64, sq=10):
        img = Image.new('RGBA', (IW, hh), (0, 0, 0, 0))
        t = ti(text, font, fill)
        img.alpha_composite(t, (28, max(0, (hh - t.height) // 2)))
        img.alpha_composite(Image.new('RGBA', (sq, sq), acc + (255,)), (0, (hh - sq) // 2))
        return img

    def kv(text):
        for sep in ('：', ':'):
            if sep in text:
                a, b = text.split(sep, 1)
                return a, b
        return text, ''

    def centered(sprite, y):
        blocks.append((sprite, IX + (IW - sprite.width) // 2, y))

    # ---- 各 type ----
    if typ == 'title':
        centered(ti(lines[0], fnt(FBD, 64), FG), IY + 130)
        centered(ti(lines[1], fnt(FRG, 30), (255, 255, 255, 225)), IY + 252)
        words = [w.strip() for w in (lines[2] if len(lines) > 2 else '').split('/') if w.strip()]
        if words:
            cf = fnt(FBD, 26)
            wds = [int(cf.getlength(w)) + 48 for w in words]
            gap = 16
            tot = sum(wds) + gap * (len(words) - 1)
            cap = Image.new('RGBA', (tot, 52), (0, 0, 0, 0))
            dc = ImageDraw.Draw(cap)
            x = 0
            for w, wd in zip(words, wds):
                dc.rounded_rectangle((x, 0, x + wd, 52), 26, fill=acc + (52,), outline=acc + (150,), width=1)
                t = ti(w, cf, acc + (255,))
                cap.alpha_composite(t, (int(x + (wd - t.width) / 2), (52 - t.height) // 2))
                x += wd + gap
            centered(cap, IY + 384)

    elif typ == 'bullets':
        n = len(lines)
        rh = min(96, IH // max(1, n))
        y0 = IY + (IH - n * rh) // 2
        for i, ln in enumerate(lines):
            blocks.append((sqrow(ln, fnt(FRG, 30), (255, 255, 255, 235)), IX, y0 + i * rh))

    elif typ == 'terms':
        items = [kv(l) for l in lines[:3]]
        hh, gap = 150, 24
        y0 = IY + (IH - (len(items) * hh + (len(items) - 1) * gap)) // 2
        for i, (name, desc) in enumerate(items):
            sc_img = Image.new('RGBA', (IW, hh), (0, 0, 0, 0))
            ds = ImageDraw.Draw(sc_img)
            ds.rounded_rectangle((0, 0, IW - 1, hh - 1), 16, fill=(255, 255, 255, 10),
                                 outline=(255, 255, 255, 22), width=1)
            sc_img.alpha_composite(ti(name, fnt(FBD, 34), acc + (255,)), (24, 22))
            sc_img.alpha_composite(ti(desc, fnt(FRG, 26), (255, 255, 255, 205)), (24, 82))
            blocks.append((sc_img, IX, y0 + i * (hh + gap)))

    elif typ == 'compare':
        tf = fnt(FBD, 30)
        df = fnt(FRG, 24)
        ymid = IY + IH // 2
        for i in range(0, min(4, len(lines)), 2):
            ttl = lines[i]
            desc = lines[i + 1] if i + 1 < len(lines) else ''
            if i == 0:
                blocks.append((ti(ttl, tf, acc + (255,)), IX, IY + 40))
                blocks.append((ti(desc, df, (255, 255, 255, 210)), IX + 28, IY + 88))
            else:
                blocks.append((ti(ttl, tf, acc + (255,)), IX, ymid + 34))
                blocks.append((ti(desc, df, (255, 255, 255, 210)), IX + 28, ymid + 82))
        # 中间虚线分隔
        y = ymid + 4
        x = IX
        while x < IX + IW:
            d.rectangle((x, y, min(x + 10, IX + IW), y + 2), fill=(255, 255, 255, 46))
            x += 20

    elif typ == 'steps':
        no = vis['meta'].get('step_no', 1)
        bx0, by0 = IX, IY
        d.ellipse((bx0, by0, bx0 + 96, by0 + 96), fill=acc + (58,), outline=acc + (200,), width=2)
        nimg = ti(str(no), fnt(FBD, 44), acc + (255,))
        sub(card, nimg, (bx0 + 48 - nimg.width // 2, by0 + 48 - nimg.height // 2))
        ttl = ti(lines[0], fnt(FBD, 40), FG)
        sub(card, ttl, (bx0 + 120, by0 + 48 - ttl.height // 2))
        for i, ln in enumerate(lines[1:]):
            blocks.append((sqrow(ln, fnt(FRG, 30), (255, 255, 255, 230), hh=72), IX, IY + 150 + i * 78))

    elif typ == 'tip':
        d.rounded_rectangle((IX - 28, IY, IX - 22, IY + IH), 3, fill=acc + (235,))
        lf = fnt(FBD, 26)
        lw = int(lf.getlength('提示')) + 40
        d.rounded_rectangle((IX, IY - 4, IX + lw, IY + 42), 21, fill=acc + (52,))
        li = ti('提示', lf, acc + (255,))
        sub(card, li, (IX + (lw - li.width) // 2, IY - 4 + (46 - li.height) // 2))
        n = len(lines)
        rh = min(84, (IH - 60) // max(1, n))
        y0 = IY + 66
        for i, ln in enumerate(lines):
            blocks.append((sqrow(ln, fnt(FRG, 30), (255, 255, 255, 232), hh=min(76, rh)), IX, y0 + i * rh))

    elif typ == 'table':
        rows = [kv(l) for l in lines]
        rh = 88
        y0 = IY + (IH - len(rows) * rh) // 2
        for i, (name, desc) in enumerate(rows):
            if i % 2 == 1:
                d.rounded_rectangle((IX, y0 + i * rh, IX + IW, y0 + (i + 1) * rh - 6), 10,
                                    fill=(255, 255, 255, 10))
            rimg = Image.new('RGBA', (IW, rh), (0, 0, 0, 0))
            rimg.alpha_composite(ti(name, fnt(FBD, 30), acc + (255,)), (20, 26))
            dd = desc if len(desc) <= 12 else desc[:12] + '…'
            dt = ti(dd, fnt(FRG, 26), (255, 255, 255, 215))
            rimg.alpha_composite(dt, (200, 30))
            blocks.append((rimg, IX, y0 + i * rh))

    elif typ == 'quote':
        qi = ti('“', fnt(FBD, 300), acc + (34,))
        sub(card, qi, (IX - 24, IY - 70))
        for i, ln in enumerate(lines):
            if i == 0:
                centered(ti(ln, fnt(FBD, 44), FG), IY + 150)
            else:
                centered(ti(ln, fnt(FRG, 28), (255, 255, 255, 208)), IY + 250 + (i - 1) * 66)

    elif typ == 'outro':
        gm = Image.new('L', (620, 620), 0)
        ImageDraw.Draw(gm).ellipse((0, 0, 619, 619), fill=78)
        gm = gm.filter(ImageFilter.GaussianBlur(84))
        gl = Image.new('RGBA', (620, 620), acc + (0,))
        gl.putalpha(gm)
        card.alpha_composite(gl, (W - 430, 900))
        centered(ti(vis['heading'], fnt(FBD, 56), FG), IY + 70)
        for i, ln in enumerate(lines[:3]):
            centered(ti(ln, fnt(FRG, 28), (255, 255, 255, 215)), IY + 230 + i * 68)

    # ---- 字幕（静态，预渲染进底图）----
    sub_font = fnt(FRG, 28)
    sw = W - 2 * MX
    sl = wrap(sc['narration'], sub_font, sw)
    while len(sl) > 3 and sub_font.size > 22:
        sub_font = fnt(FRG, sub_font.size - 2)
        sl = wrap(sc['narration'], sub_font, sw)
    lh = int(sub_font.size * 1.45)
    y0 = 1030 + max(0, (150 - len(sl) * lh) // 2)
    for i, ln in enumerate(sl):
        t = ti(ln, sub_font, (255, 255, 255, 224))
        card.alpha_composite(t, (MX + (sw - t.width) // 2, y0 + i * lh))

    # ---- 缓存：card(裁剪) + blocks 打包 ----
    bbox = card.getbbox()
    card_c = card.crop(bbox)
    card_p = pack(card_c)
    blk = [(pack(sp), x, y) for sp, x, y in blocks]
    n = len(blk)
    dur = sc['duration']
    fades = []
    for i in range(n):
        st = 0.5 + (i / n) * (dur * 0.55) if n else 0.0
        fades.append((st, st + 0.25))
    settle = max([0.6] + [e for _, e in fades])

    # settled 静止帧（不含移动光斑/进度条）
    settled = bg.copy()
    settled.paste(card_p[0], (bbox[0], bbox[1]), card_p[1])
    for (rgb, mk), x, y in blk:
        settled.paste(rgb, (x, y), mk)

    return dict(sc=sc, card=card_p, cbox=bbox, blocks=blk, fades=fades, settle=settle,
                settled=settled, acc=acc, start=sc['start'], duration=dur)


def draw_progress(img, t, sc, rt):
    d = ImageDraw.Draw(img)
    p = max(0.0, min(1.0, t / TOTAL))
    w = max(3, int(608 * p))
    d.rounded_rectangle((MX, 1220, MX + w, 1224), 2, fill=sc['acc'])
    ps = max(0.0, min(1.0, rt / max(0.001, sc['duration'])))
    ws = max(3, int(608 * ps))
    a = sc['acc']
    lc = (min(255, a[0] + 80), min(255, a[1] + 75), min(255, a[2] + 65))
    d.rounded_rectangle((MX, 1219, MX + ws, 1225), 3, fill=lc)


def render_frame(f, cache, starts, blobcache):
    t = f / FPS
    i = bisect_right(starts, t) - 1
    if i < 0:
        i = 0
    elif i >= len(cache):
        i = len(cache) - 1
    sc = cache[i]
    rt = t - sc['start']
    if rt < 0:
        rt = 0.0
    if rt >= sc['settle']:
        img = sc['settled'].copy()
    else:
        img = bg.copy()
        ea = min(1.0, rt / 0.6)
        cdy = int(round(16 * (1 - ea)))
        crgb, cmk = sc['card']
        bx, by = sc['cbox'][0], sc['cbox'][1] + cdy
        if ea >= 1.0:
            img.paste(crgb, (bx, by), cmk)
        else:
            mk = cmk.point(lambda v: int(v * ea))
            img.paste(crgb, (bx, by), mk)
        for k, ((rgb, m), x, y) in enumerate(sc['blocks']):
            st, en = sc['fades'][k]
            if rt < st:
                continue
            a = min(1.0, (rt - st) / 0.25)
            dy = int(round(10 * (1 - a)))
            if a >= 1.0:
                img.paste(rgb, (x, y), m)
            else:
                img.paste(rgb, (x, y + dy), m.point(lambda v: int(v * a)))
    bkey = sc['acc']
    brgb, bmk = blobcache[bkey]
    cx = int(0.5 * W + 210 * math.sin(t * 2 * math.pi / 42.0)) - 240
    cy = int(0.45 * H + 300 * math.sin(t * 2 * math.pi / 63.0 + 1.1)) - 240
    img.paste(brgb, (cx, cy), bmk)
    draw_progress(img, t, sc, rt)
    return img, sc['sc']['id']


# ---------------- 主流程 ----------------
if __name__ == '__main__':
    mode = MODE
    script = json.load(open(SCRIPT, encoding='utf-8'))
    tl = json.load(open(TL, encoding='utf-8'))
    TOTAL = float(tl['total_duration'])
    frames_total = round(TOTAL * FPS)
    vis_by_id = {s['id']: s for s in script['scenes']}
    scenes = []
    for s in tl['scenes']:
        v = vis_by_id[s['id']]
        scenes.append(dict(id=s['id'], start=float(s['start']), end=float(s['end']),
                           duration=float(s['duration']), narration=v['narration'], visual=v['visual']))
    starts = [s['start'] for s in scenes]

    t0 = time.time()
    bg = make_bg()
    blobcache = {v: make_blob(v) for v in ACC.values()}
    cache = [build_scene(s) for s in scenes]
    print('prep %.1fs  frames_total=%d  total=%.3fs' % (time.time() - t0, frames_total, TOTAL), flush=True)

    if mode == 'bench':
        import io
        n = min(60, frames_total)
        sz = 0
        t1 = time.time()
        for f in range(n):
            img, sid = render_frame(f, cache, starts, blobcache)
            b = io.BytesIO()
            img.save(b, 'PNG', compress_level=1)
            sz += b.tell()
        el = time.time() - t1
        ms = el / n * 1000
        print('BENCH frames=%d  total=%.2fs  per_frame=%.1fms  est_full=%.2fmin  png_avg=%dKB'
              % (n, el, ms, ms * frames_total / 1000 / 60, sz / n / 1024), flush=True)
        sys.exit(0)

    cmd = ['ffmpeg', '-y', '-f', 'image2pipe', '-framerate', '30', '-i', '-', '-an',
           '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '20', '-pix_fmt', 'yuv420p',
           '-movflags', '+faststart', OUT]
    logf = open(FFMPEG_LOG, 'wb')
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=logf)
    t1 = time.time()
    try:
        for f in range(frames_total):
            img, sid = render_frame(f, cache, starts, blobcache)
            img.save(proc.stdin, 'PNG', compress_level=1)
            if (f + 1) % 500 == 0:
                el = time.time() - t1
                print('  frame %d/%d  %.1fs  scene=%d' % (f + 1, frames_total, el, sid), flush=True)
        proc.stdin.close()
    except Exception as e:
        print('PIPE ERROR at frame %d: %r' % (f, e), flush=True)
        try:
            proc.stdin.close()
        except Exception:
            pass
    rc = proc.wait()
    logf.close()
    el = time.time() - t1
    print('ffmpeg rc=%d  render+encode %.1fs (%.1f min)  avg %.1f ms/frame'
          % (rc, el, el / 60, el / frames_total * 1000), flush=True)
    if rc != 0:
        print(open(FFMPEG_LOG, encoding='utf-8', errors='replace').read()[-3000:], flush=True)
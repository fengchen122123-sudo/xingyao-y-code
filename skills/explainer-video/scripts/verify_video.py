# -*- coding: utf-8 -*-
"""讲解视频成品验收（只读，不改任何文件）。

用法:
    python verify_video.py <工作目录> [成品文件名]

不给文件名时自动挑目录下的 .mp4（跳过 video_silent.mp4 这类中间产物）。
全部检查通过退出码 0，任一失败退出码 1 并打印失败清单。

检查内容:
  1. 容器规格：视频 h264、分辨率与 script.json 声明一致；有音频流
  2. 时长：与 timeline.json 的 total_duration 相差 < 0.5 秒
  3. 音画落位：抽 3 个分镜起点必须有语音（max_volume > -60 dB），
     段间停顿点必须是静音（max_volume < -60 dB）
  4. 画面非空：抽 2 帧，非背景像素占比 > 3%
  5. 一致性：script.json 与 timeline.json 的分镜数一致，各段 duration 之和 = total_duration
"""
import json
import os
import re
import subprocess
import sys

FAILURES = []


def fail(msg):
    FAILURES.append(msg)
    print("  [FAIL] %s" % msg)


def ok(msg):
    print("  [ok]   %s" % msg)


def run(cmd):
    p = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="ignore")
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def probe(path):
    rc, out = run(["ffprobe", "-v", "error", "-show_entries",
                   "stream=codec_type,codec_name,width,height,r_frame_rate,nb_frames,channels,sample_rate",
                   "-show_entries", "format=duration,size", "-of", "json", path])
    if rc != 0:
        return None
    try:
        return json.loads(out)
    except ValueError:
        return None


def volume_at(path, t, dur=3.0):
    rc, out = run(["ffmpeg", "-hide_banner", "-ss", "%.3f" % t, "-t", "%.3f" % dur,
                   "-i", path, "-af", "volumedetect", "-f", "null", "NUL"])
    # ffmpeg 会先打印一次 n_samples: 0 的空统计，取最后一次出现的数值
    mean = re.findall(r"mean_volume:\s*(-?\d+\.?\d*)\s*dB", out)
    mx = re.findall(r"max_volume:\s*(-?\d+\.?\d*)\s*dB", out)
    return (float(mean[-1]) if mean else None), (float(mx[-1]) if mx else None)


def frame_busy(video, t, out_png):
    rc, _ = run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                 "-ss", "%.3f" % t, "-i", video, "-frames:v", "1", out_png])
    if rc != 0 or not os.path.isfile(out_png):
        return None
    from PIL import Image
    import numpy as np
    a = np.asarray(Image.open(out_png).convert("RGB")).astype(np.int16)
    d = np.abs(a - a[0, 0][None, None, :]).sum(axis=2)
    return round(float((d > 45).mean()) * 100, 2)


def pick_video(base, explicit):
    if explicit:
        p = explicit if os.path.isabs(explicit) else os.path.join(base, explicit)
        return p if os.path.isfile(p) else None
    cands = [f for f in sorted(os.listdir(base))
             if f.lower().endswith(".mp4") and f != "video_silent.mp4"]
    return os.path.join(base, cands[0]) if cands else None


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    base = os.path.abspath(sys.argv[1])
    explicit = sys.argv[2] if len(sys.argv) > 2 else ""

    video = pick_video(base, explicit)
    if not video:
        print("no candidate mp4 found in %s" % base)
        sys.exit(1)
    print("video: %s" % video)

    tl_path = os.path.join(base, "timeline.json")
    sc_path = os.path.join(base, "script.json")
    if not os.path.isfile(tl_path):
        print("missing timeline.json in %s" % base)
        sys.exit(1)
    with open(tl_path, encoding="utf-8") as f:
        tl = json.load(f)
    sc = None
    if os.path.isfile(sc_path):
        with open(sc_path, encoding="utf-8") as f:
            sc = json.load(f)

    # 1. container
    info = probe(video)
    if not info:
        fail("ffprobe failed on %s" % video)
        sys.exit(1)
    v = [s for s in info["streams"] if s.get("codec_type") == "video"]
    a = [s for s in info["streams"] if s.get("codec_type") == "audio"]
    dur = float(info["format"]["duration"])
    size = int(info["format"]["size"])
    want_w, want_h = (sc["size"] if sc and sc.get("size") else [720, 1280])
    if v and v[0]["codec_name"] == "h264" and int(v[0]["width"]) == int(want_w) \
            and int(v[0]["height"]) == int(want_h):
        ok("video h264 %sx%s %s fps nb_frames=%s"
           % (v[0]["width"], v[0]["height"], v[0]["r_frame_rate"], v[0].get("nb_frames")))
    else:
        fail("unexpected video stream (want %sx%s): %s" % (want_w, want_h, v[0] if v else None))
    if a and a[0]["codec_name"] in ("aac", "mp3"):
        ok("audio %s %s Hz ch=%s" % (a[0]["codec_name"], a[0].get("sample_rate"), a[0].get("channels")))
    else:
        fail("missing audio stream")
    print("       duration=%.3fs  size=%.2f MiB" % (dur, size / 1048576.0))

    # 2. duration vs timeline
    delta = abs(dur - float(tl["total_duration"]))
    (ok if delta < 0.5 else fail)("duration %.3f vs timeline %.3f (delta %.3f)"
                                  % (dur, tl["total_duration"], delta))

    # 3. audio placement
    scenes = tl["scenes"]
    for s in (scenes[0], scenes[len(scenes) // 2], scenes[-1]):
        mean, mx = volume_at(video, s["start"])
        if mx is not None and mx > -60:
            ok("scene %2d start=%7.3f speech mean=%s dB max=%s dB" % (s["id"], s["start"], mean, mx))
        else:
            fail("scene %2d start=%7.3f has no speech (mean=%s max=%s)" % (s["id"], s["start"], mean, mx))
    if len(scenes) > 1:
        g = scenes[1]
        gt = g["start"] + g["audio_duration"] + 0.15
        mean, mx = volume_at(video, gt, 0.25)
        if mx is not None and mx < -60:
            ok("gap after scene %d t=%.3f is silent (mean=%s max=%s dB)" % (g["id"], gt, mean, mx))
        else:
            fail("gap after scene %d t=%.3f not silent (mean=%s max=%s)" % (g["id"], gt, mean, mx))

    # 4. frame content
    pdir = os.path.join(base, "verify_frames")
    os.makedirs(pdir, exist_ok=True)
    for tag, s in (("head", scenes[0]), ("tail", scenes[-1])):
        mid = s["start"] + s["audio_duration"] / 2.0
        busy = frame_busy(video, mid, os.path.join(pdir, "verify_%s.png" % tag))
        if busy is not None and busy > 3.0:
            ok("frame at %.3f (scene %d) non-bg pixels = %.2f%%" % (mid, s["id"], busy))
        else:
            fail("frame at %.3f (scene %d) looks empty (busy=%s)" % (mid, s["id"], busy))

    # 5. consistency
    if sc is not None:
        if len(sc["scenes"]) == len(scenes):
            ok("scene count %d matches script and timeline" % len(scenes))
        else:
            fail("scene count mismatch: script=%d timeline=%d" % (len(sc["scenes"]), len(scenes)))
    total = sum(s["duration"] for s in scenes)
    if abs(total - float(tl["total_duration"])) < 0.01:
        ok("sum(duration)=%.3f == total_duration" % total)
    else:
        fail("sum(duration)=%.3f != total_duration=%s" % (total, tl["total_duration"]))

    print("\n=== summary ===")
    if FAILURES:
        print("FAILED %d check(s):" % len(FAILURES))
        for f in FAILURES:
            print("  - %s" % f)
        sys.exit(1)
    print("ALL CHECKS PASSED")
    sys.exit(0)


if __name__ == "__main__":
    main()
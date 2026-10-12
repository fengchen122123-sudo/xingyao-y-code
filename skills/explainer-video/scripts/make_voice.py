# -*- coding: utf-8 -*-
"""竖屏讲解视频配音器。

用法: python make_voice.py <工作目录>
  读 <工作目录>/script.json，逐段 TTS 合成，产出:
    audio/scene_NN.mp3 + audio/scene_NN.wav
    timeline.json  每段的真实音频时长、段间停顿、起止时间
    voice_full.wav 按时间轴落位的整条音轨
  可调常量: VOICE(音色) / RATE(语速) / GAP(段间停顿) / TAIL(尾部留白) / SR(采样率)。
"""
import asyncio
import json
import os
import subprocess
import sys
import time

import edge_tts
import numpy as np
import wave

_DIRS = [a for a in sys.argv[1:] if not a.startswith("-")]
BASE = os.path.abspath(_DIRS[0]) if _DIRS else os.getcwd()
SCRIPT = os.path.join(BASE, "script.json")
AUDIODIR = os.path.join(BASE, "audio")
TIMELINE = os.path.join(BASE, "timeline.json")
VOICEFULL = os.path.join(BASE, "voice_full.wav")

VOICE = "zh-CN-XiaoxiaoNeural"
RATE = "+5%"
GAP = 0.45
TAIL = 1.0
SR = 24000
FPS = 30

os.makedirs(AUDIODIR, exist_ok=True)


def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("cmd failed (%d): %s\n%s" % (r.returncode, " ".join(cmd), r.stderr))
    return r.stdout.strip()


async def synth(text, path):
    comm = edge_tts.Communicate(text, VOICE, rate=RATE)
    await comm.save(path)


def probe(path):
    out = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
               "-of", "csv=p=0", path])
    return float(out.strip())


def main():
    with open(SCRIPT, encoding="utf-8") as f:
        script = json.load(f)
    scenes = script["scenes"]

    # 1) 合成 mp3
    failures = []
    for sc in scenes:
        n = int(sc["id"])
        mp3 = os.path.join(AUDIODIR, "scene_%02d.mp3" % n)
        for attempt in range(3):
            try:
                asyncio.run(synth(sc["narration"], mp3))
                if os.path.getsize(mp3) > 1000:
                    break
                raise RuntimeError("file too small: %d bytes" % os.path.getsize(mp3))
            except Exception as e:
                print("[retry %d] scene %d failed: %r" % (attempt + 1, n, e), flush=True)
                if os.path.exists(mp3):
                    os.remove(mp3)
                if attempt == 2:
                    failures.append((n, repr(e)))
                else:
                    time.sleep(1.0)
        if not any(n == f[0] for f in failures):
            print("scene %02d mp3 ok (%.1f KB)" % (n, os.path.getsize(mp3) / 1024.0), flush=True)
        time.sleep(0.3)

    if failures:
        print("FAILURES: %s" % failures)
        sys.exit(2)

    # 2) 转 wav
    wavs = []
    for sc in scenes:
        n = int(sc["id"])
        mp3 = os.path.join(AUDIODIR, "scene_%02d.mp3" % n)
        wav = os.path.join(AUDIODIR, "scene_%02d.wav" % n)
        run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", mp3,
             "-ar", str(SR), "-ac", "1", "-sample_fmt", "s16", wav])
        wavs.append(wav)

    # 3) 时间轴
    durations = [probe(w) for w in wavs]
    out_scenes = []
    t = 0.0
    for i, sc in enumerate(scenes):
        n = int(sc["id"])
        gap_or_tail = TAIL if i == len(scenes) - 1 else GAP
        seg_dur = round(durations[i] + gap_or_tail, 3)
        start = round(t, 3)
        end = round(start + seg_dur, 3)
        out_scenes.append({
            "id": n,
            "audio": "audio/scene_%02d.wav" % n,
            "audio_duration": round(durations[i], 3),
            "duration": seg_dur,
            "start": start,
            "end": end,
        })
        t = end

    total = round(t, 3)
    with open(TIMELINE, "w", encoding="utf-8") as f:
        json.dump({"fps": FPS, "gap": GAP, "total_duration": total,
                   "scenes": out_scenes}, f, ensure_ascii=False, indent=2)

    # 4) 拼接总音轨
    total_samples = int(round(total * SR))
    track = np.zeros(total_samples, dtype=np.int16)
    for i, sc in enumerate(out_scenes):
        wav = os.path.join(BASE, sc["audio"].replace("/", os.sep))
        with wave.open(wav, "rb") as wf:
            assert wf.getnchannels() == 1 and wf.getframerate() == SR, wav
            data = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16)
        off = int(round(sc["start"] * SR))
        endpos = off + len(data)
        if endpos > total_samples:
            raise RuntimeError("scene %d overflows track" % sc["id"])
        # 重叠检查
        if np.any(track[off:endpos] != 0):
            raise RuntimeError("scene %d overlaps previous audio" % sc["id"])
        track[off:endpos] = data

    with wave.open(VOICEFULL, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SR)
        wf.writeframes(track.tobytes())

    print("total_duration=%.3f samples=%d" % (total, total_samples))
    print("audio_durations min=%.3f max=%.3f" % (min(durations), max(durations)))
    print("voice_full written: %s" % VOICEFULL)


if __name__ == "__main__":
    main()
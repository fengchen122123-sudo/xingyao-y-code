---
name: explainer-video
description: 把文章、文档或一段素材做成带旁白与字幕的讲解视频（竖屏/横屏 mp4）。当用户要求"做成讲解视频""文章转视频""给这段内容配音出片""做一分钟短视频""把这篇长文压成视频"时使用。不依赖任何生成式视频模型。
triggers: 讲解视频,做视频,文章转视频,配音视频,竖屏视频,生成视频,视频制作,一分钟视频,短视频,出片
---

# 讲解视频制作

**原理一句话**：讲解视频 = 分镜脚本 + TTS 配音 + 程序化逐帧渲染 + ffmpeg 合成。

全程本地、确定性、可复算——不调用生视频模型，改一行文案重跑就能出新版本。代价是画面是"排版 + 动效"风格，不适合实景/真人内容。

## 何时用 / 何时不用

用：把长文压成 1~5 分钟讲解片、给内容加旁白与字幕、需要批量且可复现地出片、内容以概念/步骤/对比为主。

不用：需要真实拍摄、真人出镜、复杂三维动画 —— 那要接生成式视频模型（本技能给不了）。只要静态图就直接生图。

## 五步流水线

脚本都在本技能的 `scripts/` 下（释放后位于 `~/.ycode/skills/explainer-video/scripts/`，开发态为仓库 `skills/explainer-video/scripts/`）。`<DIR>` 是本次视频的工作目录，脚本只认它。

| 步骤 | 命令 / 动作 | 产出 |
|---|---|---|
| 1 备料 | 公众号文章用 `fetch_article.py <url> <输出前缀>`；其他网页用 fetch_url 抓 HTML 后自己抽正文；本地文档直接读 | 正文 txt |
| 2 写脚本 | 手工产出 `<DIR>/script.json`，字段见 `reference/script-schema.md` | script.json |
| 3 配音 | `python scripts/make_voice.py <DIR>` | `audio/scene_NN.wav`、`timeline.json`、`voice_full.wav` |
| 4 渲染 | `python scripts/render_video.py <DIR> bench` 先测速，再 `... <DIR> full` | `video_silent.mp4` |
| 5 合成验收 | ffmpeg 合音轨（见下），再 `python scripts/verify_video.py <DIR>` | 成品 mp4 |

合成命令（视频流直接复制，音频转 AAC）：

```
ffmpeg -y -hide_banner -i <DIR>/video_silent.mp4 -i <DIR>/voice_full.wav \
  -map 0:v:0 -map 1:a:0 -c:v copy -c:a aac -b:a 160k -ar 44100 -ac 2 \
  -shortest -movflags +faststart <DIR>/<成品名>.mp4
```

`-pix_fmt yuv420p`（渲染阶段已带）与 `-movflags +faststart` 是硬要求：前者保证浏览器能解，后者把索引前置，网页里不用下完就能播。

## 时长预算：先算字数，再写旁白

中文语速（`zh-CN-XiaoxiaoNeural` + `rate="+5%"`）实测 **≈ 4.5 字/秒**。

```
成片时长 ≈ 旁白总字数 ÷ 4.5 + 段间停顿 × 段数 + 尾部留白
```

| 目标时长 | 旁白字数 | 分镜数 | 每段字数 |
|---|---|---|---|
| 60 秒 | 230~250 | 7~9 | 28~40 |
| 90 秒 | 330~365 | 11~13 | 24~34 |
| 3 分钟 | 700~800 | 18~22 | 32~45 |
| 4.5 分钟 | 1150~1250 | 22~24 | 45~60 |

**先定字数再写旁白**。写超了要删信息点，不要靠调语速硬塞——语速一快就失去讲解感，而且时长仍然不可控。

## 关键参数（默认值都验证过）

| 参数 | 默认 | 说明 |
|---|---|---|
| 音色 | `zh-CN-XiaoxiaoNeural` | 女声讲解；男声用 `YunxiNeural` |
| 语速 | `+5%` | 再快会糊 |
| 段间停顿 gap | 0.35~0.45 秒 | 短视频取 0.35，长讲解取 0.45 |
| 尾部留白 | 0.6~1.0 秒 | 收尾别戛然而止 |
| 采样率 | 24000 Hz 单声道 | 合成到整条音轨用 |
| 画布 | 720×1280（竖屏）/ 1280×720（横屏） | 公众号、手机端用竖屏 |
| 帧率 | 30 fps | |
| 渲染耗时 | ≈ 25 ms/帧 | 3 分钟片子约 5 分钟渲染 |

## 实测坑（都踩过）

1. **音画对齐**：先跑配音拿真实时长，再按 `timeline.json` 决定帧数。反过来（先定画面时长再配音）必然对不齐。
2. **不要落盘中间帧**：8000 张 720×1280 PNG 有 1~2 GB。渲染器用 `subprocess.Popen` 起 ffmpeg 的 `image2pipe`，每帧 `img.save(proc.stdin, "PNG", compress_level=1)` 直写管道。`compress_level` 必须设 1，默认级别会让耗时翻倍。
3. **每帧别做全画布运算**：静态部分（背景、卡片、换行后的文字精灵）预渲染一次缓存，每帧只叠动态元素。
4. **TTS 逐段合成**：每段单独一个 mp3/wav，段间 sleep 0.3 秒，失败重试 2 次。整篇一次性合成的话，中间错一段就得全部重来，而且拿不到分段时长。
5. **验收要真的跑命令**：音量检测用 `ffmpeg -af volumedetect -f null NUL`。分镜起点 `max_volume > -60 dB` 说明有语音，段间停顿点 `< -60 dB` 说明真的静音。只"看文件存在"不算验证。
6. **子代理跑不了命令就别派**：只读类型的子代理没有 shell，会把验证项空转成"已完成"。验证类任务派给有执行权限的角色，或主 Agent 自己跑。
7. **抓公众号**：`mp.weixin.qq.com` 在开代理（Clash fake-ip）的机器上会被 SSRF 防护拦，需把主机名加进 `~/.ycode/config.json` 的 `fetch_url_allow_hosts`。

## 验收清单

跑 `scripts/verify_video.py <DIR>`，它会检查：

- 视频 h264、分辨率与 script.json 声明一致；有音频流
- 时长与 `timeline.json` 的 `total_duration` 相差 < 0.5 秒
- 3 个分镜起点有语音、1 个停顿点静音（音画按时间轴落位）
- 抽 2 帧非背景像素占比 > 3%（画面不是空屏）
- 分镜数一致、各段时长之和 = 总时长

机器判不了、必须人看的：字幕与语音是否逐句对齐、版式有没有溢出、文案表述准不准。

## 交付给用户

在星瑶 GUI 里用媒体协议展示成品（绝对路径，正斜杠最稳）：

```
[video: 视频标题](D:/path/to/final.mp4)
```

## 前置依赖

`edge-tts`（TTS，需联网）、`pillow`、`numpy`、`ffmpeg`/`ffprobe`（PATH 上）。字体默认取 Windows 系统字体（`msyhbd.ttc` / `msyh.ttc` / `consola.ttf`），换平台要改渲染器顶部的 `FBD`/`FRG`/`FMO`。

## 文件清单

- `scripts/fetch_article.py` —— 公众号正文抽取（含配图 URL 列表）
- `scripts/make_voice.py` —— 分段 TTS + 时间轴 + 整条音轨
- `scripts/render_video.py` —— 画面渲染器，9 种版式，管道编码
- `scripts/verify_video.py` —— 成品只读验收
- `reference/script-schema.md` —— script.json 字段与 9 种版式的渲染说明

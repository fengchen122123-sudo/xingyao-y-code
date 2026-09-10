---
name: tokenrhythm-image-api
description: Use when 用户要求生成图片、画图、海报、封面、配图，或要求修改/编辑已有图片（垫图改图），且需要直连 TokenRhythm（基元律动）HTTP API 完成——不依赖任何插件、平台内置工具或 MCP，也不需要装 Python：Windows 用自带 PowerShell、其他平台用系统 curl 即可
triggers: 生图,画图,海报,封面,配图,改图,垫图
---

# TokenRhythm（基元律动）生图 · 纯 API 版

不依赖插件、不装任何东西：脚本直连 `https://tokenrhythm.studio/v1`，零第三方依赖。
目录里给了两套等价实现，按对方环境挑一个，谁都不用装运行时。

**分享方式**：把整个 `tokenrhythm-image-api` 目录发给对方，放进其 skills 目录即可
（Y-code/Claude Code 为 `~/.ycode/skills/` 或 `~/.claude/skills/`；跨运行时通用位置是 `~/.agents/skills/`）。
目录内不含任何凭据，Key 由使用者自己填。

### 选哪个脚本

| 对方环境 | 用哪个 | 需要装什么 |
|---|---|---|
| Windows（有没有 Python 都一样） | `scripts/tr_image.ps1` | 系统自带 PowerShell 5.1+，无需安装 |
| 有 Python 3 的任意平台 | `scripts/tr_image.py` | 无第三方库，纯标准库 |
| macOS / Linux 且没 Python | 直接 curl（见第六节） | 系统自带 curl |

两个脚本参数含义一致、行为一致、错误码一致，可互换。

## 一、配置（使用者只改这一处）

Key 获取：登录 tokenrhythm.studio → API Keys → 新建 → 复制完整 Key（只显示一次）。

- Python 版：打开 `scripts/tr_image.py`，填顶部
  ```python
  API_KEY = ""          # ← 填这里，形如 sk_tr_xxxxxxxx
  ```
- PowerShell 版：打开 `scripts/tr_image.ps1`，填中部
  ```powershell
  $ConfiguredApiKey = ''
  ```

也可以不改文件，用环境变量 `TOKENRHYTHM_API_KEY`，或命令行传（`--api-key` / `-ApiKey`）——优先级高于文件内配置。

## 二、调用

```bash
# —— Python 版 ——
python "<技能目录>/scripts/tr_image.py" gen -p "一只戴墨镜的橘猫，赛博朋克霓虹背景"
python "<技能目录>/scripts/tr_image.py" gen -p "雪山日出" -s 1280x720 -m wan2.7-image -n 2 -o ./out
python "<技能目录>/scripts/tr_image.py" edit -p "把背景换成雪景" -i ./cat.png
```

```powershell
# —— PowerShell 版（Windows 零依赖）——
pwsh -NoProfile -ExecutionPolicy Bypass -File "<技能目录>\scripts\tr_image.ps1" gen -Prompt "一只戴墨镜的橘猫"

# 已在本目录、且执行策略允许时，可以简写
.\tr_image.ps1 gen -Prompt "雪山日出" -Size 1280x720 -N 2 -Out .\out
.\tr_image.ps1 edit -Prompt "把背景换成雪景" -Image .\cat.png
```

Windows 上 Python 用 `python`（macOS/Linux 通常 `python3`）。
**执行策略报错**（`无法加载文件…未数字签名`）时不要改系统设置，加 `-ExecutionPolicy Bypass` 即可。
提示词很长或含大量中文标点时，写进 UTF-8 文本文件用 `--prompt-file` / `-PromptFile` 更稳。

### 参数

| 参数（Python / PowerShell） | 适用 | 说明 |
|---|---|---|
| `-p/--prompt` / `-Prompt` | 全部 | 提示词 / 改图指令，必填（或用 `--prompt-file` / `-PromptFile`） |
| `-m/--model` / `-Model` | 全部 | `qwen-image-2.0`（默认）或 `wan2.7-image` |
| `-s/--size` / `-Size` | 全部 | `1024x1024`（gen 默认）/ `1280x720` / `720x1280` / `1280x1280` / `1024x1792` / `1792x1024`；edit 不传则沿用原图尺寸 |
| `-n/--n` / `-N` | gen | 张数 1-4，每张 ¥0.20 |
| `-i/--image` / `-Image` | edit | 原图路径，1 张、≤10MB、jpg/png |
| `-o/--out` / `-Out` | 全部 | 保存目录，默认当前目录 |
| `--json` / `-Json` | 全部 | 输出机器可读 JSON（含 files / cost_cny / trace_id），**纯 ASCII 转义，任何编码环境都能解析** |
| `--api-key` `--base-url` `--timeout` / `-ApiKey` `-BaseUrl` `-TimeoutSec` | 全部 | 临时覆盖 Key / 站点 / 超时秒数 |

退出码：`0` 成功 ｜ `2` 参数或配置错误 ｜ `3` 网络异常 ｜ `4` Key 无效(401) ｜ `5` 限流(429) ｜ `6` 其他 HTTP 错误 ｜ `7` 下载失败 ｜ `8` 响应无图片数据。两个脚本一致。

## 三、展示要求（重要）

脚本已经把图片下载到本地并打印绝对路径（**OSS 链接 24 小时过期，必须以本地文件为准**）。
成功之后必须在回复中用 Markdown 图片语法直接展示，不要只给路径：

```
![生成的图片](本地绝对路径)
```

## 四、常见错误

| 现象 | 原因 | 处理 |
|---|---|---|
| 退出码 2，提示未配置 Key | 三处都没提供 Key | 按提示填 `API_KEY` 或设环境变量 |
| HTTP 401 | Key 无效 / 已删除 / 复制不全 | 重新到站点建一个 Key |
| HTTP 429 | 触发限流 | 稍等再试，不要连续重试 |
| `BAILIAN_IMAGE_ERROR ... Failed to download image` | 垫图体积过大（实测 1.4MB 失败、小图通过） | 压缩原图后重试，别反复调参数 |
| 400 `MODEL_NOT_AVAILABLE` | 模型 ID 写错或未上线 | 用站点 `/api/models` 核对（模型会上下线） |
| 图片下载失败 | 链接已过期或网络不通 | 重新生成，或检查代理 |
| `无法加载文件…未数字签名` / 禁止运行脚本 | PowerShell 执行策略 | 加 `-ExecutionPolicy Bypass`，别去改系统策略 |
| 中文提示词/路径乱码 | 调用方解码编码与终端不一致 | 程序化调用统一用 `-Json` / `--json`（纯 ASCII 转义）；提示词长就用 `--prompt-file` / `-PromptFile` |

## 五、成本与边界

- 计费按张：¥0.20/张，`-n 4` 即 ¥0.80，调用前确认用户要几张。
- 只做生图与垫图改图这一类站内既有能力；不用于绕过鉴权、批量刷接口或压测。
- Key 属于用户私有凭据：**不要写进对话、日志、提交记录或记忆**，脚本只从配置读取，不打印 Key。

## 六、macOS / Linux 且没 Python 时（curl 兜底）

```bash
curl -X POST "https://tokenrhythm.studio/v1/images/generations" \
  -H "Authorization: Bearer sk_tr_xxxx" -H "Content-Type: application/json" \
  -d "{\"model\":\"qwen-image-2.0\",\"prompt\":\"雪山日出\",\"n\":1,\"size\":\"1280x720\",\"response_format\":\"url\"}"
```

返回 `data[0].url` 即图片地址，需自行下载（24 小时过期）。
垫图改图用 `-F "model=..." -F "prompt=<提示词文件" -F "image=@本地图.png"` 打 `/v1/images/edits`。

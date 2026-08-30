# 对话界面图片/视频显示——详细实现设计（media_v1）

日期：2026-08-30
状态：已确认（继承自 Codex 会话「编写图片视频显示实现文档」中确认的架构与消息协议）
范围：GUI 对话界面的图片/视频渲染全链路。CLI 终端不受影响（终端只回放文本）。

## 1. 背景与目标

模型在最终回复中以 `![描述](图片路径)` / `[video: 标题](路径)` 输出本地生成的媒体，
当前前端 `frontend/src/utils/markdown.ts` 没有实现图片语法（`![..](..)` 被当成
普通蓝色文件链接），视频则没有任何渲染协议。模型直接输出的 `<img>/<video>` 被
XSS 转义策略当作纯文本，属于预期安全行为，不作为实现路径。

目标：

1. 模型输出的本地图片/视频在 GUI 对话气泡中直接渲染（图片网格 + 灯箱、video 控件）。
2. 源文件被移动/删除后，历史会话仍可查看（永久归档，内容寻址去重）。
3. 媒体不扩大攻击面：URL 不含真实磁盘路径、能力令牌鉴权、只服务仓库内已登记文件、
   SVG 与远程 URL 不进入渲染链路。
4. Provider 请求字节零变化：媒体清单只进 `_display`，`get_history_for_llm()` 已剔除
   `_display`，不改 Prompt Ledger / 前缀缓存 / 任何请求构造。
5. 历史旧消息通过一次性、幂等、DB 层迁移补齐清单，正文保持不变。

## 2. 根因（勘察结论）

- `frontend/src/utils/markdown.ts:renderMarkdown` 的管线是「先占位、后转义」，
  第 3 步把 `[label](url)` 渲染为链接，第 4 步把裸本地路径渲染为文件链接，
  `![alt](path)` 因此命中第 3 步变成蓝色链接；全管线不存在图片/视频语法。
- 助手消息落库点在 `core/llm.py`（`persisted_message` 构造处），已通过
  `_model_display_snapshot()` 附带 `_display.model_id/model_name`；
  `core/memory.py:get_history_for_llm()` 已剔除 `_display`，是天然的媒体清单挂载点。
- 前端历史回放（`gui_bridge._load_session_messages`）与流式自愈
  （`gui_bridge._emit_message_final` → `message_final` 事件）是两条已存在的
  「落库副本 → 前端」通道，媒体清单复用这两条通道，无需第三套。

## 3. 总体架构

```text
模型最终回复正文
  │  ![江南水乡](img1_jiangnan.png)
  │  [video: 演示视频](demo.mp4) / [视频：演示视频](demo.mp4)
  ▼
core/media_refs.py  媒体引用解析器（core/llm.py 持久化热路径调用）
  ├─ 跳过 ``` 围栏代码块与行内代码（与前端 markdown.ts 同规则，保证 ordinal 对齐）
  ├─ 相对路径以“产生该消息时的 get_workdir()”解析
  ├─ 扩展名白名单 + 文件头 MIME 嗅探双重校验
  ▼
core/media_store.py  永久媒体仓库
  ├─ ~/.ycode/media/blobs/<sha256>.<ext>（内容寻址，跨会话去重）
  ├─ 临时文件校验后 os.replace 原子入位
  └─ Pillow 探测图片宽高（可选，失败为 null）
  ▼
messages 表 assistant 行（一次事务）
  └─ _display.media_v1 = {"items": [...]}（正文 content 不动）
  ▼
core/media_server.py  受控 HTTP 服务（127.0.0.1 随机端口，进程生命周期）
  ├─ 每次启动 secrets.token_urlsafe(32) 能力令牌
  ├─ GET /m/<sha256>.<ext>?t=<token>，Cache-Control immutable，Range 206
  └─ 文件名白名单 ^[0-9a-f]{64}\.[a-z0-9]+$，绝无路径穿越
  ▼
gui_bridge.py
  ├─ _load_session_messages：历史气泡透传 media（含 url 已拼好）
  └─ _emit_message_final：message_final 事件携带 media（流式回合收尾补挂）
  ▼
Vue（appStore + MessageBubble + markdown.ts）
  ├─ markdown.ts 新增第 2.5 步：图片/视频语法在普通链接之前解析
  ├─ 按 ordinal 命中 media 列表 → <img>/<video>；未命中 → 退回文件链接（旧行为）
  └─ 图片点击进灯箱（复用 store.openLightbox）
```

## 4. 模块设计

### 4.1 `core/media_store.py`（新建）——仓库层

常量：

```python
MEDIA_BLOBS_DIR = os.path.join(config.YCODE_DIR, "media", "blobs")
# mime -> 扩展名 白名单（双向映射的唯一事实源）
SUPPORTED_MIME = {
    "image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp",
    "image/gif": ".gif", "image/avif": ".avif",
    "video/mp4": ".mp4", "video/webm": ".webm",
}
MAX_MEDIA_BYTES = 512 * 1024 * 1024  # 512MB，防止误归档超大文件
```

`sniff_mime(path) -> str | None`：按文件头嗅探真实 MIME（魔数字节），
不信任扩展名。规则：

- PNG `\x89PNG\r\n\x1a\n`、JPEG `\xff\xd8\xff`、GIF `GIF87a/GIF89a`、
  WebM/EBML `\x1a\x45\xdf\xa3`、AVIF/HEIC 容器 `ftyp` box（brand 含 avif/avis → avif）
- MP4 `ftyp` box（其余 brand → video/mp4；isom/mp42/dash 等）
- WebP `RIFF....WEBP`
- 其余返回 None

`archive_media_file(path, workdir) -> dict`：

1. 相对路径以 `workdir` 解析为绝对路径；`os.path.realpath` 后必须存在且是文件。
2. 先按扩展名粗筛（大小写不敏感），再用 `sniff_mime` 精筛；扩展名与嗅探结果
   不一致时以嗅探为准，嗅探结果不在白名单 → `unsupported_type`。
3. 大小超过 `MAX_MEDIA_BYTES` → `too_large`。
4. 流式分块（1MB）计算 SHA-256；同一遍流式复制到
   `MEDIA_BLOBS_DIR/tmp/<随机名>`，校验后 `os.replace` 到
   `MEDIA_BLOBS_DIR/<sha256><ext>`。目标已存在（去重命中）则删除临时文件直接复用。
5. 图片（mime 以 `image/` 开头）尝试 Pillow 探测 `width/height`，任何异常置 null。
6. 返回 item 字典（见 4.2）。仓库目录不可写、复制失败等基础设施异常抛
   `MediaStoreError("repo_unwritable" | "read_failed")`——按已确认契约直接向调用方
   报错，绝不假装成功，也绝不静默降级。

`blob_path_for(digest, mime) -> str`：供 HTTP 服务取文件。
`digest` 必须匹配 `^[0-9a-f]{64}$`（否则抛错），扩展名由 mime 查白名单得出，
拼出的路径 `os.path.realpath` 后必须位于 `MEDIA_BLOBS_DIR` 内（纵深防御）。

### 4.2 `core/media_refs.py`（新建）——引用解析与清单组装

`extract_media_refs(text, workdir) -> list[MediaRef]`：

- 图片：`![caption](path)`；视频：`[video: caption](path)` 与
  `[视频：caption](path)`（全角冒号兼容）。
- 先删除 ``` 围栏代码块与行内代码 span 再匹配（与 markdown.ts 保护顺序一致，
  保证「第 N 个媒体语法」在前后端是同一个），围栏内语法不生成媒体。
- `path` 去引号；仅接受本地路径（`http://`/`https://`/`data:` 一律跳过——
  远程 URL 不由渲染器自动下载，防 SSRF，生成工具必须先落盘再引用）。
- 相对路径以 `workdir` 解析；绝对路径直接使用。
- 按 text 中出现顺序编号 ordinal。

`build_media_manifest(text, workdir) -> dict`：

- 对每个 ref 调 `archive_media_file` 归档，成功生成 item：

```json
{
  "ordinal": 0,
  "kind": "image",
  "digest": "sha256hex64",
  "mime": "image/png",
  "ext": "png",
  "bytes": 1491616,
  "width": 1024,
  "height": 1024,
  "duration_ms": null,
  "caption": "江南水乡",
  "source_name": "img1_jiangnan.png"
}
```

- 单个 ref 的路径不存在/类型不支持 → 生成错误 item
  `{"ordinal": N, "kind": "error", "error_code": "not_found" | "unsupported_type" | "too_large",
    "source_name": "...", "caption": "..."}`，前端渲染明确失败态。
- 基础设施错误（仓库不可写/读失败）按契约直接抛 `MediaStoreError` 终止。
- 返回 `{"version": 1, "items": [...]}`；正文无任何媒体语法时返回 None。

### 4.3 落库集成（`core/llm.py`，最小侵入）

- `persisted_message.update(_model_display_snapshot())` 之后追加：

```python
from core.media_refs import attach_media_manifest
attach_media_manifest(persisted_message, get_workdir())
```

- `attach_media_manifest(msg, workdir)`：仅当 `msg["role"]=="assistant"` 且
  `content` 为非空 str 时执行；把 `build_media_manifest` 结果挂到
  `msg.setdefault("_display", {})["media_v1"]`。
- 只挂 `persisted_message`（落库副本），`message_to_append`（发给 LLM 的内存副本）
  不动 → Provider 请求字节零变化。无进展熔断消息与轮次上限消息是固定系统文案，
  不含媒体语法，不挂钩。
- `memory.py:get_history_for_llm()` 已 `pop("_display")`，无需改动。
- 失败语义：`MediaStoreError` 在持久化路径直接抛出（flush_persistence 内），
  回合显式失败——按「Let it crash」与已确认契约，不降级、不静默。

### 4.4 `core/media_server.py`（新建）——受控媒体 HTTP 服务

- `http.server.ThreadingHTTPServer` + `BaseHTTPRequestHandler`，绑定
  `127.0.0.1`，端口 0（系统分配），daemon_threads。进程内单例惰性启动。
- 令牌：启动时 `secrets.token_urlsafe(32)`；校验用 `hmac.compare_digest`。
- 路由：`GET /m/<digest>.<ext>`，参数 `t=<token>`。
  - 令牌错 → 404（不区分 401/404，避免探测）；文件名不匹配白名单正则或
    `blob_path_for` 越界 → 404。
  - 200：`Content-Type` 按扩展名查白名单；`Accept-Ranges: bytes`；
    `Cache-Control: private, max-age=31536000, immutable`（内容寻址不可变）。
  - `Range: bytes=a-b` → 206 + `Content-Range`（视频拖动进度必需）；
    起点越界 → 416。end 缺省取文件尾，end 超尾截断。
- HEAD 同 200/404 语义（省略 body）。
- `public_url(digest, ext)` 返回 `http://127.0.0.1:<port>/m/<digest>.<ext>?t=<token>`。
- 进程退出随进程销毁（daemon 线程），无需显式 shutdown 钩子。

### 4.5 桥接层（`gui_bridge.py`）

- 新增 `_attach_media_urls(display)`：读 `_display.media_v1.items`，对每个非
  error item 补 `url = media_server.public_url(digest, ext)`（服务器不可用时置
  `error_code: "service_unavailable"`），返回可 JSON 化的 items 列表。
- `_load_session_messages`：assistant 气泡增加 `media` 字段 = 处理后的 items。
- `_emit_message_final`：事件 payload 增加 `media` 字段（同一处理函数）；
  只在消息确有 media_v1 时携带。
- LAN 手机端：媒体服务只绑 127.0.0.1，手机远程会话中媒体显示为失败态
  （`service_unavailable`），本期明确不支持，不做内网暴露。

### 4.6 前端

`frontend/src/utils/markdown.ts`：

- `renderMarkdown(md, mediaCtx?)`，`mediaCtx: { items: MediaItem[] } | undefined`。
- 新增第 2.5 步（在行内代码之后、显式链接之前）：
  - 图片 `![caption](path)`；视频 `[video: caption](path)` 与
    `[视频：caption](path)`；正则与后端 extract_media_refs 同构。
  - 按出现顺序维护 `mediaOrdinal`，命中 `mediaCtx.items[ordinal]`：
    - `kind==="image"` → `<img class="md-img" src="..." alt="..." data-media-ordinal="N">`
    - `kind==="video"` → `<video class="md-video" controls preload="metadata" playsinline src="..."></video>`
    - `kind==="error"` → `<span class="md-media-error">⚠ 媒体不可用（原因）</span>`
  - 无 mediaCtx 或 ordinal 越界 → 图片语法退回现有文件链接渲染（旧行为兜底，
    如迁移前旧消息、CLI 复制文本）。
  - 渲染产出同样入占位数组（`${PH}MD${idx}${PH}`），在代码块/行内代码占位还原
    之后、文件链接之前还原。
- HTML 属性全部经 `escapeHtml`（src 是本地 media server URL，仍走转义管线）。

`frontend/src/stores/appStore.ts`：

- `ChatMessage` 增加 `media?: MediaItem[]`（`frontend/src/types/` 下新增
  `media.ts` 或并入 composer.ts，字段与 `_display.media_v1.items` 对齐）。
- `message_final` 处理：`data.media` 存在且非空时挂到最后一条 assistant 气泡
  （不做长度门控，媒体补挂独立于文本自愈）。
- 历史回放 `switch_session` / `get_initial_state` 的 messages 透传 `media`。

`frontend/src/components/MessageBubble.vue`：

- `renderMarkdown(msg.content, msg.media ? { items: msg.media } : undefined)`。
- `onMdBodyClick` 增加委托：`img.md-img` → `store.openLightbox(urls, ordinal)`。

样式：`.md-img` 圆角、max-width 100%、点击可放大；`.md-video` 同宽约束；
`.md-media-error` 弱化告警条。

### 4.7 历史迁移（`core/media_migration.py`，新建）

- 入口 `migrate_media_manifests_once(manager)`，GUI 启动后台线程调用
  （不阻塞启动，失败只记日志）。
- 一次性门闩：`config.load_config()["media_v1_migration_done"]` 为真则跳过；
  完成后 `save_config_value("media_v1_migration_done", True)`。
- 扫描：全部 `role='assistant'` 且 `content` 含媒体语法且 `_display` 无
  `media_v1` 键的消息（逐条判断，天然幂等；中断重跑安全）。
- workdir 取该消息所属会话的 `sessions.workdir`（GLOBAL 会话用
  `os.path.expanduser("~")`，相对路径多数解析不出 → 记 not_found 错误项，
  不猜路径）。
- 只 `UPDATE messages SET content = ?` 其中 content = 原 JSON 加
  `_display.media_v1`（`json.dumps(ensure_ascii=False)`），正文文本字节不动。
- 基础设施错误：单条失败记录日志继续其余；整体失败不置门闩（下次启动重试）。

## 5. 安全边界（汇总）

| 威胁 | 对策 |
| --- | --- |
| XSS（模型输出 `<img onerror>` 等） | 前端先转义后标记管线不变；媒体标签由占位符机制生成，属性全 escapeHtml |
| 任意文件读取 | URL 只含 digest+ext；文件名白名单正则；realpath 越界校验 |
| 未授权访问媒体 | 每进程随机令牌 + compare_digest；错令牌返回 404 |
| SVG/HTML 注入 | 白名单无 image/svg+xml，嗅探不过即拒 |
| SSRF | 渲染器不下载远程 URL；生成工具必须先落盘 |
| 路径泄露 | DB 只存 digest/source_name，不存绝对路径 |
| 前缀缓存失效 | 清单只进 `_display`，`get_history_for_llm` 已剔除 |

## 6. 测试设计

后端（pytest）：

- `tests/test_media_store.py`：各格式魔数嗅探；扩展名不符以嗅探为准；
  同文件二次归档去重（inode 不变）；错误码 not_found/unsupported_type/too_large；
  `blob_path_for` 拒绝非法 digest 与穿越。
- `tests/test_media_refs.py`：图片/视频/中文视频标记提取；围栏代码块与行内
  代码内语法忽略；相对路径按 workdir 解析；错误项生成；基础设施异常透传。
- `tests/test_media_server.py`：错令牌 404；正确 200 与 Content-Type；
  Range 206/416；immutable 头；文件名白名单。
- `tests/test_media_migration.py`：幂等（跑两遍结果一致）；正文不变；
  无媒体语法的消息不动；门闩生效。
- 回归：`tests/test_prompt_byte_stability.py` 必须原样通过（证明请求字节零变化）；
  `tests/test_gui_notice.py`、`tests/test_lan_server.py` 不回归。

前端（vitest）：`markdown-media.test.ts`——有 mediaCtx 时渲染 img/video/error
与 ordinal 对齐；无 mediaCtx 时退回文件链接；代码块内语法不渲染媒体。

## 7. 实施顺序

1. `core/media_store.py` + `core/media_refs.py` + 后端测试
2. `core/llm.py` 落库挂钩 + byte-stability 回归
3. `core/media_server.py` + 测试
4. `gui_bridge.py` 两条通道 + `core/media_migration.py` + 测试
5. 前端 markdown.ts / appStore / MessageBubble / 样式 + vitest
6. 全量验证：pytest 相关文件、py_compile、vitest run

## 8. 明确不做（本期范围外）

- 不新增工具调用、不改 system prompt（模型已会自发输出该语法；
  改提示词需重制 byte-golden，另行任务）。
- CLI 终端不渲染媒体（终端无图形能力，维持现状）。
- LAN 手机端媒体显示（需内网绑定与令牌分发策略，另立任务）。
- width/height/duration 探测仅图片走 Pillow；视频时长不探测（置 null）。

# 星瑶画布全量重写设计：对齐 topwin 画布（2026-11-04）

> 状态：设计定稿。本文记录「删除星瑶旧画布、按星瑶技术栈全量重写 topwin 画布」的完整设计。所有决策均已由用户拍板，本文不做替代方案比选，只给出决策与一句理由。

---

## 1. 结论与决策记录

| 编号 | 决策 | 理由 |
| --- | --- | --- |
| D1 | **不整包嵌入 topwin（Next.js/React），按星瑶现有技术栈（Vue 3 + Pinia + pywebview 桥）参考 topwin 的交互与功能全量重写。** | 整包嵌入会引入第二套前端运行时与云端账号依赖，与星瑶桌面端单栈架构冲突，长期维护成本高于重写。 |
| D2 | **范围全量对齐 topwin 八类节点（image / panorama / text / config / video / audio / director / group）与全部工具（裁剪、蒙版改图、涂鸦、放大、切分、旋转角度、批次生成、全景查看器、导演台、音频处理、小地图、资源引用连线、撤销重做、框选、分组吸附、复制粘贴、导出）。** | 用户明确要求完整体验而非子集，逐工具裁剪会造成交互断裂。 |
| D3 | **旧画布数据不迁移，直接删代码；各项目 `.ycode/canvas/*.json` 磁盘文件保留不清理。** | 旧数据格式与新 schema 差异大且用户确认无迁移价值，保留磁盘文件只是不做破坏性清理，零成本兜底。 |
| D4 | **生成后端走星瑶现有 tokenrhythm 插件（`tokenrhythm_generate_image` / `tokenrhythm_edit_image` / `tokenrhythm_generate_video`），不接 topwin 云端账号体系；模型专属参数按 tokenrhythm 实际能力（qwen-image-2.0 / wan2.7-image / wan3.0-video）裁剪；音频生成一期占位+明确提示。** | 星瑶已有可用的本地生成链路与计费体系，接 topwin 云端等于引入第二套账号/计费，收益为零。 |
| D5 | **导演台子应用全部一起做：按 iframe postMessage 接口契约用 Three.js 重写，作为本计划内独立子项目。** | topwin 的 `/director/index.html` 产物不在仓库（`storyai:` 前缀，独立项目），无法复用只能新写；用户拍板纳入本计划而非留缺口。 |
| D6 | **渲染底座放弃 vue-flow，自研 DOM transform 平移缩放层对齐 topwin；`@vue-flow/*` 依赖随旧画布删除。** | topwin 的 ConnectionPath 贝塞尔连线、SelectionBox 框选、MiniMap 小地图、分组吸附均为世界坐标自绘，vue-flow 的节点/边抽象会成为束缚；此决策覆盖并取代早期「新画布继续用 vue-flow」的初步倾向。 |

> D6 说明：删除清单中「@vue-flow/core 依赖保留」是删除阶段的过渡口径；因 D6 最终定为自研平移层，该依赖在新画布不再使用，随旧画布一并从 `package.json` 移除（见 §2 A 清单的修订注记）。

---

## 2. 旧画布退役策略

### 2.1 数据声明

- 旧画布项目文件 `.ycode/canvas/*.json`（schema_version 1 口径）**不迁移、不读取、不清理**。新画布落盘使用 schema_version 2（见 §3.4），加载时遇到 schema_version ≠ 2 的文件直接跳过并在列表中标注「旧版数据，不支持」，不尝试转换。
- `.ycode/canvas/media/` 目录沿用（新画布媒体落盘同一位置），旧媒体文件不做清理，新画布仅索引自己写入的文件。

### 2.2 A 类：可整体删除

| 目标 | 说明 |
| --- | --- |
| `frontend/src/canvas/` 整目录 | 含 CanvasPane.vue、AnnotationDialog.vue、CharacterRefDialog.vue、annotation.ts、nodes/ 及全部关联测试；其中 `CanvasDock.vue` 已确认无引用方，属死代码。 |
| `frontend/src/stores/canvas.ts` | 旧画布 Pinia store。 |
| `frontend/src/stores/canvas.character.test.ts`、`canvas.derived.test.ts`、`canvas.inlineEdit.test.ts`、`canvas.video.test.ts` | 旧 store 测试。 |
| `gui_canvas_mixin.py`（1418 行 CanvasBridgeMixin） | 旧桥面整体删除，新桥按 §4 重写。 |
| `tests/test_canvas_inline_edit.py`、`tests/test_canvas_video_bridge.py`、`tests/test_canvas_character_bridge.py` | 旧桥测试。 |
| `@vue-flow/*` 前端依赖 | **修订注记**：初步删除清单曾标注「新画布继续用 vue-flow，依赖保留」；D6 定稿自研平移层后，此例外取消，依赖随旧画布删除。新画布不 import vue-flow。 |

### 2.3 B 类：局部改造（不是纯删）

新画布**复用 `'canvas'` 页签 id 与「最近产出托盘」交互口径**，因此 appStore 的产出采集/事件分发是「改造替换」而非纯删。

| 文件与位置（已核实行号） | 处理 |
| --- | --- |
| `frontend/src/components/RightPanel.vue` :70 | `flush` 条件 `visualTab === 'browser' \|\| visualTab === 'canvas'` **保留**——新画布同样需要满幅无内边距。 |
| RightPanel.vue :179-181 | 画布容器：删除阶段替换为占位组件（文案「画布重构中」），接入阶段替换为新 `CanvasPane`。 |
| RightPanel.vue :232-234 | 旧画布组件 import：删除阶段移除，接入阶段改 import 新画布入口。 |
| RightPanel.vue :350 / :361 / :372 | 页签定义/标签文案/图标：`'canvas'` id 保留，文案图标不变。 |
| `frontend/src/stores/appStore.ts` :4-7 | 旧 canvas store import：删除阶段移除，接入阶段指向新 store（同名文件，见 §3.1）。 |
| appStore.ts :112-186 | `collectGeneratedImages` 生图产出采集：**保留并改造**——继续采集聊天生图产出推给新画布「最近产出托盘」。 |
| appStore.ts :236-239 | `RightPanelTabId` 联合类型：**`'canvas'` 成员保留**，否则会话快照中已打开的画布页签反序列化断裂。 |
| appStore.ts :453-540 | `parseCanvasEditResult` / `parseCanvasVideoResult`：保留函数，事件 payload 契约不变（§3.3），解析目标改为新 store 的回填动作。 |
| appStore.ts :5635-5665 | `canvas_*` 事件分发：**保留骨架**，删除阶段分发目标置空（no-op），接入阶段接新 store。 |
| appStore.ts :6049-6051 | 解析函数调用点：随 :453-540 一并改造。 |
| `gui_bridge.py` :57 / :1118 | 旧 mixin 的 import 与继承：删除阶段移除，接入阶段换成新 `CanvasBridgeMixin` 的同位 import/继承。 |

### 2.4 C 类：绝不能删

| 目标 | 原因 |
| --- | --- |
| `core/media_server.py`、`core/media_store.py` | 附件缩略图、idle_maintenance、media_migration、media_refs 共用；新画布媒体预览也依赖它。 |
| `core/character_store.py` 及 `gui_bridge.py`:1325-1331 角色库初始化 | 新画布的角色一致性功能继续使用角色库。 |
| `core/embedded_browser.py`、`gui_snip_mixin.py` | 与画布无耦合的独立功能。 |
| `MessageBubble.vue`:717 与 `RightPanelSnip.vue`:229 的 `document.createElement('canvas')` | 无关同名（截图/标注用途），不属于画布代码。 |

### 2.5 删除顺序原则

常规原则是「先建新后删旧」以避免功能真空；**本计划按用户要求先删后建**（M1 即删除），例外处理如下：

1. 删除阶段**保留 RightPanel 的 `'canvas'` 页签占位**：`RightPanelTabId` 白名单含 `'canvas'`、页签条可打开、容器渲染占位组件。理由：会话快照里已打开的画布页签在重启后仍能恢复，不触发白名单校验丢弃或渲染报错。
2. appStore 的 `canvas_*` 事件分发骨架保留为 no-op，避免后端残留事件（如仍在跑的旧 job 回填）导致前端未处理告警。
3. `gui_bridge.py` 删除旧 mixin 继承后，M1 交付时桥面不含任何 `canvas_*` 方法；M2 起新桥逐步补齐。删除与重建之间画布页签显示「画布重构中」占位，这是用户接受的过渡态。

---

## 3. 新画布架构

### 3.1 目录决策

**删旧后重建同名 `frontend/src/canvas/`，store 仍名 `frontend/src/stores/canvas.ts`。** 理由：RightPanel 与 appStore 的 import 路径、页签 id、测试文件命名全部不用改路径，改动面最小；「同名新写」在 git 历史上表现为删除+新增，不会与旧实现混淆。

```
frontend/src/canvas/
├── CanvasPane.vue            # 画布页签入口（RightPanel 容器挂载点）
├── render/                   # 渲染层（自研平移缩放）
│   ├── InfiniteCanvas.vue    # translate+scale 世界容器、48px 网格、空格抓手
│   ├── ConnectionPath.vue    # 贝塞尔资源引用连线（世界坐标自绘 SVG）
│   ├── SelectionBox.vue      # 框选矩形与命中计算
│   └── MiniMap.vue           # 小地图（世界坐标缩略自绘）
├── nodes/                    # 八类节点组件
│   ├── ImageNode.vue / PanoramaNode.vue / TextNode.vue / ConfigNode.vue
│   ├── VideoNode.vue / AudioNode.vue / DirectorNode.vue / GroupNode.vue
│   └── NodeShell.vue         # 公共外壳：标题栏/选中态/缩放手柄/端口
├── tools/                    # 工具弹窗
│   ├── CropDialog.vue / MaskEditDialog.vue / DoodleDialog.vue
│   ├── UpscaleDialog.vue / SplitDialog.vue / RotateViewDialog.vue
│   ├── BatchGenerateDialog.vue / PanoramaViewer.vue（@photo-sphere-viewer/core）
│   ├── AudioToolsDialog.vue / ExportDialog.vue
│   └── RecentTray.vue        # 最近产出托盘（复用旧交互口径）
├── utils/                    # 纯逻辑 TS 模块（从 topwin 近乎直译，见 §5 映射）
│   ├── canvas-group.ts / canvas-node-size.ts / canvas-image-data.ts
│   ├── canvas-reference-guard.ts / canvas-sanitize.ts / canvas-video-frame.ts
│   ├── history.ts            # 撤销重做（快照/命令栈）
│   └── clipboard.ts          # 复制粘贴序列化
├── bridge.ts                 # pywebview 桥方法封装（window.pywebview.api.canvas_*）
└── types.ts                  # CanvasNodeData / CanvasConnection / CanvasProject
frontend/src/stores/canvas.ts # Pinia store：项目状态、选中集、视口、历史栈、job 表
frontend/public/director/     # 导演台子应用构建产物（§6）
```

### 3.2 模块划分与职责

- **渲染层**：对齐 topwin `infinite-canvas.tsx` 的自研 DOM transform（`translate(x,y) scale(k)`），48px 网格背景、空格+拖拽抓手、滚轮缩放锚定指针。框选、连线、小地图、分组吸附全部在同一世界坐标系内自绘，不经任何画布框架。
- **节点组件**：八类节点共用 NodeShell，各自渲染内容与工具入口；节点尺寸规则直译 `canvas-node-size.ts`。
- **工具弹窗**：每个工具一个对话框组件，图像像素操作（裁剪/切分/放大插值/涂鸦合成）直译 `canvas-image-data.ts` 的纯前端 Canvas2D 实现；生成类操作经 store → bridge 受理 job。
- **纯逻辑 utils**：topwin `canvas/utils/` 为框架无关 TS，**近乎直译复用，不做 Vue 化**；仅把平台调用（localforage / PlatformAdapter）替换为桥方法。
- **store**：Pinia 单 store，持有 CanvasProject、选中集、视口、撤销重做栈、进行中 job 表；所有落盘经防抖 save。
- **桥层**：`bridge.ts` 统一封装 `canvas_*` 桥方法与事件订阅，组件不直接触碰 `window.pywebview`。

### 3.3 与 appStore 的事件契约（保留兼容）

以下事件名与 payload 结构保留旧口径，appStore 侧解析函数（`parseCanvasEditResult` / `parseCanvasVideoResult`）与分发骨架不换名：

| 事件 | 方向 | 语义 |
| --- | --- | --- |
| `canvas_edit_result` | 后端 → 前端 | 图像生成/改图 job 完成或失败，payload 含 `job_id`、`status`、`images`（media 引用）、`error`。 |
| `canvas_video_result` | 后端 → 前端 | 视频生成 job 回填，payload 含 `job_id`、`status`、`video`、`error`。 |
| `canvas_focus_node` | 前端内部（appStore → canvas store） | 聊天区「送到画布/定位节点」跳转，payload 含 `node_id`。 |

聊天生图产出继续经 `collectGeneratedImages` 进入新画布「最近产出托盘」，交互口径与旧画布一致。

### 3.4 数据模型与落盘

字段名**保持 topwin 口径**（`canvas/types.ts`），便于对照 8550 行参考实现：

```ts
interface CanvasNodeData {
  id: string;
  type: 'image' | 'panorama' | 'text' | 'config' | 'video' | 'audio' | 'director' | 'group';
  title: string;
  position: { x: number; y: number };
  width: number;
  height: number;
  metadata: Record<string, unknown>; // 各类型专属：src/prompt/model/params/groupId…
}
interface CanvasConnection { id: string; fromNodeId: string; toNodeId: string; }
interface CanvasProject {
  id: string; title: string; createdAt: number; updatedAt: number;
  nodes: CanvasNodeData[]; connections: CanvasConnection[];
  viewport: { x: number; y: number; zoom: number };
  backgroundMode: string; sidePanel: unknown;
  schema_version: 2; // 星瑶新增字段，topwin 原结构无此字段
}
```

- 落盘：`.ycode/canvas/<id>.json`，`schema_version: 2`。加载时校验 sanitize（直译 `canvas-sanitize.ts`）：非法节点丢弃并记录警告，不整体拒载。
- 后端沿用旧 mixin 的三个口径重写精简版：**原子写**（临时文件+替换）、**白名单 ID**（项目 id 仅允许 `[A-Za-z0-9_-]`，防路径穿越）、**工作目录限定**（一切路径 resolve 后必须在当前项目 `.ycode/canvas/` 内）。
- 媒体：不用 topwin 的 PlatformAdapter + localforage；文件落 `.ycode/canvas/media/`，经 `core/media_server` 换预览 URL（沿用旧 `canvas_resolve_media` 口径：磁盘路径 → 本地 HTTP 预览 URL，前端只持有 URL 与 media id）。

---

## 4. 后端桥面：新 CanvasBridgeMixin

新写 `gui_canvas_mixin.py`（同名新文件，精简版），`gui_bridge.py` 在原 :57/:1118 位置恢复 import 与继承。方法清单：

| 分组 | 方法 | 说明 |
| --- | --- | --- |
| 持久化 | `canvas_list_projects()` | 列出 `.ycode/canvas/*.json` 元信息（id/title/updatedAt/schema_version），旧版文件标注不支持。 |
| | `canvas_load_project(project_id)` | 读取+sanitize，schema_version≠2 返回明确错误。 |
| | `canvas_save_project(project)` | 原子写，白名单 ID 校验。 |
| | `canvas_delete_project(project_id)` | 删除项目 json；媒体文件不级联删除（由 media 维护任务统一回收）。 |
| 媒体 | `canvas_save_media(data_url_or_bytes, hint_name)` | 前端像素结果/上传落盘到 media/，返回 media id。 |
| | `canvas_resolve_media(media_id)` | 换 media_server 预览 URL（沿用旧口径）。 |
| 生成 | `canvas_generate_image(params)` | 受理即返回 `{pending: true, job_id}`；后台线程调插件工具注册表执行 `tokenrhythm_generate_image`，完成后 `emit_event('canvas_edit_result', …)`。 |
| | `canvas_edit_image(params)` | 同上，走 `tokenrhythm_edit_image`（蒙版改图/涂鸦改图/参考改图）。 |
| | `canvas_generate_video(params)` | 同上，走 `tokenrhythm_generate_video`，回填 `canvas_video_result`。 |
| | `canvas_cancel_job(job_id)` | 尽力取消：置取消标记，工具调用返回后丢弃结果不回填。 |
| 音频 | `canvas_audio_process(operation, media_id, params)` | operation ∈ split-av / trim / tempo / silence-split；用随包 `vendor/ffmpeg` 本地执行（topwin 走服务端 ffmpeg，星瑶改本地），后台线程+进度事件，产出落 media/。 |
| 角色 | （沿用） | 不新增角色桥方法：`core/character_store.py` 与 gui_bridge 既有角色库接口直接使用，新画布角色一致性功能经现有接口读写。 |

生成链路契约保留旧 `canvas_edit_image` / `canvas_generate_video` 的 **pending/job_id/事件回填** 三段式：桥方法立即返回受理凭据，前端 job 表置 loading，事件到达后按 job_id 匹配回填节点。理由：pywebview 桥同步调用不能阻塞 UI，且 appStore 的事件解析函数据此保留兼容。

---

## 5. 生成能力映射与参考实现映射

### 5.1 topwin 功能 → tokenrhythm 能力映射表

| topwin 功能 | 星瑶实现 | 备注 |
| --- | --- | --- |
| 文生图（多模型面板） | `tokenrhythm_generate_image`（qwen-image-2.0 / wan2.7-image） | 参数面板只暴露 tokenrhythm 实际支持的参数（prompt/尺寸/张数等）。 |
| 图生图/蒙版改图/涂鸦改图 | `tokenrhythm_edit_image`（qwen-image-2.0 / wan2.7-image） | 参考图/蒙版经 media id 传入。 |
| 视频生成 | `tokenrhythm_generate_video`（wan3.0-video） | 首帧图/时长/分辨率按工具实际入参裁剪。 |
| 批次生成 | 前端循环受理多个 job，共享参数模板 | 并发上限由桥侧队列控制。 |
| 音频生成（TTS/音乐） | **已知缺口**：一期节点可建、UI 占位并明确提示「音频生成暂未接入」 | tokenrhythm 无音频生成工具；不伪造能力。 |
| MJ 专属参数（stylize/chaos 等） | **裁剪项，不实现** | tokenrhythm 无 Midjourney 通道。 |
| Kling elementList | **裁剪项，不实现** | tokenrhythm 无 Kling 通道。 |
| Seedance 合规转存 arkAsset 链路 | **裁剪项，不实现** | 该链路绑定 topwin 云端资产合规体系，本地无对应物。 |
| 云端素材库 / 公共素材 / 分享 / 登录计费 | **全部裁剪** | 星瑶为本地单机应用，素材即本地 media/，计费走 tokenrhythm 既有体系。 |

### 5.2 参考实现映射（topwin 仓库 → 星瑶模块）

- **行为规格来源**：`web/src/app/(user)/canvas/[id]/canvas-client-page.tsx`（8550 行）是编辑器主页，重写时逐段解读其交互行为作为规格；`canvas/components/` 约 60 个组件按 §3.1 目录归并为 Vue 组件。
- **纯逻辑直译**（框架无关 TS，近乎原样搬运，仅替换平台调用）：
  - `canvas/utils/canvas-group.ts` → 分组吸附；
  - `canvas-node-size.ts` → 节点尺寸规则；
  - `canvas-image-data.ts` → 裁剪/切分/放大的纯前端 Canvas2D 像素操作；
  - `canvas-reference-guard.ts` → 资源引用连线校验（类型兼容/防环）；
  - `canvas-sanitize.ts` → 落盘数据防护；
  - `canvas-video-frame.ts` → 视频抽帧。
- **渲染**：`infinite-canvas.tsx` 的 translate+scale、48px 网格、空格抓手 → `render/InfiniteCanvas.vue`；ConnectionPath/SelectionBox/MiniMap 世界坐标自绘 → render/ 同名组件（D6）。
- **媒体**：PlatformAdapter（projects/media/transfers）+ localforage → 桥方法 + `.ycode/canvas/media/` + media_server 预览 URL。
- **生成**：云端 `/api/v1` 计费接口 → 桥受理 job + 插件工具注册表 + emit_event 回填。
- **音频处理**：服务端 ffmpeg → `vendor/ffmpeg` 本地桥方法。
- **全景查看器**：`@photo-sphere-viewer/core` 直接引入星瑶前端（MIT 许可，无兼容问题）。
- **导演台**：`/director/index.html`（产物不在仓库）→ 按 postMessage 契约新写 Three.js 子应用（§6）。

---

## 6. 导演台子项目设计

### 6.1 形态与技术

- 独立子项目，源码放 `frontend/director-src/`（独立 Vite 入口），构建产物输出 `frontend/public/director/`（Vite 静态目录），画布内以 **iframe 同源嵌入**（`/director/index.html`）。理由：Three.js 场景与画布 DOM 隔离，崩溃/性能互不拖累，且与 topwin 的 iframe 契约完全对齐。
- 技术栈：Three.js + 原生 TS（不引 Vue，子应用足够小）；全景背景用 equirectangular 贴图映射到球体内壁。

### 6.2 接口契约（已核实 topwin canvas-director.tsx，逐字对齐）

**入站（画布 → iframe）：**

| 消息 type | payload |
| --- | --- |
| `storyai:director-session` | `{instanceId, theme, project}` — 会话初始化：实例 id、明暗主题、导演台项目数据。 |
| `storyai:director-panoramas` | `{panoramas: [{edgeId, sourceNodeId, imageUrl, fileName, projectionMode}]}` — 画布推送可用全景图列表（经资源引用连线关联的全景节点）。 |

**出站（iframe → 画布）：**

| 消息 type | payload |
| --- | --- |
| `storyai:director-ready` | 无 — 子应用就绪，画布随后发送 session 与 panoramas。 |
| `storyai:director-close` | 无 — 用户请求关闭导演台。 |
| `storyai:director-project-changed` | `{project}` — 场景编辑变更，画布回写 director 节点 metadata（持久化）。 |
| `storyai:director-panorama-removed` | `{edgeId, sourceNodeId}` — 用户在导演台移除某全景引用，画布删除对应连线。 |
| `storyai:director-captures-sent` | `{captures: [{dataUrl, fileName}]}` — 多视角截图发回画布，落地为 image 节点（dataUrl 经 `canvas_save_media` 落盘）。 |

消息校验：双向校验 `event.source`（iframe contentWindow / parent）与 type 前缀 `storyai:`，同源部署下不放宽 origin 校验。

### 6.3 功能范围

1. 全景背景：加载 equirectangular 全景图为环境球，支持切换 panoramas 列表中任一图。
2. 基础几何体摆放：立方体/球/圆柱/平面的增删、平移/旋转/缩放（transform gizmo），作为构图占位物。
3. 相机机位：保存/切换多个机位（位置+朝向+FOV），机位列表入 project 数据。
4. 多视角截图：对选定机位批量渲染 `renderer.domElement.toDataURL()`，经 `director-captures-sent` 发回画布。

### 6.4 验收口径

- 画布新建 director 节点 → 打开 iframe → ready/session/panoramas 握手成功，全景正确显示。
- 摆放几何体并保存两个机位 → 关闭重开导演台，project 数据完整恢复（经节点 metadata 持久化）。
- 批量截图两个机位 → 画布出现两个新 image 节点，媒体已落盘可预览。
- 在导演台移除全景 → 画布对应引用连线被删除。
- 主题切换消息生效；close 消息正确关闭弹窗。

---

## 7. 风险与开放问题

| 风险 | 影响 | 对策 |
| --- | --- | --- |
| 8550 行 canvas-client-page.tsx 规格解读工作量 | 交互细节遗漏（快捷键、边界态、拖拽手感） | 按功能域分段解读并逐段登记到实现清单；M3-M6 每个里程碑对照原文核验，不凭印象实现。 |
| 全景素材版权 | topwin 演示全景图不可直接搬运 | 只搬代码不搬素材；全景查看器/导演台验收用自生成或明确可商用（CC0）素材。 |
| ffmpeg 参数对齐 | split-av/trim/tempo/silence-split 的服务端参数未知细节（静音阈值、编码预设） | 以 topwin 客户端可见参数为准，vendor/ffmpeg 本地实测校准；差异记录在音频工具实现说明中，不阻塞其他里程碑。 |
| 性能：自研平移层大量节点 | 数百节点+高分辨率图时掉帧 | 节点内容虚拟化（视口外降级为占位）、图片经 media_server 缩略图、transform 用 CSS will-change；M8 做 300 节点压测。 |
| 先删后建的过渡态 | M1-M2 期间画布页签不可用 | 用户已接受；占位组件明确提示「画布重构中」，会话快照不丢页签（§2.5）。 |
| 旧 job 残留事件 | 删除阶段后端可能仍回填 canvas_* 事件 | appStore 分发骨架 no-op 兜底（§2.5），不产生未处理告警。 |

开放问题均已给出对策与归属里程碑，无遗留待决事项；实现期若发现 topwin 行为与本文描述不符，以 topwin 源码为准并在实现说明中登记偏差。

---

## 8. 里程碑划分

| 里程碑 | 内容 | 交付判定 |
| --- | --- | --- |
| **M1 删除旧画布** | 按 §2 A/B 清单删除与占位改造；C 清单回归确认（附件缩略图/角色库/截图不受影响）；移除 @vue-flow 依赖。 | 前端构建通过、现有测试通过、画布页签显示占位、会话快照恢复不报错。 |
| **M2 画布底座+持久化** | InfiniteCanvas 平移缩放/网格/抓手；types.ts + schema_version 2；新 CanvasBridgeMixin 持久化四方法+媒体两方法；新 canvas store 骨架；CanvasPane 替换占位。 | 新建/保存/重开项目，视口与空画布状态恢复；原子写与白名单 ID 测试通过。 |
| **M3 节点与连线** | 八类节点组件（内容为本地展示态）、NodeShell、ConnectionPath + reference-guard、SelectionBox 框选、分组吸附、复制粘贴、撤销重做、小地图、导出。 | 全工具中非生成类交互对齐 topwin；utils 直译模块单测通过。 |
| **M4 生成链路** | 桥生成三方法+job 表+事件回填；config 节点参数面板（tokenrhythm 能力口径）；批次生成；最近产出托盘接通 collectGeneratedImages。 | 文生图/改图/视频端到端出图回填节点；取消与失败态正确；音频生成占位提示到位。 |
| **M5 图像工具** | 裁剪/切分/放大（canvas-image-data 直译）、蒙版改图、涂鸦、旋转角度。 | 各工具像素结果与 topwin 行为一致，产物落 media 并生成新节点。 |
| **M6 视频/音频/全景** | 视频抽帧（canvas-video-frame）、音频四操作（vendor/ffmpeg 桥）、全景查看器（@photo-sphere-viewer/core）与 panorama 节点。 | 抽帧出图、四音频操作产物可播放、全景节点可交互查看。 |
| **M7 导演台** | §6 子项目全部功能 + 画布 director 节点集成。 | §6.4 五条验收全部通过。 |
| **M8 打磨回归** | 快捷键/手感对照 8550 行规格查漏；300 节点性能压测；全量回归（含 C 清单共用功能）；文档更新。 | 无 S1/S2 级偏差；性能达标；回归通过。 |

依赖关系：M1 → M2 → M3 → {M4, M5, M6 可并行} → M7（依赖 M2 持久化与 M6 全景）→ M8。

---

## 附：自查记录

- 占位符检查：全文无 TBD/TODO/待定占位。
- 一致性检查：D6（自研平移层、删 vue-flow）与 §2.2 修订注记、§3.2、M1 交付项一致；D3（数据不迁移）与 §2.1、§3.4 schema 校验一致；D4 裁剪项与 §5.1 表一致；§2.5 先删后建例外与 M1/风险表一致；导演台契约与用户核实口径逐字段一致。
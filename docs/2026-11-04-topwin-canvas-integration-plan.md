# topwin 画布接入实施计划（M2–M8，2026-11-04）

> 规格来源：`docs/2026-11-04-topwin-canvas-integration-design.md`（已定稿，下称「设计」）。
> 本计划从设计 §8 的 **M2** 开始；**M1（删除旧画布）由另一份删除计划覆盖，不在本计划内**。
> 开工前置：M1 已完成并合入（`frontend/src/canvas/`、`frontend/src/stores/canvas.ts`、`gui_canvas_mixin.py` 均已删除，画布页签为占位组件）。
> 参考实现（只读）：`D:\fengchenhao\topwin\web\src\app\(user)\canvas\`（下称「参考仓」）。行为规格主文件：`参考仓/[id]/canvas-client-page.tsx`（8550 行），每个交互任务开工前逐段解读对应功能区，不凭印象实现。

---

## 0. 目标、架构与全局约束

### 0.1 目标

按星瑶技术栈全量重写 topwin 画布：八类节点（image / panorama / text / config / video / audio / director / group）+ 全部工具（裁剪、蒙版改图、涂鸦、放大、切分、旋转角度、批次生成、全景查看器、导演台、音频处理、小地图、资源引用连线、撤销重做、框选、分组吸附、复制粘贴、导出）。生成走 tokenrhythm 插件；音频生成一期占位；MJ/Kling/Seedance/云端体系裁剪（设计 §5.1）。

### 0.2 架构

- **前端**：Vue 3 + Pinia + 自研 DOM transform 渲染层（`translate(x,y) scale(k)`，48px 网格，空格抓手；不用 vue-flow）。
- **桥**：pywebview `window.pywebview.api.canvas_*` → 新 `gui_canvas_mixin.py`（`CanvasBridgeMixin`，同名重建）。
- **生成**：桥受理 job → 后台守护线程调 `core.tool_registry.ToolRegistry.execute_strict` 执行 tokenrhythm 三工具 → `_emit_raw` 事件回填（实际事件出口沿用旧 mixin 的 `_emit_raw`，经 `_js_queue` 线程安全投递；设计 §4 所述 emit_event 语义即此）。
- **导演台**：`frontend/director-src/` 独立 Vite + Three.js 子项目，产物 `frontend/public/director/`，iframe 同源 + postMessage 契约（设计 §6）。

### 0.3 全局约束（每个任务都适用）

| 项 | 约束 |
| --- | --- |
| Shell | PowerShell 7；命令串联用 `;` 不用 `&` |
| 前端验证 | `cd frontend; npm run build`（含 vue-tsc 类型检查）；单测 `cd frontend; npx vitest run`（可加路径限定单文件） |
| 后端验证 | `python -m pytest tests/test_canvas_*.py -q`（在仓库根 `D:\fengchenhao\ycode\xingyao-y-code` 执行） |
| 提交 | **每个任务独立 `git commit`**，信息格式 `canvas(Mx-Tyy): 摘要` |
| 字段口径 | `CanvasNodeData / CanvasConnection / CanvasProject` 字段名对齐 topwin `参考仓/types.ts`（完整定义见 M2-T01）；落盘新增 `schema_version: 2` |
| 事件契约 | `canvas_edit_result` / `canvas_video_result` / `canvas_focus_node` 事件名保留；payload 为旧字段超集（见 §0.5），appStore 解析函数只扩不改名 |
| 路径口径 | 本计划所有相对路径以星瑶仓库根为基准；前端新代码仅落 `frontend/src/canvas/` 与 `frontend/src/stores/canvas.ts`；后端新桥为 `gui_canvas_mixin.py` 同名重建 |
| 素材 | 只搬代码不搬素材；测试/验收素材用自生成或 CC0（设计 §7） |

### 0.4 共享类型基线（所有任务统一引用，禁止另起同义类型）

- TS 数据模型：`frontend/src/canvas/types.ts`（M2-T01 完整定义）。
- 桥封装：`frontend/src/canvas/bridge.ts`（M2-T04 定义全部方法签名，后续任务只调用不改签名；确需扩展须在任务 Interfaces 里写明增量）。
- store：`frontend/src/stores/canvas.ts`（M2-T04 骨架，后续任务追加 action，签名以各任务 Interfaces 为准）。
- 事件载荷 TS 类型：`CanvasEditResultData` / `CanvasVideoResultData` 定义在 `frontend/src/stores/appStore.ts`（已存在），M4-T01 扩展字段后全局共用。

### 0.5 事件 payload 契约（新画布 = 旧字段超集）

`canvas_edit_result`（旧口径 `ok/source_node_id/path/url/error` 保留，新增字段旧解析函数忽略、不破坏）：

```json
{"ok": true, "job_id": "cj_xxx", "project_id": "p1", "source_node_id": "n1",
 "status": "success", "path": ".ycode/canvas/media/xxx.png", "url": "http://127.0.0.1:PORT/media/...",
 "images": [{"path": "…", "url": "…", "media_id": "sha256digest", "width": 1024, "height": 1024}],
 "error": ""}
```

`status ∈ success | error | cancelled`；`ok === (status === 'success')`；失败时 `images: []`。
`canvas_video_result`：同上骨架，`images` 换为 `video: {path, url, media_id, duration: number, model: string} | null`，顶层保留旧字段 `job_id/source_node_id/path/url/duration/model/error`。
`canvas_focus_node`：`{node_id: string}`（前端内部 appStore → canvas store）。

### 0.6 utils 直译总清单（源 `参考仓/utils/` → 目标 `frontend/src/canvas/utils/`）

| 源文件 | 目标文件 | 任务 | 平台调用替换点 |
| --- | --- | --- | --- |
| canvas-node-size.ts | canvas-node-size.ts | M2-T05 | 仅删 `"use client"` |
| canvas-sanitize.ts | canvas-sanitize.ts | M2-T05 | `CanvasProject` import 从 `../stores/use-canvas-store` 改 `../types` |
| canvas-image-data.ts | canvas-image-data.ts | M2-T05 | 删 `"use client"`；`getProxyUrl`（`@/services/image-storage`）替换为直接加载 URL（media_server 同源可直载），`loadImage` 去掉代理分支 |
| canvas-video-frame.ts | canvas-video-frame.ts | M2-T05 | 无平台依赖，原样直译 |
| canvas-video-duration.ts | canvas-video-duration.ts | M2-T05 | 类型 import 路径改 `../types`；探测 URL 改走节点 `metadata.content`（已是 media_server URL） |
| canvas-audio-duration.ts | canvas-audio-duration.ts | M2-T05 | 同上 |
| canvas-panorama.ts（依赖） | canvas-panorama.ts | M2-T05 | 原样直译（material-title/reference-guard/resource-references 依赖 `isCanvasImageNodeType`） |
| canvas-group.ts | canvas-group.ts | M3-T02 | 无（纯逻辑） |
| canvas-material-title.ts | canvas-material-title.ts | M3-T02 | import 改本目录 `./canvas-panorama` |
| canvas-resource-references.ts | canvas-resource-references.ts | M3-T04 | `@/lib/image-reference-prompt.imageReferenceLabel` 与 `@/lib/seedance-video.seedanceReferenceLabel` 两个标签函数按参考仓行为直译为本地实现（输出「图片N/视频N」口径不变）；Seedance 专属标签分支裁剪，保留 图片/视频/音频/文本 四类 |
| canvas-reference-guard.ts | canvas-reference-guard.ts | M3-T04 | `@/lib/video-reference-rules` 整表裁剪重写为 tokenrhythm 规则（见 M3-T04）；`buildNodeGenerationInputs`（`../components/canvas-node-generation`）随 M3-T04 直译为 `frontend/src/canvas/utils/canvas-generation-inputs.ts`，其中云端素材分支裁剪 |

### 0.7 测试策略

1. **utils 直译**：逐文件 vitest 单测，用例从 topwin 源码行为推导（边界值写进用例名），与实现同任务提交。
2. **桥方法 pytest**：`tests/test_canvas_*.py`，参考已删除的旧测试口径（`test_canvas_inline_edit / test_canvas_video_bridge / test_canvas_character_bridge`，git 历史可查）按 schema_version 2 重写：原子写（临时文件+fsync+replace、失败不留 tmp）、白名单 ID（`[A-Za-z0-9_-]+`、路径穿越拒绝）、schema 校验（≠2 拒载/拒存）、并发闸门（同画布同类任务同时只跑一个）、事件回填（fake registry + 捕获 `_emit_raw`）。生成工具全部 mock，不发真实计费请求。
3. **组件**：`@vue/test-utils`（已在 frontend 依赖中）+ jsdom，挂 store 用 `createPinia`；像素类断言用注入的 canvas stub 校验调用参数，不做真实渲染像素比对。
4. **端到端**：每里程碑末尾给手工验收清单（见各组末），真实 tokenrhythm 计费调用只在里程碑验收时人工执行。

### 0.8 里程碑依赖与并行派发

```
M2 → M3 → {M4, M5, M6 三组可并行派发} → M7 → M8
```

- M2 内部：T01→T04 串行；T02/T03 可并行；T05 依赖 T01，可与 T02/T03 并行；T06 依赖 T04。
- M3 内部：T01/T02 并行；T03/T04 依赖 T01；T05/T06 依赖 T01–T03；T07 依赖 T01–T06。
- M4/M5/M6 三组整体可并行派发给不同执行者（文件面不重叠：M4 主改桥生成段+config/BatchGenerate/RecentTray；M5 主改 tools 图像弹窗；M6 主改 video/audio/panorama 与桥音频段）。共改文件仅 `gui_canvas_mixin.py` 与 `stores/canvas.ts`：约定 M4 先合入桥生成段，M6 的音频段独立函数追加，合并冲突按「函数级追加」解决。
- M7 内部 T01–T05 串行（同一子项目），T06 依赖 T05 与 M6-T04。
- M8 全组串行收尾。

---

## M2 画布底座与持久化（6 任务）

### M2-T01 类型基线

- **Files**
  - Create: `frontend/src/canvas/types.ts`
- **Interfaces**
  - Produces: 下列全部类型（后续所有任务的唯一数据模型来源）。
- **步骤**：`types.ts` 完整定义如下（对齐 topwin `types.ts`，metadata 裁剪 MJ/Kling/Seedance/ark/云端字段，保留其余字段名不变；新增 `mediaId`/`jobId`/`schema_version`）：

```ts
export type Position = { x: number; y: number };
export type ViewportTransform = { x: number; y: number; k: number }; // topwin 口径 k，不是 zoom

export enum CanvasNodeType {
  Image = 'image', Panorama = 'panorama', Text = 'text', Config = 'config',
  Video = 'video', Audio = 'audio', Director = 'director', Group = 'group',
}
export type CanvasNodeStatus = 'idle' | 'success' | 'loading' | 'error';
export type CanvasGenerationMode = 'text' | 'image' | 'video' | 'audio';
export type CanvasImageGenerationType = 'generation' | 'edit';

export interface CanvasNodeMetadata {
  content?: string;                 // 文本内容 / 媒体预览 URL（media_server URL）
  mediaId?: string;                 // 星瑶新增：media_store digest，canvas_resolve_media 入参
  groupId?: string;
  composerContent?: string;
  prompt?: string;
  excludeUpstreamText?: boolean;
  status?: CanvasNodeStatus;
  errorDetails?: string;
  jobId?: string;                   // 星瑶新增：进行中 job 认领
  blamedReferenceNodeIds?: string[];
  referenceBlameNotes?: Record<string, string>;
  fontSize?: number;
  generationMode?: CanvasGenerationMode;
  generationType?: CanvasImageGenerationType;
  model?: string;                   // qwen-image-2.0 | wan2.7-image | wan3.0-video
  size?: string;                    // 1024x1024 / 1280x720 / 720x1280 / 1280x1280 / 1024x1792 / 1792x1024；全景 2:1
  count?: number;                   // 1-4（tokenrhythm n）
  seconds?: string;                 // 视频时长（提交时转 int，-1 智能时长）
  negativePrompt?: string;          // 拼接进 prompt 尾部（tokenrhythm 无独立入参）
  references?: string[];
  naturalWidth?: number; naturalHeight?: number;
  freeResize?: boolean;
  isBatchRoot?: boolean; batchRootId?: string; batchChildIds?: string[];
  batchUsesReferenceImages?: boolean;
  primaryImageId?: string; imageBatchExpanded?: boolean;
  storageKey?: string;              // 星瑶口径 = 项目内相对磁盘路径
  thumbnailContent?: string; thumbnailStorageKey?: string;
  mimeType?: string; bytes?: number;
  mediaDurationMs?: number;         // 素材真实时长（毫秒）
  durationMs?: number;              // 生成耗时（毫秒）
  startedAt?: number; progress?: number;
  firstFrameNodeId?: string; lastFrameNodeId?: string; // lastFrame 一期仅存不提交（wan3.0 无尾帧）
  panoramaSourcePrompt?: string; panoramaFinalPrompt?: string;
  panoramaProjection?: 'equirectangular';
  directorProject?: unknown;        // 导演台 project 数据（M7 定义结构）
}

export interface CanvasNodeData {
  id: string; type: CanvasNodeType; title: string;
  position: Position; width: number; height: number;
  metadata?: CanvasNodeMetadata;
}
export interface CanvasConnection { id: string; fromNodeId: string; toNodeId: string; }

export type CanvasBackgroundMode = 'grid' | 'dots' | 'plain'; // 默认 grid
export interface CanvasSidePanelState { open: boolean; width: number; nodeId: string | null; }

export interface CanvasProject {
  id: string; title: string;
  createdAt: number; updatedAt: number;   // epoch ms（落盘口径；topwin 用 ISO 字符串，桥侧以前端值为准）
  nodes: CanvasNodeData[]; connections: CanvasConnection[];
  viewport: ViewportTransform;
  backgroundMode: CanvasBackgroundMode;
  sidePanel: CanvasSidePanelState;
  nodeCount?: number; connectionCount?: number; // sanitize 同步维护（canvas-sanitize.ts 依赖）
  schema_version: 2;
}

export const CANVAS_ASSET_DRAG_TYPE = 'application/x-infinite-canvas-asset';
export type InsertAssetPayload =
  | { kind: 'text'; content: string; title: string }
  | { kind: 'image'; dataUrl: string; title: string; storageKey?: string; width?: number; height?: number; bytes?: number; mimeType?: string }
  | { kind: 'video'; url: string; title: string; storageKey?: string; width?: number; height?: number; bytes?: number; mimeType?: string }
  | { kind: 'audio'; url: string; title: string; storageKey?: string; bytes?: number; mimeType?: string; durationMs?: number };
export type ConnectionHandle = { nodeId: string; handleType: 'source' | 'target' };
export type SelectionBoxState = {
  startWorldX: number; startWorldY: number; currentWorldX: number; currentWorldY: number;
  additive: boolean; initialSelectedNodeIds: string[]; initialSelectedConnectionIds: string[];
};
export type ContextMenuState =
  | { type: 'node'; x: number; y: number; nodeId: string }
  | { type: 'connection'; x: number; y: number; connectionId: string };
export type CanvasDirectorPanorama = {
  edgeId: string; sourceNodeId: string; imageUrl: string; fileName: string;
  projectionMode: 'equirectangular' | 'backdrop';
};
export type CanvasDirectorCapture = { dataUrl: string; fileName: string };
```

- **验证**：`cd frontend; npm run build`
- **提交**：`canvas(M2-T01): 画布类型基线 schema_version 2`

### M2-T02 后端桥：持久化四方法

- **Files**
  - Create: `gui_canvas_mixin.py`（同名重建，本任务只含持久化段）
  - Modify: `gui_bridge.py`（恢复 `from gui_canvas_mixin import CanvasBridgeMixin` import 与类继承，位置对齐删除前 :57/:1118 的同位）
  - Test: `tests/test_canvas_persistence.py`
- **Interfaces**
  - Produces（Python 签名与返回 dict 形状，`self` 依赖宿主 `GuiBridge` 的工作目录与 `_emit_raw`）：

```python
CANVAS_SCHEMA_VERSION = 2
_PROJECT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

class CanvasBridgeMixin:
    def _canvas_dir(self) -> Path: ...        # <workdir>/.ycode/canvas，一切路径 resolve 后必须在其内
    def _canvas_path(self, project_id: Any) -> Optional[Path]: ...  # 白名单不过返回 None

    def canvas_list_projects(self) -> Dict[str, Any]:
        # {"ok": True, "projects": [{"id": str, "title": str, "updatedAt": int,
        #   "schema_version": int, "supported": bool}]}   # supported = (schema_version == 2)
        ...
    def canvas_load_project(self, project_id: Any) -> Dict[str, Any]:
        # 成功 {"ok": True, "project": {...完整 CanvasProject dict, schema_version: 2}}
        # 失败 {"ok": False, "msg": str}；schema_version != 2 → msg="旧版数据，不支持"
        ...
    def canvas_save_project(self, project: Any) -> Dict[str, Any]:
        # 入参完整 project dict（含 id/schema_version）；成功 {"ok": True, "updatedAt": int}
        # 校验：id 白名单、schema_version==2、nodes/connections 为 list；原子写（mkstemp+fsync+os.replace，
        # 失败清 tmp），照抄旧 canvas_save 的原子写骨架；updatedAt 由桥侧写回 epoch ms
        ...
    def canvas_delete_project(self, project_id: Any) -> Dict[str, Any]:
        # {"ok": True}；媒体不级联删除；文件不存在也返回 ok（幂等）
        ...
```

- **步骤**：1) 按上述签名新建 mixin；`_canvas_dir` 基于宿主既有工作目录属性（对齐旧 `_canvas_path` 的取法）；2) `canvas_load_project` 读 JSON 后做结构兜底（nodes/connections 非 list 置空数组），不在此做前端 sanitize（前端加载后再跑 `sanitizeCanvasProject`）；3) gui_bridge 同位恢复 import/继承；4) 写 pytest：白名单 ID（`../x`、`a/b`、空串、超长拒绝）、原子写失败注入（mock `os.replace` 抛错→原文件不变、无 `.tmp` 残留）、schema 拒载/拒存、list 对 v1 文件标 `supported: false`、delete 幂等。
- **验证**：`python -m pytest tests/test_canvas_persistence.py -q`; `cd frontend; npm run build`
- **提交**：`canvas(M2-T02): 新桥持久化四方法+原子写/白名单测试`

### M2-T03 后端桥：媒体两方法

- **Files**
  - Modify: `gui_canvas_mixin.py`（追加媒体段）
  - Test: `tests/test_canvas_media.py`
- **Interfaces**
  - Consumes: `core.media_store.archive_media_file`、`core.media_server.media_server.public_url`（C 类保留，旧 `canvas_resolve_media` 同口径）
  - Produces:

```python
def canvas_save_media(self, data_url_or_bytes: Any, hint_name: Any = "") -> Dict[str, Any]:
    # 入参：dataURL 字符串（"data:image/png;base64,..."）或纯 base64 字符串
    # 落盘 <workdir>/.ycode/canvas/media/<uuid><ext>，再 archive_media_file 入内容寻址仓库
    # 成功 {"ok": True, "media_id": str, "path": str, "url": str, "width": int|None, "height": int|None, "bytes": int}
    #   path 为项目内相对路径（正斜杠）；url 为 media_server 预览 URL；非图片 width/height 为 None
    # 失败 {"ok": False, "msg": str}；大小上限 32MB
    ...
def canvas_resolve_media(self, media_id: Any) -> Dict[str, Any]:
    # {"ok": True, "url": str} / {"ok": False, "msg": str}；media_id 为 digest，经 media_store 查 ext 后 public_url
    ...
```

- **步骤**：1) dataURL 解析（mime→扩展名白名单 png/jpg/jpeg/webp/gif/mp4/webm/mp3/wav/m4a）；2) 落盘+归档+URL；归档失败不推翻落盘事实（url 留空，前端占位）——照旧 mixin 口径；3) pytest：dataURL 落盘往返、坏 base64 拒绝、超 32MB 拒绝、resolve 未知 id 报错、路径全部限定在 `.ycode/canvas/media/`。
- **验证**：`python -m pytest tests/test_canvas_media.py -q`
- **提交**：`canvas(M2-T03): 画布媒体落盘与预览 URL 桥`

### M2-T04 前端 store 骨架 + bridge 封装 + CanvasPane 接入

- **Files**
  - Create: `frontend/src/canvas/bridge.ts`、`frontend/src/canvas/CanvasPane.vue`、`frontend/src/stores/canvas.ts`
  - Modify: `frontend/src/components/RightPanel.vue`（占位组件换新 `CanvasPane`，import 同位替换）、`frontend/src/stores/appStore.ts`（canvas store import 恢复；`canvas_*` 分发骨架从 no-op 接回新 store；`collectGeneratedImages` 推给新 store 的 `ingestGeneratedImages`）
  - Test: `frontend/src/stores/canvas.test.ts`
- **Interfaces**
  - Produces `bridge.ts`（全量签名一次定型；生成/音频方法体本任务先抛 `canvas bridge not ready`，M4/M6 填实现）：

```ts
export interface BridgeOk { ok: boolean; msg?: string }
export interface ProjectMeta { id: string; title: string; updatedAt: number; schema_version: number; supported: boolean }
export interface SaveMediaResult extends BridgeOk { media_id: string; path: string; url: string; width: number | null; height: number | null; bytes: number }
export interface JobAck extends BridgeOk { pending?: true; job_id?: string }

export const canvasBridge = {
  listProjects(): Promise<{ ok: boolean; projects: ProjectMeta[] }>,
  loadProject(projectId: string): Promise<{ ok: boolean; project?: CanvasProject; msg?: string }>,
  saveProject(project: CanvasProject): Promise<{ ok: boolean; updatedAt?: number; msg?: string }>,
  deleteProject(projectId: string): Promise<BridgeOk>,
  saveMedia(dataUrlOrBase64: string, hintName?: string): Promise<SaveMediaResult>,
  resolveMedia(mediaId: string): Promise<{ ok: boolean; url?: string; msg?: string }>,
  // 以下 M4/M6 实装，签名在此定型：
  generateImage(params: CanvasGenerateImageParams): Promise<JobAck>,
  editImage(params: CanvasEditImageParams): Promise<JobAck>,
  generateVideo(params: CanvasGenerateVideoParams): Promise<JobAck>,
  cancelJob(jobId: string): Promise<BridgeOk>,
  audioProcess(operation: CanvasAudioOperation, mediaId: string, params: CanvasAudioParams): Promise<JobAck>,
};
// 参数类型定义于 bridge.ts（M4-T02 / M6-T03 使用）：
export interface CanvasGenerateImageParams { project_id: string; source_node_id: string; prompt: string; size?: string; model?: 'qwen-image-2.0' | 'wan2.7-image'; n?: number }
export interface CanvasEditImageParams { project_id: string; source_node_id: string; prompt: string; image_media_id: string; mask_media_id?: string; size?: string; model?: 'qwen-image-2.0' | 'wan2.7-image' }
export interface CanvasGenerateVideoParams { project_id: string; source_node_id: string; prompt: string; first_frame_media_id?: string; duration?: number; resolution?: '480P' | '720P' | '1080P' }
export type CanvasAudioOperation = 'split-av' | 'trim' | 'tempo' | 'silence-split';
export interface CanvasAudioParams { start_ms?: number; end_ms?: number; rate?: number; silence_noise_db?: number; silence_min_ms?: number }
```

  - Produces store 骨架（Pinia `useCanvasStore`）：

```ts
state: () => ({
  project: null as CanvasProject | null,
  projectList: [] as ProjectMeta[],
  selectedNodeIds: new Set<string>(), selectedConnectionIds: new Set<string>(),
  jobs: new Map<string, CanvasJob>(),   // CanvasJob = { jobId: string; nodeId: string; kind: 'image'|'video'|'audio'; status: 'loading'|'success'|'error'|'cancelled' }
  recentOutputs: [] as InsertAssetPayload[],  // 最近产出托盘（M4-T05 接 UI）
  saving: false, loadError: '',
})
actions: {
  createProject(title?: string): Promise<void>;      // id = 'p' + Date.now().toString(36) + 随机4位；空 nodes/connections；viewport {x:0,y:0,k:1}
  openProject(id: string): Promise<void>;            // load → sanitizeCanvasProject → state.project
  refreshProjectList(): Promise<void>;
  deleteProject(id: string): Promise<void>;
  scheduleSave(): void;                              // 800ms 防抖 → saveProject；保存前跑 sanitizeCanvasProject
  setViewport(v: ViewportTransform): void;           // 只改内存，随防抖落盘
  applyEditResult(data: CanvasEditResultData): void; // M4-T01 实装，本任务先占位空函数
  applyVideoResult(data: CanvasVideoResultData): void;
  focusNode(nodeId: string): void;                   // canvas_focus_node 入口：选中+视口居中（M2 先只做选中）
  ingestGeneratedImages(items: InsertAssetPayload[]): void; // collectGeneratedImages 改造落点
}
```

- **步骤**：1) bridge.ts 按签名封装 `window.pywebview.api`（就绪等待沿用前端既有桥等待工具）；2) store 骨架+防抖 save；3) CanvasPane 先渲染项目列表/新建按钮+空画布容器（InfiniteCanvas 由 T06 挂入）；4) RightPanel/appStore 三处接线（设计 §2.3 行号口径）；5) vitest：createProject 字段齐全且 schema_version=2、scheduleSave 防抖合并、focusNode 选中、ingestGeneratedImages 追加去重。
- **验证**：`cd frontend; npm run build; npx vitest run src/stores/canvas.test.ts`; `python -m pytest tests/test_canvas_*.py -q`
- **提交**：`canvas(M2-T04): store骨架+bridge封装+页签接入`

### M2-T05 utils 直译第一批（7 文件）+ 单测

- **Files**
  - Create: `frontend/src/canvas/utils/canvas-node-size.ts`、`canvas-sanitize.ts`、`canvas-image-data.ts`、`canvas-video-frame.ts`、`canvas-video-duration.ts`、`canvas-audio-duration.ts`、`canvas-panorama.ts`（均同名直译，替换点见 §0.6）
  - Test: `frontend/src/canvas/utils/__tests__/canvas-node-size.test.ts`、`canvas-sanitize.test.ts`、`canvas-image-data.test.ts`、`canvas-video-frame.test.ts`、`canvas-duration.test.ts`（video+audio 合并一文件）、`canvas-panorama.test.ts`
- **Interfaces**（导出签名与 topwin 逐字一致，列关键项）：
  - `fitNodeSize(width, height, maxWidth=640, maxHeight=640): {width,height}`；`nodeSizeFromRatio(size: string, baseWidth, baseHeight): {width,height} | null`
  - `sanitizeCanvasProject<T extends Pick<CanvasProject,'nodes'|'connections'|'nodeCount'|'connectionCount'>>(project: T): T`；`isDestructiveCanvasSnapshot(nodes, connections): boolean`；`isSuspiciousCanvasNodeDrop(previousNodes, nextNodes): boolean`；`isSuspiciousCanvasProjectShrink(previous, next): boolean`；`shouldPreferRicherCanvasSnapshot(previous, next): boolean`
  - `cropDataUrl(dataUrl: string, crop?: ImageCropRect): Promise<string>`；`splitDataUrl(dataUrl, params: ImageSplitParams): Promise<ImageSplitPiece[]>`；`transformAngleDataUrl(dataUrl, params: ImageAngleTransform): Promise<string>`；`upscaleDataUrl(dataUrl, params: ImageUpscaleParams): Promise<string>`；`resolveUpscaleSize(width, height, targetLongEdge): {width,height}`；类型 `ImageCropRect{x,y,width,height}`（0-1 归一化）、`ImageAngleTransform{horizontalAngle,pitchAngle,cameraDistance,wideAngle}`、`ImageUpscaleParams{targetLongEdge,algorithm:'nearest'|'bilinear'|'high'}`、`ImageSplitParams{horizontalLines:number[],verticalLines:number[]}`、`ImageSplitPiece{row,column,dataUrl}`、`MAX_UPSCALE_LONG_EDGE=4096`
  - `captureVideoFrame(video: HTMLVideoElement, timeSec?): Promise<{dataUrl,width,height}>`；`captureVideoFrameByKind(video, kind:'first'|'last'|'current')`；`captureVideoPosterFromUrl(url): Promise<{dataUrl,width,height}|null>`
  - `probeVideoDurationMs(url, timeoutMs=10000): Promise<number|undefined>`；`canvasVideoProbeUrl(node): string`；`probeAudioDurationMs(url, timeoutMs=8000)`；`canvasAudioProbeUrl(node): string`
  - `PANORAMA_IMAGE_SIZE='2:1'`；`PANORAMA_NODE_SIZE={width:340,height:170}`；`isPanoramaNodeType(type)`；`isCanvasImageNodeType(type)`；`buildPanoramaPrompt(userPrompt, hasReferenceImages): string`
- **步骤**：逐文件从参考仓复制→按 §0.6 替换平台调用→写单测。关键用例（从源码行为推导）：fitNodeSize 最小 48/不放大；nodeSizeFromRatio 比例 <0.25 或 >4 回退基准、`"1280x720"` 与 `"16:9"` 均可解析；sanitize 悬空连线剔除+计数同步；isSuspiciousCanvasNodeDrop 的 3/8/75% 阈值边界；resolveUpscaleSize 长边钳制 4096；split 切线排序去界外；probe 超时返回 undefined（fake timer + stub media 元素）。
- **验证**：`cd frontend; npx vitest run src/canvas/utils; npm run build`
- **提交**：`canvas(M2-T05): utils直译第一批(尺寸/防护/像素/抽帧/时长/全景)`

### M2-T06 InfiniteCanvas 渲染底座 + 视口持久化

- **Files**
  - Create: `frontend/src/canvas/render/InfiniteCanvas.vue`
  - Modify: `frontend/src/canvas/CanvasPane.vue`（挂入）
  - Test: `frontend/src/canvas/render/__tests__/InfiniteCanvas.test.ts`
- **Interfaces**
  - Consumes: `useCanvasStore().project.viewport`、`setViewport`
  - Produces 组件契约：

```ts
// InfiniteCanvas.vue
props: { viewport: ViewportTransform }              // 受控
emits: { 'update:viewport': [v: ViewportTransform] }
slots: { default(): VNode[] }                        // 世界坐标内容层
expose: { screenToWorld(x: number, y: number): Position; worldToScreen(p: Position): Position }
```

- **步骤**：对照参考仓 `components/infinite-canvas.tsx`：世界容器 `transform: translate(x px, y px) scale(k)`；48px 网格背景（backgroundMode 三态）；滚轮缩放锚定指针（`k' = clamp(k * exp(-deltaY*0.001), 0.05, 4)`，锚点公式 `x' = mx - (mx-x)*k'/k`，系数以参考仓源码为准核正）；空格+左键 / 中键拖拽抓手；`will-change: transform`。视口变更 emit → CanvasPane 调 `setViewport` 走防抖落盘。单测：screenToWorld/worldToScreen 互逆、滚轮锚点公式、缩放钳制。
- **验证**：`cd frontend; npm run build; npx vitest run src/canvas/render`
- **提交**：`canvas(M2-T06): 自研平移缩放渲染底座`

**M2 手工验收清单**：新建项目→拖拽/缩放画布→关闭重开项目，视口与空画布恢复；项目列表含旧 v1 文件时显示「旧版数据，不支持」且不可打开；`python -m pytest tests/test_canvas_*.py -q` 全绿。


---

## M3 节点与连线（7 任务）

### M3-T01 NodeShell + 八类节点展示态
- **Files** — Create: `frontend/src/canvas/nodes/NodeShell.vue`、`ImageNode.vue`、`PanoramaNode.vue`、`TextNode.vue`、`ConfigNode.vue`、`VideoNode.vue`、`AudioNode.vue`、`DirectorNode.vue`、`GroupNode.vue`、`nodes/index.ts`（类型→组件映射表）；Modify: `CanvasPane.vue`（按 project.nodes 渲染）；Test: `nodes/__tests__/NodeShell.test.ts`
- **Interfaces** — Produces: `NodeShell` props `{ node: CanvasNodeData; selected: boolean; interactive: boolean }`，emits `{ action: [payload: { nodeId: string; action: string; args?: Record<string, unknown> }] }`（节点只发事件不碰 store，沿用星瑶旧画布约定）；slot `default` 为节点内容。八类节点默认尺寸对齐参考仓 `constants.ts` 的 `NODE_DEFAULT_SIZE`（image 340×260 / panorama 340×170 / text 320×200 / config 360×280 / video 360×240 / audio 360×168 / director 360×320 / group 760×480，执行时以参考仓核对）。
- **步骤**：NodeShell 画标题栏/选中描边/八向缩放手柄/左右连接端口；内容区各节点：image/panorama 显示 `metadata.content`（media URL）或占位、video/audio 用原生 `<video>/<audio>` 控件、text 双击进入编辑（textarea，失焦落 store）、config 显示参数摘要、director 显示「打开导演台」按钮（M7 接线）、group 半透明容器。loading 态遮罩（`metadata.status==='loading'`）+ error 态红边与 `errorDetails` 提示。
- **验证**：`cd frontend; npm run build; npx vitest run src/canvas/nodes`
- **提交**：`canvas(M3-T01): NodeShell与八类节点展示态`

### M3-T02 节点拖拽/缩放/分组吸附 + utils 第二批
- **Files** — Create: `frontend/src/canvas/utils/canvas-group.ts`、`canvas-material-title.ts`（直译，§0.6）；Modify: `stores/canvas.ts`（`moveNodes`/`resizeNode` action）、`NodeShell.vue`（指针交互）；Test: `utils/__tests__/canvas-group.test.ts`
- **Interfaces** — Consumes: 参考仓 `canvas-group.ts` 的 `GROUP_PADDING/findContainingGroupId/findGroupDropTarget/getNodeBounds/snapNodesIntoGroup`（签名逐字保留）；Produces store action：`moveNodes(ids: string[], dx: number, dy: number): void`、`resizeNode(id: string, rect: { x?: number; y?: number; width: number; height: number }): void`（均触发 scheduleSave）。
- **步骤**：指针拖拽用 world 坐标增量；拖拽结束跑 `findGroupDropTarget`+`snapNodesIntoGroup`（进组写 `metadata.groupId`，出组清除）；group 拖动带动组内子节点；缩放手柄按 `freeResize` 与媒体纵横比约束（`fitNodeSize`）。单测：吸附边界（GROUP_PADDING 内外）、进出组 groupId 变更、组拖动子节点跟随。
- **验证**：`cd frontend; npx vitest run src/canvas/utils; npm run build`
- **提交**：`canvas(M3-T02): 拖拽缩放与分组吸附`

### M3-T03 框选 + SelectionBox + 选区工具条
- **Files** — Create: `render/SelectionBox.vue`、`tools/SelectionToolbar.vue`；Modify: `InfiniteCanvas.vue`（空白处拖拽出框）、`stores/canvas.ts`（选中集 action）；Test: `render/__tests__/SelectionBox.test.ts`
- **Interfaces** — Consumes: `SelectionBoxState`（M2-T01）；Produces: `setSelection(nodeIds: string[], connectionIds: string[], additive: boolean)`、`clearSelection()`；SelectionToolbar 按钮：删除/复制/成组/取消组（emit 事件由 CanvasPane 落 store）。
- **步骤**：对照参考仓框选逻辑：左键空白拖拽画矩形（世界坐标），命中判定用 `getNodeBounds` 相交；Shift 加选（initialSelected* 快照）；选中连线同步高亮。单测：矩形相交命中、加选合并、拖拽阈值（<4px 视为点击清选）。
- **验证**：`cd frontend; npm run build; npx vitest run src/canvas/render`
- **提交**：`canvas(M3-T03): 框选与选区工具条`

### M3-T04 资源引用连线 + reference-guard + utils 第三批
- **Files** — Create: `render/ConnectionPath.vue`、`utils/canvas-reference-guard.ts`、`utils/canvas-resource-references.ts`、`utils/canvas-generation-inputs.ts`（§0.6 直译与裁剪）；Modify: `stores/canvas.ts`（`connect`/`disconnect` action）、`NodeShell.vue`（端口拖拽）；Test: `utils/__tests__/canvas-reference-guard.test.ts`、`render/__tests__/ConnectionPath.test.ts`
- **Interfaces** — Consumes: 参考仓 `ConnectionPath/ActiveConnectionPath/connectionIntersectsWorldRect` 行为；Produces: `connect(fromNodeId: string, toNodeId: string): { ok: boolean; reason?: string }`（先过 guard：类型兼容表按 tokenrhythm 能力重写——image→image 改图参考、image→video 首帧、text→config/生成节点 prompt、audio→video 参考音频占位拒绝并提示「音频参考暂未接入」、panorama→director；防自连/防重复/防环）、`disconnect(connectionId: string)`；`buildCanvasResourceReferences(node, nodes, connections)`（产出「图片N/视频N/文本N」标签与媒体清单，Seedance 分支裁剪）。
- **步骤**：SVG 贝塞尔曲线（世界坐标，控制点水平外延），拖拽中画 ActiveConnectionPath 虚线；连线命中用 `connectionIntersectsWorldRect` 供框选；违规连线弹 `referenceBlameNotes` 红框角标（沿用 topwin 交互）。单测：guard 类型矩阵逐格、防环、标签编号稳定性。
- **验证**：`cd frontend; npx vitest run src/canvas; npm run build`
- **提交**：`canvas(M3-T04): 资源引用连线与校验`

### M3-T05 撤销重做 + 复制粘贴 + 快捷键
- **Files** — Create: `utils/history.ts`、`utils/clipboard.ts`；Modify: `stores/canvas.ts`（历史栈接线）、`CanvasPane.vue`（键盘事件）；Test: `utils/__tests__/history.test.ts`、`clipboard.test.ts`
- **Interfaces** — Produces: `HistoryStack`（`push(snapshot: { nodes; connections })` / `undo(): snapshot | null` / `redo()`，容量 50，防抖合并 300ms 内连续变更）；clipboard 序列化标记 `xingyao-canvas:nodes:`（JSON，含节点与内部连线），`Ctrl+C/V/X/D`、`Delete`、`Ctrl+Z/Y`、`Ctrl+A`；粘贴偏移 +24px、id 重生成、组关系保留。
- **步骤**：store 每次落盘级变更 push 快照；undo/redo 恢复后 scheduleSave；粘贴跨项目可用（媒体引用按 storageKey 相对路径解析，缺失显示占位）。单测：合并窗口、栈容量、粘贴 id 唯一与连线重映射。
- **验证**：`cd frontend; npx vitest run src/canvas/utils; npm run build`
- **提交**：`canvas(M3-T05): 撤销重做与复制粘贴`

### M3-T06 小地图 + 缩放控件 + ContextMenu
- **Files** — Create: `render/MiniMap.vue`、`render/ZoomControls.vue`、`tools/NodeContextMenu.vue`；Modify: `CanvasPane.vue`；Test: `render/__tests__/MiniMap.test.ts`
- **Interfaces** — Consumes: `viewport`、`project.nodes`；Produces: MiniMap props `{ nodes; viewport; containerSize }`，emits `{ navigate: [center: Position] }`；ContextMenu 按 `ContextMenuState` 渲染节点菜单（复制/删除/成组/各类型工具入口占位）与连线菜单（删除）。
- **步骤**：对照参考仓 `canvas-mini-map.tsx`：世界包围盒归一化缩略、视口矩形、点击/拖拽导航；ZoomControls：±/百分比/适应内容（fitView 按节点包围盒 padding 80）。单测：包围盒归一化、点击坐标反算。
- **验证**：`cd frontend; npm run build; npx vitest run src/canvas/render`
- **提交**：`canvas(M3-T06): 小地图缩放控件与右键菜单`

### M3-T07 项目导出/导入 + 侧栏
- **Files** — Create: `tools/ExportDialog.vue`、`canvas-side-panel/ProjectList.vue`；Modify: `CanvasPane.vue`；Test: `stores/canvas.export.test.ts`
- **Interfaces** — Produces: 导出 = 当前 CanvasProject JSON 下载（文件名 `<title>.canvas.json`）+ 选中图片批量下载（JSZip 不引新依赖，逐张触发下载）；导入 = 文件选择器读 JSON → sanitize → 白名单 id 重生成 → createProject；侧栏 = 项目列表/新建/重命名/删除（调 M2 桥）。
- **步骤**：导入时 schema_version≠2 明确报「旧版数据，不支持」；导出剥掉运行时字段（jobId/status loading 归 idle）。单测：导入 id 重映射、悬空连线剔除、导出剥离。
- **验证**：`cd frontend; npx vitest run src/stores/canvas.export.test.ts; npm run build`
- **提交**：`canvas(M3-T07): 导出导入与项目侧栏`

**M3 手工验收清单**：八类节点建/拖/缩/删；分组吸附进出；框选加选；连线类型校验与红框提示；Ctrl+Z/Y/C/V/D 全套；小地图导航；导出重导入还原。

---

## M4 生成链路（5 任务，可与 M5/M6 并行）

### M4-T01 事件契约扩展 + store 回填
- **Files** — Modify: `frontend/src/stores/appStore.ts`（`CanvasEditResultData`/`CanvasVideoResultData` 增 `job_id/project_id/status/images/video` 可选字段，解析函数容错扩展，旧字段不改名）、`stores/canvas.ts`（`applyEditResult`/`applyVideoResult` 实装：按 jobId 匹配 jobs 表→更新源节点或新建产物节点+连线→清 job）；Test: `stores/canvas.jobs.test.ts`
- **Interfaces** — Consumes: §0.5 payload 契约；Produces: `applyEditResult(data: CanvasEditResultData): boolean`（认领成功 true）、`applyVideoResult(data: CanvasVideoResultData): boolean`。
- **步骤**：产物节点落源节点右侧 +56px、连「改图」/「视频」边（沿用旧画布溯源口径）；status=cancelled 清 job 不落节点；无主 job（画布已关）落 recentOutputs 兜底。单测：认领/未认领/取消/多图 images 数组。
- **验证**：`cd frontend; npx vitest run src/stores/canvas.jobs.test.ts; npm run build`
- **提交**：`canvas(M4-T01): 生成事件回填与job表`

### M4-T02 桥生成三方法 + 取消 + 并发闸门
- **Files** — Modify: `gui_canvas_mixin.py`（生成段）；Test: `tests/test_canvas_generation_bridge.py`
- **Interfaces** — Consumes: `core.tool_registry` 执行 `tokenrhythm_generate_image/edit_image/generate_video`（工具名与入参以插件注册表实际 schema 为准，执行时读 `plugins/tokenrhythm_image/plugin.py`、`plugins/tokenrhythm_video/plugin.py` 核对）；Produces: `canvas_generate_image/canvas_edit_image/canvas_generate_video(params: dict) -> {"ok": True, "pending": True, "job_id": str}`、`canvas_cancel_job(job_id) -> {"ok": True}`；事件走宿主 `_emit_raw`（§0.5 payload）。
- **步骤**：照旧 mixin 三段式重写：入口校验（prompt 非空、媒体存在、单画布同类闸门 `_CANVAS_*_JOBS`+锁）→受理返回 job_id→守护线程执行工具→产物归档 media/→emit 事件→释放闸门；取消置标记、结果丢弃。插件未启用返回 `{"ok": False, "msg": "请在设置页启用 tokenrhythm_image/video 并配置 API Key"}`。pytest 全 mock 工具注册表：受理/回填/失败/取消/闸门连点拒绝/插件缺失，零真实计费。
- **验证**：`python -m pytest tests/test_canvas_generation_bridge.py -q`
- **提交**：`canvas(M4-T02): 生成桥三方法与并发闸门`

### M4-T03 ConfigNode 参数面板 + 生成入口
- **Files** — Create: `tools/ConfigPanel.vue`、`tools/PromptComposer.vue`；Modify: `ConfigNode.vue`、`ImageNode.vue`/`VideoNode.vue`（生成按钮）、`stores/canvas.ts`（`submitGeneration(nodeId)` 汇集上游引用→bridge）；Test: `tools/__tests__/ConfigPanel.test.ts`
- **Interfaces** — Consumes: `CanvasGenerateImageParams` 等（M2-T04 bridge.ts）；Produces: 参数面板字段=prompt/模型（qwen-image-2.0|wan2.7-image）/尺寸（六档）/张数 1-4/负向提示（拼 prompt 尾）；视频=首帧引用/时长 2-30 或 -1/分辨率三档。
- **步骤**：对照参考仓 `canvas-config-composer.tsx` 交互（@提及上游资源、Enter 提交）；MJ/Kling/Seedance 面板不实现；提交经 `buildCanvasResourceReferences` 拼参考清单；音频模式按钮置灰+提示「音频生成暂未接入」。单测：参数映射到 bridge 入参、负向拼接、音频禁用。
- **验证**：`cd frontend; npm run build; npx vitest run src/canvas/tools`
- **提交**：`canvas(M4-T03): 生成配置面板与提交链路`

### M4-T04 批次生成
- **Files** — Create: `tools/BatchGenerateDialog.vue`、`utils/canvas-batch.ts`；Modify: `stores/canvas.ts`；Test: `utils/__tests__/canvas-batch.test.ts`
- **Interfaces** — Produces: `expandBatch(root: CanvasNodeData, prompts: string[]): CanvasNodeData[]`（isBatchRoot/batchRootId/batchChildIds 维护，网格排布列宽+56）；桥侧队列串行执行（同画布闸门天然限流）。
- **步骤**：多行 prompt→子节点矩阵；tokenrhythm n>1 的多图结果展开为 sibling 节点（primaryImageId 口径）；失败子节点独立标错不影响批次。单测：展开坐标、n 图展开幂等（jobId 防重）。
- **验证**：`cd frontend; npx vitest run src/canvas/utils; npm run build`
- **提交**：`canvas(M4-T04): 批次生成`

### M4-T05 最近产出托盘接通
- **Files** — Create: `tools/RecentTray.vue`；Modify: `CanvasPane.vue`、`stores/canvas.ts`（`ingestGeneratedImages` 补 URL 解析：路径经 `canvas_resolve_media` 换预览）；Test: `stores/canvas.tray.test.ts`
- **Interfaces** — Consumes: appStore `collectGeneratedImages`（M1 保留骨架，M2-T04 已接线）；Produces: 托盘容量 8、点击落节点（去重键=路径）、清空。
- **步骤**：沿用旧画布托盘交互口径（左下浮层、有内容才渲染）；聊天生图→托盘→点击入画布端到端。单测：去重、容量出栈、落节点。
- **验证**：`cd frontend; npx vitest run src/stores/canvas.tray.test.ts; npm run build`
- **提交**：`canvas(M4-T05): 最近产出托盘`

**M4 手工验收清单**（真实计费，人工执行）：文生图 1 张回填；参考改图带连线溯源；图生视频 5s 回填可播放；连点被闸门拒绝；取消不回填；聊天生图进托盘点击入画布；插件未启用提示正确。

---

## M5 图像工具（4 任务，可与 M4/M6 并行）

### M5-T01 裁剪/切分/放大三弹窗
- **Files** — Create: `tools/CropDialog.vue`、`SplitDialog.vue`、`UpscaleDialog.vue`；Modify: `ImageNode.vue` 工具入口、`NodeContextMenu.vue`；Test: `tools/__tests__/CropDialog.test.ts`
- **Interfaces** — Consumes: M2-T05 直译的 `cropDataUrl/splitDataUrl/upscaleDataUrl`（像素逻辑零重写）；Produces: 产物经 `canvas_save_media` 落盘→新 image 节点落源右侧+「裁剪/切分/放大」连线。
- **步骤**：UI 对照参考仓三个 dialog（裁剪框拖拽/比例锁定、切分横竖线拖动、放大长边+算法三档）；切分多产物网格排布。单测：弹窗参数→utils 入参映射（canvas stub）。
- **验证**：`cd frontend; npm run build; npx vitest run src/canvas/tools`
- **提交**：`canvas(M5-T01): 裁剪切分放大弹窗`

### M5-T02 蒙版改图
- **Files** — Create: `tools/MaskEditDialog.vue`；Modify: `stores/canvas.ts`（提交走 `editImage` 带 mask_media_id）；Test: `tools/__tests__/MaskEditDialog.test.ts`
- **Interfaces** — Consumes: 参考仓 `canvas-node-mask-edit-dialog.tsx` 行为（画笔涂抹白色蒙版、笔刷粗细、橡皮、撤销）；Produces: `{ prompt, image_media_id, mask_media_id }` 提交 `canvas_edit_image`。
- **步骤**：底图只显示不 drawImage（跨源标脏坑沿用旧画布注释口径：media_server 与前端不同端口）；蒙版独立 canvas 导出 PNG dataURL→saveMedia；tokenrhythm edit_image 无独立 mask 入参时把蒙版语义拼入 prompt（「白色涂抹区域替换为…」）并附蒙版合成图——执行时读插件 schema 定案，偏差登记实现说明。
- **验证**：`cd frontend; npm run build; npx vitest run src/canvas/tools`
- **提交**：`canvas(M5-T02): 蒙版改图`

### M5-T03 涂鸦标注
- **Files** — Create: `tools/DoodleDialog.vue`；Test: `tools/__tests__/DoodleDialog.test.ts`
- **Interfaces** — Consumes: 旧画布 annotation.ts 坐标换算口径（git 历史找回：显示坐标→原图像素、越界夹取、三档饱和色）；Produces: 透明笔迹层 dataURL+prompt→`canvas_edit_image`（后端合成或直接垫图，按插件能力定案）。
- **步骤**：笔迹层与原图同尺寸导出；底图不 drawImage；提交后产物落源右侧带溯源边。单测：坐标换算边界（直译旧 annotation.test.ts 用例）。
- **验证**：`cd frontend; npx vitest run src/canvas/tools; npm run build`
- **提交**：`canvas(M5-T03): 涂鸦标注改图`

### M5-T04 旋转角度（视角变换）
- **Files** — Create: `tools/RotateViewDialog.vue`；Test: `tools/__tests__/RotateViewDialog.test.ts`
- **Interfaces** — Consumes: `transformAngleDataUrl`（M2-T05）与参考仓 `canvas-camera.ts` 的 `applyCameraPrompt`；Produces: 本地透视近似预览+相机参数拼 prompt 的改图提交（horizontalAngle/pitchAngle/cameraDistance/wideAngle）。
- **步骤**：对照参考仓 `canvas-node-angle-dialog.tsx`：滑杆实时 Canvas2D 近似预览，提交走 `canvas_edit_image` 把相机句拼入 prompt。单测：参数→prompt 句映射。
- **验证**：`cd frontend; npm run build; npx vitest run src/canvas/tools`
- **提交**：`canvas(M5-T04): 旋转角度工具`

**M5 手工验收清单**：五工具产物像素方向正确、落盘可预览、溯源连线齐全；蒙版/涂鸦在大图（4000px）上笔迹位置不漂移。

---

## M6 视频/音频/全景（4 任务，可与 M4/M5 并行）

### M6-T01 视频节点增强 + 抽帧
- **Files** — Modify: `VideoNode.vue`（播放器/首尾帧按钮/进度）；Create: `tools/VideoFrameActions.ts`；Test: `nodes/__tests__/VideoNode.test.ts`
- **Interfaces** — Consumes: `captureVideoFrameByKind/captureVideoPosterFromUrl/probeVideoDurationMs`（M2-T05）；Produces: 首帧/尾帧/当前帧→saveMedia→新 image 节点+「抽帧」边；首帧节点可作视频生成参考（firstFrameNodeId）。
- **步骤**：对照参考仓 `canvas-video-player.tsx`：播放错误分类提示、时长探测回填 `mediaDurationMs`；抽帧在 loadeddata 后执行。单测：抽帧 kind 分发（video stub）。
- **验证**：`cd frontend; npm run build; npx vitest run src/canvas/nodes`
- **提交**：`canvas(M6-T01): 视频播放与抽帧`

### M6-T02 音频节点 + 播放器
- **Files** — Modify: `AudioNode.vue`；Create: `tools/AudioPlayer.vue`；Test: `nodes/__tests__/AudioNode.test.ts`
- **Interfaces** — Consumes: `probeAudioDurationMs`；Produces: 波形占位（不做真实波形解码，一期显示时长+播放条）、播放/暂停/进度、工具入口（M6-T03）。
- **验证**：`cd frontend; npm run build; npx vitest run src/canvas/nodes`
- **提交**：`canvas(M6-T02): 音频节点播放器`

### M6-T03 桥音频四操作（vendor/ffmpeg）
- **Files** — Modify: `gui_canvas_mixin.py`（音频段，独立函数追加）；Test: `tests/test_canvas_audio_bridge.py`
- **Interfaces** — Produces: `canvas_audio_process(operation, media_id, params) -> {"ok": True, "pending": True, "job_id"}`；完成事件 `canvas_audio_result`（新事件，payload `{ok, job_id, operation, outputs: [{media_id, path, url, duration}], error}`；appStore 增一个分发分支→store `applyAudioResult`）。ffmpeg 命令：split-av=`-vn -acodec copy` + `-an -vcodec copy` 双输出；trim=`-ss <start> -to <end> -acodec copy`（非对齐时重编码 aac）；tempo=`-filter:a atempo=<rate>`（0.5-2 之外链式）；silence-split=`silencedetect=n=<noise>dB:d=<min_s>` 解析 stderr 后分段导出。
- **步骤**：ffmpeg 路径取 `vendor/ffmpeg`（打包口径与既有随包用法一致，执行时 grep 现用法核对）；subprocess 超时 120s、产物落 media/；pytest 用 1s 正弦 wav 夹具真实跑 ffmpeg（本地零计费），四操作产物存在且时长合理；ffmpeg 缺失返回明确 msg。
- **验证**：`python -m pytest tests/test_canvas_audio_bridge.py -q`
- **提交**：`canvas(M6-T03): 本地ffmpeg音频四操作`

### M6-T04 全景节点 + 查看器
- **Files** — Modify: `frontend/package.json`（增 `@photo-sphere-viewer/core`）；Create: `tools/PanoramaViewer.vue`；Modify: `PanoramaNode.vue`（双击开查看器）、`ConfigPanel.vue`（全景生成模式：size 强制 2:1+`buildPanoramaPrompt`）；Test: `tools/__tests__/PanoramaViewer.test.ts`
- **Interfaces** — Consumes: `@photo-sphere-viewer/core`（MIT）；Produces: 查看器 props `{ url: string }`，emits `{ capture: [dataUrl: string], close: [] }`（当前视角截图→新 image 节点）；全景生成走 `canvas_generate_image`（prompt 经 buildPanoramaPrompt 包装）。
- **步骤**：对照参考仓 `canvas-panorama-viewer.tsx`（dynamic import 同款懒加载）；非 2:1 图提示「非等距柱状投影，显示可能变形」。单测：懒加载与 props 传递（viewer 本体 mock）。
- **验证**：`cd frontend; npm install; npm run build; npx vitest run src/canvas/tools`
- **提交**：`canvas(M6-T04): 全景查看器与全景生成`

**M6 手工验收清单**：抽帧三 kind 出图；四音频操作产物可播放（trim 边界/tempo 0.5 与 2/silence-split 分段数合理）；全景拖拽视角+截图回画布；视频首帧→生成视频端到端。

---

## M7 导演台（6 任务，依赖 M2 与 M6-T04）

### M7-T01 子项目脚手架 + 握手
- **Files** — Create: `frontend/director-src/`（package.json：vite+three+typescript；index.html；src/main.ts、src/protocol.ts）、构建脚本 `frontend/director-src/build.ps1`（npm run build → 产物拷 `frontend/public/director/`）；Test: `frontend/director-src/src/protocol.test.ts`（vitest）
- **Interfaces** — Produces `protocol.ts`：设计 §6.2 七种消息的 TS 类型与 `parseDirectorMessage(raw): DirectorMessage | null`（type 前缀 `storyai:` 校验、payload 形状校验）；main.ts 加载即发 `director-ready`，收 `director-session` 存 `{instanceId, theme, project}`、收 `director-panoramas` 存列表；关闭按钮发 `director-close`。
- **验证**：`cd frontend/director-src; npm install; npm run build; npx vitest run`；产物存在 `frontend/public/director/index.html`
- **提交**：`canvas(M7-T01): 导演台脚手架与消息握手`

### M7-T02 全景环境球
- **Files** — Create: `frontend/director-src/src/scene.ts`（Three.js 场景/渲染器/OrbitControls）、`panorama.ts`（equirectangular→球内壁 MeshBasicMaterial side:BackSide）；Modify: main.ts
- **Interfaces** — Consumes: `director-panoramas` 列表；Produces: `setPanorama(imageUrl: string): Promise<void>`（TextureLoader，失败提示）；全景切换 UI（列表下拉）；移除按钮→发 `director-panorama-removed {edgeId, sourceNodeId}`。
- **步骤**：相机在球心、fov 75；projectionMode=backdrop 时改为平面背景板。验收：CC0 全景图正确无翻转。
- **验证**：`cd frontend/director-src; npm run build` + 浏览器手工验收
- **提交**：`canvas(M7-T02): 全景环境球`

### M7-T03 几何体与 gizmo
- **Files** — Create: `frontend/director-src/src/objects.ts`（立方体/球/圆柱/平面增删）、`gizmo.ts`（TransformControls：translate/rotate/scale 三模式）
- **Interfaces** — Produces: project 数据结构定稿 `{ objects: [{id, kind, position:[x,y,z], rotation:[x,y,z], scale:[x,y,z], color}], cameras: M7-T04, panoramaEdgeId: string | null }`；每次变更节流 500ms 发 `director-project-changed {project}`。
- **验证**：构建 + 手工（增删/三模式变换/变更消息节流）
- **提交**：`canvas(M7-T03): 几何体摆放与gizmo`

### M7-T04 机位管理
- **Files** — Create: `frontend/director-src/src/cameras.ts`；Modify: main.ts（机位列表 UI）
- **Interfaces** — Produces: `CameraBookmark { id, name, position:[x,y,z], target:[x,y,z], fov }`；保存当前视角/切换/重命名/删除；入 project.cameras 持久化。
- **验证**：构建 + 手工（两机位切换视角一致）
- **提交**：`canvas(M7-T04): 相机机位管理`

### M7-T05 批量截图
- **Files** — Create: `frontend/director-src/src/capture.ts`
- **Interfaces** — Produces: `captureBookmarks(ids: string[]): Promise<{dataUrl, fileName}[]>`（逐机位摆相机→render→`toDataURL('image/png')`，renderer 需 `preserveDrawingBuffer: true`）；「发送选中机位截图」按钮→`director-captures-sent {captures}`。
- **验证**：构建 + 手工（两机位两张 PNG 内容不同且视角正确）
- **提交**：`canvas(M7-T05): 多视角批量截图`

### M7-T06 画布侧 DirectorNode 集成
- **Files** — Create: `frontend/src/canvas/tools/DirectorFrame.vue`（iframe 容器+消息桥接，对照参考仓 canvas-director.tsx 逐消息对齐）；Modify: `DirectorNode.vue`（打开按钮）、`stores/canvas.ts`（project-changed 回写 metadata.directorProject、panorama-removed 删连线、captures-sent 逐张 saveMedia→image 节点）；Test: `tools/__tests__/DirectorFrame.test.ts`
- **Interfaces** — Consumes: M7-T01 protocol 契约（前端侧同样做 type/shape 校验）；全景列表由 `buildCanvasResourceReferences` 过滤 panorama→director 连线生成。
- **步骤**：iframe src=`director/index.html`（Vite base 相对路径，打包后同源）；ready 后发 session（带 metadata.directorProject）与 panoramas；origin 校验不放宽。单测：七消息路由（iframe mock）。
- **验证**：`cd frontend; npm run build; npx vitest run src/canvas/tools` + 设计 §6.4 五条手工验收
- **提交**：`canvas(M7-T06): 导演台画布集成`

**M7 手工验收清单**：设计 §6.4 五条全过（握手/持久化恢复/批量截图落节点/移除全景删线/主题与关闭）。

---

## M8 打磨回归（3 任务）

### M8-T01 规格查漏
- **Files** — Modify: 视查漏结果而定；Create: `docs/canvas-spec-deviations.md`（偏差登记表）
- **步骤**：对照参考仓 canvas-client-page.tsx 按功能域分段核对快捷键/边界态/拖拽手感/错误文案；每处偏差要么修齐要么登记裁剪理由（云端相关一律登记裁剪）。验收：登记表覆盖全部八功能域，无未解释偏差。
- **提交**：`canvas(M8-T01): 规格查漏与偏差登记`

### M8-T02 性能压测与虚拟化
- **Files** — Modify: `InfiniteCanvas.vue`/`NodeShell.vue`（视口外节点内容降级占位、图片懒加载）；Create: `frontend/src/canvas/__bench__/perf.md`（压测记录）
- **步骤**：脚本造 300 节点（含 50 图）项目；拖拽/缩放帧率目标 ≥30fps（DevTools performance 面板量化）；不达标依次上：视口裁剪→缩略图→will-change 收敛。验收：压测记录达标。
- **提交**：`canvas(M8-T02): 大画布性能优化`

### M8-T03 全量回归与文档
- **Files** — Modify: `docs/all_features.md`（画布章节重写）、`更新说明.md`
- **步骤**：`cd frontend; npm run build; npx vitest run` 全绿；`python -m pytest tests/ -q`（秘书体系 7 条已知隔离性失败按基线口径不算回归）；C 类共用面（附件缩略图/角色库/画笔截图/内嵌浏览器）手工回归；M2-M7 六份手工验收清单复跑。验收：全绿+清单复跑记录。
- **提交**：`canvas(M8-T03): 全量回归与文档更新`

---

## 附：任务索引与并行派发

| 组 | 任务数 | 可并行 |
| --- | --- | --- |
| M2 | 6 | T02/T03/T05 并行，其余串行 |
| M3 | 7 | T01/T02 并行，依赖见 §0.8 |
| M4 | 5 | 与 M5/M6 组间并行 |
| M5 | 4 | 与 M4/M6 组间并行 |
| M6 | 4 | 与 M4/M5 组间并行 |
| M7 | 6 | T01-T05 串行，T06 依赖 M6-T04 |
| M8 | 3 | 串行收尾 |

共 35 任务。共享文件冲突约定见 §0.8（gui_canvas_mixin.py 与 stores/canvas.ts 按函数级追加合并）。

## 附：自查记录

- 设计 M2-M8 交付项逐条对照：M2（底座/持久化/媒体/store/utils/渲染）→T01-T06；M3（节点/连线/框选/分组/复制/撤销/小地图/导出）→T01-T07；M4（生成三桥/job 回填/config/批次/托盘）→T01-T05；M5（裁剪/切分/放大/蒙版/涂鸦/旋转）→T01-T04；M6（抽帧/音频四操作/全景）→T01-T04；M7（设计 §6 全部）→T01-T06；M8（查漏/压测/回归）→T01-T03。无遗漏。
- 接口一致性：viewport 统一 `{x,y,k}`（M2-T01 定稿，M2-T06/M3-T06 引用一致）；事件 payload 统一 §0.5；bridge 签名统一 M2-T04；job 认领统一 jobId 字段。
- 占位符检查：全文无 TBD/TODO；「执行时核对」均给出核对来源文件与定案规则，属受控偏差登记机制而非缺失设计。

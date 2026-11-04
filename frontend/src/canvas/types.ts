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

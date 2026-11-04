// M3-T04：直译自参考仓 `components/canvas-node-generation.ts` 的 buildNodeGenerationInputs，
// 云端素材分支（ark asset:// / 公网链 / Seedance 规格 / Kling 高级参考 / 多模态 composer）全部裁掉。
//
// 口径（与参考仓一致）：
//   - 「连线即参考」：只认同方向（resource → target）的入线，经 Config 中转时以 Config 侧入线为准；
//   - 每个上游节点只出一种入参，优先级 image > video > audio > text（与参考仓 flatMap 同序）；
//   - 读不到内容的节点直接跳过，绝不产出空引用（避免上游收到空 dataUrl / 空 prompt）。
import { CanvasNodeType, type CanvasConnection, type CanvasNodeData } from '../types';
import { isCanvasImageNodeType } from './canvas-panorama';
import { getGenerationResourceNodes } from './canvas-resource-references';

/** 参考图（对齐参考仓 ReferenceImage 的字段面，去掉 ark/云端字段） */
export type CanvasReferenceImage = {
  id: string;
  name: string;
  /** MIME，缺省 image/png */
  type: string;
  /** 可直接预览/上传的地址（media_server URL 或 dataURL） */
  dataUrl: string;
  storageKey?: string;
  /** 给上游的可用 URL（http(s) 优先，其次 dataURL） */
  url?: string;
  width?: number;
  height?: number;
  bytes?: number;
};

/** 参考视频 */
export type CanvasReferenceVideo = {
  id: string;
  name: string;
  type: string;
  url: string;
  storageKey?: string;
  bytes?: number;
  width?: number;
  height?: number;
  durationMs?: number;
};

/** 参考音频 */
export type CanvasReferenceAudio = {
  id: string;
  name: string;
  type: string;
  url: string;
  storageKey?: string;
  durationMs?: number;
};

/** 一条上游入参：type 决定读哪个字段（其余为空） */
export type NodeGenerationInput = {
  nodeId: string;
  type: 'text' | 'image' | 'video' | 'audio';
  title: string;
  text?: string;
  image?: CanvasReferenceImage;
  video?: CanvasReferenceVideo;
  audio?: CanvasReferenceAudio;
};

/**
 * 生成入参（参考仓同名函数直译）：
 * getGenerationResourceNodes → 逐节点读一种载荷 → NodeGenerationInput[]。
 * 顺序即 @ 编号顺序（图片1、视频1…），调用方不得再排序。
 */
export function buildNodeGenerationInputs(
  nodeId: string,
  nodes: CanvasNodeData[],
  connections: CanvasConnection[],
): NodeGenerationInput[] {
  const nodeList = Array.isArray(nodes) ? nodes : [];
  const connectionList = Array.isArray(connections) ? connections : [];
  if (!nodeId) return [];
  return getGenerationResourceNodes(nodeId, nodeList, connectionList).flatMap(
    (node): NodeGenerationInput[] => {
      const image = readReferenceImage(node);
      if (image) return [{ nodeId: node.id, type: 'image' as const, title: node.title, image }];
      const video = readReferenceVideo(node);
      if (video) return [{ nodeId: node.id, type: 'video' as const, title: node.title, video }];
      const audio = readReferenceAudio(node);
      if (audio) return [{ nodeId: node.id, type: 'audio' as const, title: node.title, audio }];
      const text = readNodeTextInput(node);
      if (text) return [{ nodeId: node.id, type: 'text' as const, title: node.title, text }];
      return [];
    },
  );
}

function nodeHasMediaPayload(node: CanvasNodeData): boolean {
  return Boolean(
    node.metadata?.content ||
      node.metadata?.storageKey ||
      node.metadata?.thumbnailStorageKey ||
      node.metadata?.thumbnailContent,
  );
}

function readNodeTextInput(node: CanvasNodeData): string {
  if (node.type === CanvasNodeType.Text) return node.metadata?.content || node.metadata?.prompt || '';
  // 非文本节点也能把 prompt 当上游提示词（Config / 图片节点常这样串）
  return node.metadata?.prompt || '';
}

/** 图片节点（含全景）：有载荷才算参考，避免把空占位节点当参考图提交 */
function readReferenceImage(node: CanvasNodeData): CanvasReferenceImage | null {
  if (!isCanvasImageNodeType(node.type) || !nodeHasMediaPayload(node)) return null;
  const preview = String(node.metadata?.content || '').trim();
  const thumb = String(node.metadata?.thumbnailContent || '').trim();
  const dataUrl = preview || thumb;
  const storageKey = node.metadata?.storageKey || node.metadata?.thumbnailStorageKey;
  return {
    id: node.id,
    // 素材名带标题：用户在上传列表里认得出「素材1-abc123.png」，而不是节点 id
    name: `${node.title || '图片'}-${String(node.id).slice(-6)}.png`,
    type: node.metadata?.mimeType || 'image/png',
    dataUrl,
    storageKey,
    url: /^https?:\/\//i.test(dataUrl) ? dataUrl : dataUrl || undefined,
    width: Number(node.metadata?.naturalWidth) || undefined,
    height: Number(node.metadata?.naturalHeight) || undefined,
    bytes: Number(node.metadata?.bytes) || undefined,
  };
}

/** 参考视频时长要用「成片时长」：metadata.durationMs 在生成节点上是排队+渲染耗时，不能当素材时长 */
export function resolveReferenceVideoDurationMs(node: CanvasNodeData): number | undefined {
  const media = node.metadata?.mediaDurationMs;
  if (typeof media === 'number' && Number.isFinite(media) && media > 0) return Math.round(media);
  const seconds = Number(node.metadata?.seconds);
  if (Number.isFinite(seconds) && seconds > 0 && seconds <= 120) return Math.round(seconds * 1000);
  return undefined;
}

/** 节点 naturalWidth/Height 常被写成画布展示尺寸（如 340px），不能当媒体分辨率 */
function resolveReferenceVideoDimensions(node: CanvasNodeData): { width?: number; height?: number } {
  const width = Number(node.metadata?.naturalWidth || 0);
  const height = Number(node.metadata?.naturalHeight || 0);
  if (!Number.isFinite(width) || !Number.isFinite(height)) return {};
  if (width < 300 || height < 300 || width > 6000 || height > 6000) return {};
  return { width: Math.round(width), height: Math.round(height) };
}

function readReferenceVideo(node: CanvasNodeData): CanvasReferenceVideo | null {
  if (node.type !== CanvasNodeType.Video || !nodeHasMediaPayload(node)) return null;
  const dims = resolveReferenceVideoDimensions(node);
  return {
    id: node.id,
    name: `${node.title || '视频'}-${String(node.id).slice(-6)}.mp4`,
    type: node.metadata?.mimeType || 'video/mp4',
    url: node.metadata?.content || '',
    storageKey: node.metadata?.storageKey,
    bytes: node.metadata?.bytes,
    width: dims.width,
    height: dims.height,
    durationMs: resolveReferenceVideoDurationMs(node),
  };
}

/** 只采纳像真实音轨的时长；过大值多半是生成耗时误写入 */
export function resolveReferenceAudioDurationMs(node: CanvasNodeData): number | undefined {
  const media = node.metadata?.mediaDurationMs;
  if (typeof media === 'number' && Number.isFinite(media) && media >= 500 && media <= 60_000) {
    return Math.round(media);
  }
  return undefined;
}

function readReferenceAudio(node: CanvasNodeData): CanvasReferenceAudio | null {
  if (node.type !== CanvasNodeType.Audio || !nodeHasMediaPayload(node)) return null;
  return {
    id: node.id,
    name: `${node.title || '音频'}-${String(node.id).slice(-6)}.mp3`,
    type: node.metadata?.mimeType || 'audio/mpeg',
    url: node.metadata?.content || '',
    storageKey: node.metadata?.storageKey,
    durationMs: resolveReferenceAudioDurationMs(node),
  };
}

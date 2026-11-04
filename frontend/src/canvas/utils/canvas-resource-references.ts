// M3-T04：直译自参考仓 `utils/canvas-resource-references.ts`，
// 把 `@/lib/image-reference-prompt` 的 imageReferenceLabel 与 `@/lib/seedance-video` 的
// seedanceReferenceLabel 就地实现（「图片N / 视频N / 文本N / 音频N」口径），
// Seedance 专属分支（prompt 拼装、帧编辑、官方规格校验）全部裁掉。
//
// 作用：画布上「哪些节点算资源、编号是几」的唯一来源。编号顺序由连入该节点的连线顺序决定，
// 生成侧（M4）按同一份编号把「图片1」写进提示词，编号稳定是硬要求。
import { CanvasNodeType, type CanvasConnection, type CanvasNodeData } from '../types';
import { isCanvasImageNodeType } from './canvas-panorama';

export type CanvasResourceKind = 'image' | 'video' | 'audio' | 'text';

export type CanvasResourceReference = {
  id: string;
  nodeId: string;
  kind: CanvasResourceKind;
  label: string;
  title: string;
  previewUrl?: string;
  storageKey?: string;
  thumbnailStorageKey?: string;
  text?: string;
  active: boolean;
};

/** 图片编号（参考仓 lib/image-reference-prompt.ts 同形，1 基） */
export function imageReferenceLabel(index: number): string {
  return `图片${index + 1}`;
}

/** 视频/音频编号（参考仓 lib/seedance-video.ts 同形；只保留编号函数，剪掉 Seedance 专属逻辑） */
export function seedanceReferenceLabel(kind: 'image' | 'video' | 'audio', index: number): string {
  if (kind === 'image') return `图片${index + 1}`;
  if (kind === 'video') return `视频${index + 1}`;
  return `音频${index + 1}`;
}

function labelForKind(kind: CanvasResourceKind, index: number): string {
  if (kind === 'image') return imageReferenceLabel(index);
  if (kind === 'video') return seedanceReferenceLabel('video', index);
  if (kind === 'audio') return seedanceReferenceLabel('audio', index);
  return `文本${index + 1}`;
}

/**
 * 画布资源清单：全局按「图片1、图片2、视频1、文本1…」编号；
 * 传 contextNodeId 时，该节点上下文里的资源标 active=true（画布高亮/可 @）。
 */
export function buildCanvasResourceReferences(
  nodes: CanvasNodeData[],
  connections: CanvasConnection[],
  contextNodeId?: string | null,
): CanvasResourceReference[] {
  const nodeList = Array.isArray(nodes) ? nodes : [];
  const contextNodes = contextNodeId ? getMentionResourceNodes(contextNodeId, nodeList, connections) : [];
  const globalReferences = labelResourceNodes(nodeList.filter(isResourceNode), false);
  const activeByNodeId = new Map(
    labelResourceNodes(contextNodes, true).map((reference) => [reference.nodeId, reference]),
  );
  return globalReferences.map((reference) => activeByNodeId.get(reference.nodeId) || reference);
}

/** Agent 输入框 @ 引用：画布上全部资源节点均可提及 */
export function buildAssistantMentionReferences(nodes: CanvasNodeData[]): CanvasResourceReference[] {
  return labelResourceNodes((Array.isArray(nodes) ? nodes : []).filter(isResourceNode), true);
}

/**
 * 从文案里解析已写入的「图片1 / 视频1」标签 → 节点 ID。
 * 标签从长到短排序匹配，且用「后面不能紧跟数字」的边界，避免「图片1」误匹配「图片10」。
 */
export function collectMentionedNodeIds(
  text: string,
  references: CanvasResourceReference[],
): Set<string> {
  const ids = new Set<string>();
  if (!text) return ids;
  const sorted = [...references].sort((a, b) => b.label.length - a.label.length);
  for (const reference of sorted) {
    if (
      textHasResourceLabel(text, reference.label) ||
      textHasResourceLabel(text, reference.title) ||
      text.includes(`@${reference.title}`)
    ) {
      ids.add(reference.nodeId);
    }
  }
  return ids;
}

/** 解析提示词中的 图片N/视频N/…，按当前可用资源标签解析到 nodeId（不用裸标题，避免误连） */
export function resolvePromptMentionedNodeIds(
  text: string,
  nodes: CanvasNodeData[],
  connections: CanvasConnection[],
  contextNodeId?: string | null,
): Set<string> {
  const references = buildCanvasResourceReferences(nodes, connections, contextNodeId);
  const ids = collectMentionedNodeIds(text, references);
  if (contextNodeId) ids.delete(contextNodeId);
  return ids;
}

function textHasResourceLabel(text: string, label: string): boolean {
  if (!label) return false;
  const escaped = label.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  // 允许「构图参考图片2」这类中文紧邻；禁止「图片10」误匹配「图片1」
  return new RegExp(`(?<![0-9A-Za-z_])${escaped}(?!\\d)`, 'i').test(text);
}

/** 某个节点上下文里的资源（@ 编号顺序由连线顺序决定；经 Config 中转时以 Config 侧入线为准） */
export function buildNodeMentionReferences(
  node: CanvasNodeData,
  nodes: CanvasNodeData[],
  connections: CanvasConnection[],
): CanvasResourceReference[] {
  return labelResourceNodes(getMentionResourceNodes(node.id, nodes, connections), true);
}

/** 素材 @ 编号顺序由连入该节点的连线顺序决定；经 Config 中转时以 Config 侧入线为准 */
export function getMentionResourceConnectionTarget(
  nodeId: string,
  nodes: CanvasNodeData[],
  connections: CanvasConnection[],
): string {
  const configConnection = connections.find(
    (connection) =>
      connection.fromNodeId === nodeId &&
      nodes.find((node) => node.id === connection.toNodeId)?.type === CanvasNodeType.Config,
  );
  if (configConnection) return configConnection.toNodeId;
  return nodeId;
}

/** 按给定源节点顺序重排进入 targetNodeId 的连线（@ 编号稳定性靠它） */
export function reorderConnectionsForMentionResources(
  connections: CanvasConnection[],
  targetNodeId: string,
  orderedFromNodeIds: string[],
): CanvasConnection[] {
  const incoming = connections.filter((connection) => connection.toNodeId === targetNodeId);
  if (!incoming.length || !orderedFromNodeIds.length) return connections;
  const byFrom = new Map(incoming.map((connection) => [connection.fromNodeId, connection]));
  const reordered = orderedFromNodeIds
    .map((fromNodeId) => byFrom.get(fromNodeId))
    .filter((connection): connection is CanvasConnection => Boolean(connection));
  for (const connection of incoming) {
    if (!reordered.includes(connection)) reordered.push(connection);
  }
  const firstIndex = connections.findIndex((connection) => connection.toNodeId === targetNodeId);
  if (firstIndex < 0) return connections;
  const head = connections.slice(0, firstIndex);
  const tail = connections.slice(firstIndex).filter((connection) => connection.toNodeId !== targetNodeId);
  return [...head, ...reordered, ...tail];
}

export function getMentionResourceNodes(
  nodeId: string,
  nodes: CanvasNodeData[],
  connections: CanvasConnection[],
): CanvasNodeData[] {
  const configInputs = getConnectedConfigResourceNodes(nodeId, nodes, connections);
  if (configInputs.length) return configInputs;
  return getContextResourceNodes(nodeId, nodes, connections);
}

/** 生成侧入参用的资源节点（连线即参考）：Config 中转优先，其次自身入线 */
export function getGenerationResourceNodes(
  nodeId: string,
  nodes: CanvasNodeData[],
  connections: CanvasConnection[],
): CanvasNodeData[] {
  const configInputs = getConnectedConfigResourceNodes(nodeId, nodes, connections);
  if (configInputs.length) return configInputs;
  return getContextResourceNodes(nodeId, nodes, connections);
}

function getContextResourceNodes(
  nodeId: string,
  nodes: CanvasNodeData[],
  connections: CanvasConnection[],
): CanvasNodeData[] {
  return connections
    .filter((connection) => connection.toNodeId === nodeId)
    .map((connection) => nodes.find((node) => node.id === connection.fromNodeId))
    .filter((node): node is CanvasNodeData => Boolean(node && isResourceNode(node)));
}

function getConnectedConfigResourceNodes(
  nodeId: string,
  nodes: CanvasNodeData[],
  connections: CanvasConnection[],
): CanvasNodeData[] {
  const configConnection = connections.find(
    (connection) =>
      connection.fromNodeId === nodeId &&
      nodes.find((node) => node.id === connection.toNodeId)?.type === CanvasNodeType.Config,
  );
  if (!configConnection) return [];
  return getContextResourceNodes(configConnection.toNodeId, nodes, connections).filter(
    (node) => node.id !== nodeId,
  );
}

/** 统一编号：按传入顺序分别给 image/video/audio/text 递增（与参考仓 labelResourceNodes 同口径） */
function labelResourceNodes(nodes: CanvasNodeData[], active: boolean): CanvasResourceReference[] {
  const counts: Record<CanvasResourceKind, number> = { image: 0, video: 0, audio: 0, text: 0 };
  return nodes.flatMap((node): CanvasResourceReference[] => {
    const kind = resourceKind(node);
    if (!kind) return [];
    const index = counts[kind]++;
    const label = labelForKind(kind, index);
    return [
      {
        id: node.id,
        nodeId: node.id,
        kind,
        label,
        title: node.title || label,
        previewUrl: resourcePreviewUrl(node),
        storageKey: node.metadata?.storageKey || undefined,
        thumbnailStorageKey: node.metadata?.thumbnailStorageKey || undefined,
        text:
          node.type === CanvasNodeType.Text
            ? node.metadata?.content || node.metadata?.prompt
            : undefined,
        active,
      },
    ];
  });
}

/** 与画布节点预览一致：优先缩略图，跳过失效 blob */
function resourcePreviewUrl(node: CanvasNodeData): string | undefined {
  for (const value of [node.metadata?.thumbnailContent, node.metadata?.content]) {
    if (!value || value.startsWith('blob:')) continue;
    return value;
  }
  return undefined;
}

function isResourceNode(node: CanvasNodeData): boolean {
  return Boolean(resourceKind(node));
}

/** 资源判定：有实际内容才算（空占位节点不进编号，避免「图片1」指向一张空气） */
function resourceKind(node: CanvasNodeData): CanvasResourceKind | null {
  if (
    isCanvasImageNodeType(node.type) &&
    (node.metadata?.content ||
      node.metadata?.storageKey ||
      node.metadata?.thumbnailStorageKey ||
      node.metadata?.thumbnailContent)
  ) {
    return 'image';
  }
  if (node.type === CanvasNodeType.Video && (node.metadata?.content || node.metadata?.storageKey)) {
    return 'video';
  }
  if (node.type === CanvasNodeType.Audio && (node.metadata?.content || node.metadata?.storageKey)) {
    return 'audio';
  }
  if (node.type === CanvasNodeType.Text && (node.metadata?.content || node.metadata?.prompt)) {
    return 'text';
  }
  return null;
}

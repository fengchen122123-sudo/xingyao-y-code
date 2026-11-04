// M3-T05：画布节点剪贴板（纯函数层）。
//
// 口径：序列化成带标记前缀的字符串（`xingyao-canvas:nodes:` + JSON），
// 这样粘贴时能一眼认出「是不是本画布的节点数据」，别的程序塞进来的文本一律当无效。
// 内容 = 选中节点 + 它们之间的内部连线；指向选区外的连线属于「半截引用」，序列化时即丢弃。
//
// 反序列化做形状校验（缺字段/类型不对 → null），绝不把脏数据灌进 project：
// 画布是落盘的，脏节点会被 sanitize 之外的路径带进存档。
import type { CanvasConnection, CanvasNodeData, CanvasNodeMetadata } from '../types';

/** 剪贴板标记前缀：粘贴时据此判断内容来源 */
export const CANVAS_CLIPBOARD_PREFIX = 'xingyao-canvas:nodes:';
/** 序列化版本（后续字段变更时便于兼容分支） */
export const CANVAS_CLIPBOARD_VERSION = 1;
/** 粘贴偏移：每粘贴一次向右下挪一点，避免和原件完全重叠 */
export const PASTE_OFFSET = 24;

export type CanvasClipboardPayload = { nodes: CanvasNodeData[]; connections: CanvasConnection[] };

type SerializedClipboard = {
  version: number;
  nodes: CanvasNodeData[];
  connections: CanvasConnection[];
};

/** 深拷贝（structuredClone 不可用/不可克隆时退回 JSON 往返） */
function deepClone<T>(value: T): T {
  if (typeof structuredClone === 'function') {
    try {
      return structuredClone(value);
    } catch {
      /* 落到 JSON 往返 */
    }
  }
  return JSON.parse(JSON.stringify(value)) as T;
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function isFiniteNumber(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value);
}

/** 节点形状校验：id/type/title 必填，position 与宽高必须有限数字（宽高 >0） */
function isValidNode(value: unknown): value is CanvasNodeData {
  if (!isPlainObject(value)) return false;
  if (typeof value.id !== 'string' || !value.id) return false;
  if (typeof value.type !== 'string' || !value.type) return false;
  const position = value.position;
  if (!isPlainObject(position) || !isFiniteNumber(position.x) || !isFiniteNumber(position.y)) return false;
  if (!isFiniteNumber(value.width) || !isFiniteNumber(value.height)) return false;
  if (value.width <= 0 || value.height <= 0) return false;
  if (value.title !== undefined && typeof value.title !== 'string') return false;
  if (value.metadata !== undefined && !isPlainObject(value.metadata)) return false;
  return true;
}

function isValidConnection(value: unknown): value is CanvasConnection {
  if (!isPlainObject(value)) return false;
  return (
    typeof value.id === 'string' &&
    !!value.id &&
    typeof value.fromNodeId === 'string' &&
    !!value.fromNodeId &&
    typeof value.toNodeId === 'string' &&
    !!value.toNodeId
  );
}

/**
 * 序列化选中节点 + 它们之间的内部连线。
 * 节点为空返回空串（空内容写剪贴板反而会覆盖掉之前复制的东西，这里干脆不产）。
 */
export function serializeNodes(nodes: CanvasNodeData[], connections: CanvasConnection[]): string {
  const list = (Array.isArray(nodes) ? nodes : []).filter(isValidNode);
  if (!list.length) return '';
  const ids = new Set(list.map((node) => node.id));
  const internal = (Array.isArray(connections) ? connections : []).filter(
    (connection) =>
      isValidConnection(connection) && ids.has(connection.fromNodeId) && ids.has(connection.toNodeId),
  );
  const payload: SerializedClipboard = {
    version: CANVAS_CLIPBOARD_VERSION,
    nodes: deepClone(list),
    connections: deepClone(internal),
  };
  try {
    return `${CANVAS_CLIPBOARD_PREFIX}${JSON.stringify(payload)}`;
  } catch {
    // 节点里混进了不可序列化值（函数/symbol）：当作没复制，不给上层留半截数据
    return '';
  }
}

/**
 * 反序列化：校验标记 + JSON 形状。
 * 任何一处不合格都返回 null（上层据此提示「剪贴板里没有画布节点」）。
 * 连线做容错：端点不在节点集合里的直接剔除，不因为一根坏线否掉整份内容。
 */
export function deserializeNodes(raw: string): CanvasClipboardPayload | null {
  if (typeof raw !== 'string' || !raw.startsWith(CANVAS_CLIPBOARD_PREFIX)) return null;
  const body = raw.slice(CANVAS_CLIPBOARD_PREFIX.length);
  let parsed: unknown;
  try {
    parsed = JSON.parse(body);
  } catch {
    return null;
  }
  // 兼容「直接存数组」的老写法：没有 version 包裹时也认
  const rawNodes = Array.isArray(parsed) ? parsed : isPlainObject(parsed) ? parsed.nodes : null;
  if (!Array.isArray(rawNodes) || !rawNodes.length) return null;

  const nodes: CanvasNodeData[] = [];
  const ids = new Set<string>();
  for (const item of rawNodes) {
    if (!isValidNode(item) || ids.has(item.id)) continue;
    ids.add(item.id);
    nodes.push(deepClone(item));
  }
  if (!nodes.length) return null;

  const rawConnections = Array.isArray(parsed)
    ? []
    : isPlainObject(parsed) && Array.isArray(parsed.connections)
      ? parsed.connections
      : [];
  const connections: CanvasConnection[] = [];
  const connectionIds = new Set<string>();
  for (const item of rawConnections) {
    if (!isValidConnection(item) || connectionIds.has(item.id)) continue;
    if (!ids.has(item.fromNodeId) || !ids.has(item.toNodeId)) continue;
    connectionIds.add(item.id);
    connections.push({ id: item.id, fromNodeId: item.fromNodeId, toNodeId: item.toNodeId });
  }
  return { nodes, connections };
}

/** id 生成器：`n_`/`c_` + 时间戳 36 进制 + 序号 + 随机后缀，同批次内保证不重复 */
function nextId(prefix: 'n_' | 'c_', taken: Set<string>, counter: { value: number }): string {
  let id = '';
  do {
    counter.value += 1;
    const stamp = Date.now().toString(36);
    const seq = counter.value.toString(36);
    const rand = Math.random().toString(36).slice(2, 6).padEnd(4, '0');
    id = `${prefix}${stamp}${seq}${rand}`;
  } while (taken.has(id));
  taken.add(id);
  return id;
}

/**
 * metadata 重映射：
 * - 指向选区内节点的引用（groupId/batchRootId/primaryImageId/首尾帧/批次子节点）换成本次生成的新 id；
 * - 指向选区外的引用直接删（复制出来的节点不该再牵着原件或原分组）；
 * - job 相关字段抹掉（复制品不是在跑的 job，留着会挂在 loading 上等一个永远不来的回填）。
 */
function cloneMetadata(
  metadata: CanvasNodeMetadata | undefined,
  idMap: Map<string, string>,
): CanvasNodeMetadata | undefined {
  if (!metadata) return undefined;
  const next: CanvasNodeMetadata = deepClone(metadata);

  const remap = (value: string | undefined): string | undefined => {
    if (typeof value !== 'string' || !value) return undefined;
    return idMap.get(value);
  };

  const groupId = remap(next.groupId);
  if (groupId) next.groupId = groupId;
  else delete next.groupId;

  const batchRootId = remap(next.batchRootId);
  if (batchRootId) next.batchRootId = batchRootId;
  else delete next.batchRootId;

  const primaryImageId = remap(next.primaryImageId);
  if (primaryImageId) next.primaryImageId = primaryImageId;
  else delete next.primaryImageId;

  const firstFrame = remap(next.firstFrameNodeId);
  if (firstFrame) next.firstFrameNodeId = firstFrame;
  else delete next.firstFrameNodeId;

  const lastFrame = remap(next.lastFrameNodeId);
  if (lastFrame) next.lastFrameNodeId = lastFrame;
  else delete next.lastFrameNodeId;

  if (Array.isArray(next.batchChildIds)) {
    const children = next.batchChildIds.map((id) => idMap.get(id)).filter((id): id is string => !!id);
    if (children.length) next.batchChildIds = children;
    else delete next.batchChildIds;
  }

  // 复制品从 idle 开始：没有在跑的 job，也不该显示上一次的进度/错误
  delete next.jobId;
  delete next.progress;
  delete next.startedAt;
  delete next.durationMs;
  delete next.errorDetails;
  if (next.status === 'loading') next.status = 'idle';

  return next;
}

/**
 * 复制一份可粘贴的节点：id 全量重生成、位置右下偏移、组关系在内聚时保留。
 * 连线按 idMap 重映射；任一端点不在选区内的连线丢弃（「指向选区外的引用」不复制）。
 */
export function cloneNodes(payload: CanvasClipboardPayload, offset = PASTE_OFFSET): CanvasClipboardPayload {
  const source = (Array.isArray(payload?.nodes) ? payload.nodes : []).filter(isValidNode);
  if (!source.length) return { nodes: [], connections: [] };

  const dx = Number.isFinite(offset) ? offset : PASTE_OFFSET;
  const taken = new Set<string>();
  const nodeCounter = { value: 0 };
  const idMap = new Map<string, string>();
  for (const node of source) {
    if (idMap.has(node.id)) continue;
    idMap.set(node.id, nextId('n_', taken, nodeCounter));
  }

  const nodes: CanvasNodeData[] = source
    .filter((node) => idMap.has(node.id))
    .map((node) => ({
      ...deepClone(node),
      id: idMap.get(node.id) as string,
      position: { x: node.position.x + dx, y: node.position.y + dx },
      metadata: cloneMetadata(node.metadata, idMap),
    }));

  const connectionCounter = { value: 0 };
  const connections: CanvasConnection[] = [];
  for (const connection of Array.isArray(payload?.connections) ? payload.connections : []) {
    if (!isValidConnection(connection)) continue;
    const fromNodeId = idMap.get(connection.fromNodeId);
    const toNodeId = idMap.get(connection.toNodeId);
    if (!fromNodeId || !toNodeId) continue; // 端点落在选区外 → 丢弃
    connections.push({
      id: nextId('c_', taken, connectionCounter),
      fromNodeId,
      toNodeId,
    });
  }

  return { nodes, connections };
}

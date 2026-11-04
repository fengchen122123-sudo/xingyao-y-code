// M3-T04：直译自参考仓 `utils/canvas-reference-guard.ts` 的「参考素材不合规即时拦截」口径，
// 但把 `@/lib/video-reference-rules` 的整张模型表裁掉，按 tokenrhythm 现有能力重写成
// 「源节点类型 → 目标节点类型」的静态兼容表（计划稿 §M3-T04）。
//
// 与参考仓的差异（均为缩表，不是另起一套交互）：
//   1) 参考仓按「模型 + 素材数量/时长/规格」细粒度校验（Kling/Seedance 各一套规则）；
//      tokenrhythm 一期只有生图（qwen-image/wan2.7-image）与生视频（wan3.0-video）两条链，
//      没有参考视频/参考音频/尾帧能力，故退化为静态类型表 + 单条 reason；
//   2) 参考仓的 validateCanvasVideoReferences 会在「切模型」时裁掉超额连线（disconnectConnectionIds）；
//      tokenrhythm 在 connect 时就直接拒绝（validateConnection 返回 {ok, reason}），
//      切模型裁剪留给 M4 生成侧；validateCanvasConnection 只保留参考仓「红框 + blameNotes」返回形状。
import { CanvasNodeType, type CanvasConnection, type CanvasNodeData } from '../types';

/** connect 的判定结果：ok=false 时 reason 直接进右上角 toast */
export type CanvasConnectionCheck = { ok: boolean; reason?: string };

/** 与参考仓一致的违规返回形状：红框节点 + 角标短句 + 待断开连线 + 大白话文案 */
export type CanvasReferenceViolation = {
  message: string;
  /** 需要红框高亮的节点 */
  blameNodeIds: string[];
  /** 红框角标短句：nodeId → 一句话（不超过一行） */
  blameNotes: Record<string, string>;
  /** 需要自动断开的连线（tokenrhythm 在 connect 阶段就拒绝，故恒为空，保留字段对齐参考仓） */
  disconnectConnectionIds: string[];
};

/** 连线的引用语义：决定 M4 提交时把它当参考图 / 首帧 / 提示词 / 环境球 */
export type CanvasReferenceRole = 'edit-reference' | 'first-frame' | 'prompt' | 'panorama-environment';

export type CanvasReferenceRule =
  | { allowed: true; role: CanvasReferenceRole; note: string }
  | { allowed: false; reason: string };

/** 节点类型中文名：reason 里直接说人话，调用方不用再拼字符串 */
export const CANVAS_NODE_TYPE_LABELS: Record<string, string> = {
  [CanvasNodeType.Image]: '图片',
  [CanvasNodeType.Panorama]: '全景图',
  [CanvasNodeType.Text]: '文本',
  [CanvasNodeType.Config]: '生成配置',
  [CanvasNodeType.Video]: '视频',
  [CanvasNodeType.Audio]: '音频',
  [CanvasNodeType.Director]: '导演台',
  [CanvasNodeType.Group]: '分组',
};

function allow(role: CanvasReferenceRole, note: string): CanvasReferenceRule {
  return { allowed: true, role, note };
}

function deny(reason: string): CanvasReferenceRule {
  return { allowed: false, reason };
}

/**
 * tokenrhythm 类型兼容表（本任务的核心交付物，逐格可测）：
 *
 * | 源 \ 目标 | image | panorama | config | video | audio | director | group |
 * |-----------|-------|----------|--------|-------|-------|----------|-------|
 * | image     | 改图参考 | ✗ | ✗ | 首帧 | ✗ | ✗ | ✗ |
 * | panorama  | ✗ | ✗ | ✗ | ✗ | ✗ | 环境球 | ✗ |
 * | text      | 提示词 | 提示词 | 提示词 | 提示词 | 提示词 | 提示词 | ✗ |
 * | audio     | ✗ | ✗ | ✗ | ✗（音频参考暂未接入） | ✗ | ✗ | ✗ |
 * | video     | ✗ | ✗ | ✗ | ✗（视频参考暂未接入） | ✗ | ✗ | ✗ |
 * | config    | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ |
 * | director  | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ |
 * | group     | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ |
 *
 * 说明：
 * - image → image 是改图参考（生成类型 generationType=edit）；
 * - image → video 是首帧（wan3.0-video 的 image2video 入参）；
 * - text → config/生成节点是 prompt 汇入；
 * - panorama → director 是导演台环境球（M7 消费）；
 * - audio → video 预留了音频参考的语义位，但 tokenrhythm 一期没有入参，明确拒绝并给提示。
 * - config 作为「素材汇集入口」（image/video/audio → config）依赖 M4 的批量生成侧定义，
 *   本表暂按计划稿从简：其余组合一律拒绝，届时只改这一张表即可。
 */
export const CANVAS_REFERENCE_RULES: Record<string, Record<string, CanvasReferenceRule>> = {
  [CanvasNodeType.Image]: {
    [CanvasNodeType.Image]: allow('edit-reference', '改图参考'),
    [CanvasNodeType.Video]: allow('first-frame', '首帧'),
  },
  [CanvasNodeType.Panorama]: {
    [CanvasNodeType.Director]: allow('panorama-environment', '环境球'),
  },
  [CanvasNodeType.Text]: {
    [CanvasNodeType.Config]: allow('prompt', '提示词'),
    [CanvasNodeType.Image]: allow('prompt', '提示词'),
    [CanvasNodeType.Panorama]: allow('prompt', '提示词'),
    [CanvasNodeType.Video]: allow('prompt', '提示词'),
    [CanvasNodeType.Audio]: allow('prompt', '提示词'),
    [CanvasNodeType.Director]: allow('prompt', '导演台提示词'),
  },
  [CanvasNodeType.Audio]: {
    [CanvasNodeType.Video]: deny('音频参考暂未接入'),
  },
  [CanvasNodeType.Video]: {
    [CanvasNodeType.Video]: deny('视频参考暂未接入'),
  },
  [CanvasNodeType.Config]: {},
  [CanvasNodeType.Director]: {},
  [CanvasNodeType.Group]: {},
};

function isKnownType(type: string | null | undefined): boolean {
  return typeof type === 'string' && Object.prototype.hasOwnProperty.call(CANVAS_NODE_TYPE_LABELS, type);
}

function typeLabel(type: string | null | undefined): string {
  if (!type) return '空类型';
  return CANVAS_NODE_TYPE_LABELS[type] ?? type;
}

/** 取某条类型组合的规则；不在表内返回 undefined（= 需要走 defaultDenyReason） */
export function referenceRuleFor(
  fromType: string | null | undefined,
  toType: string | null | undefined,
): CanvasReferenceRule | undefined {
  if (!fromType || !toType) return undefined;
  const row = CANVAS_REFERENCE_RULES[fromType];
  if (!row) return undefined;
  return row[toType];
}

/** 表外组合的拒绝文案：先点明脏数据（未知类型/分组），再说「暂不支持」 */
export function defaultDenyReason(
  fromType: string | null | undefined,
  toType: string | null | undefined,
): string {
  if (!isKnownType(fromType)) return `不支持的源节点类型：${fromType || '空'}`;
  if (!isKnownType(toType)) return `不支持的目标节点类型：${toType || '空'}`;
  if (fromType === CanvasNodeType.Group) return '分组节点不能作为连线起点';
  if (toType === CanvasNodeType.Group) return '分组节点不接收连线';
  return `${typeLabel(fromType)}暂不支持连到${typeLabel(toType)}`;
}

/**
 * 新增一条 from → to 的连线是否会成环。
 * 判据：从 to 顺着已有出边往下游走，若能回到 from，则新边闭合成环（画布是有向无环图）。
 */
export function willCreateCycle(
  fromNodeId: string,
  toNodeId: string,
  connections: CanvasConnection[],
): boolean {
  if (!fromNodeId || !toNodeId) return false;
  if (fromNodeId === toNodeId) return true;
  const outgoing = new Map<string, string[]>();
  for (const connection of connections) {
    const list = outgoing.get(connection.fromNodeId);
    if (list) list.push(connection.toNodeId);
    else outgoing.set(connection.fromNodeId, [connection.toNodeId]);
  }
  const stack: string[] = [toNodeId];
  const seen = new Set<string>([toNodeId]);
  while (stack.length) {
    const id = stack.pop() as string;
    if (id === fromNodeId) return true;
    for (const next of outgoing.get(id) ?? []) {
      if (seen.has(next)) continue;
      seen.add(next);
      stack.push(next);
    }
  }
  return false;
}

export type ValidateConnectionParams = {
  fromNodeId: string;
  toNodeId: string;
  nodes: CanvasNodeData[];
  connections: CanvasConnection[];
};

/**
 * 连线合法性主入口（store.connect 先过这里）：
 * 空 id → 自连 → 节点存在性 → 重复 → 类型表 → 环检测。
 * 顺序刻意把「用户最容易犯的错」放前面：自连/重复的文案比类型表更直白。
 */
export function validateConnection(params: ValidateConnectionParams): CanvasConnectionCheck {
  const { fromNodeId, toNodeId, nodes, connections } = params;
  if (!fromNodeId || !toNodeId) return { ok: false, reason: '连线缺少起点或终点' };
  if (fromNodeId === toNodeId) return { ok: false, reason: '不能把节点连到自己' };

  const nodeList = Array.isArray(nodes) ? nodes : [];
  const connectionList = Array.isArray(connections) ? connections : [];
  const from = nodeList.find((node) => node.id === fromNodeId);
  const to = nodeList.find((node) => node.id === toNodeId);
  if (!from) return { ok: false, reason: '起点节点不存在' };
  if (!to) return { ok: false, reason: '目标节点不存在' };

  if (connectionList.some((c) => c.fromNodeId === fromNodeId && c.toNodeId === toNodeId)) {
    return { ok: false, reason: '这两个节点已经连线了' };
  }

  const rule = referenceRuleFor(from.type, to.type);
  if (!rule) return { ok: false, reason: defaultDenyReason(from.type, to.type) };
  if (!rule.allowed) return { ok: false, reason: rule.reason };

  if (willCreateCycle(fromNodeId, toNodeId, connectionList)) {
    return { ok: false, reason: '不能连成环：目标节点已经在当前节点的上游' };
  }
  return { ok: true };
}

/** 允许的连线 → 引用语义（M4 提交时按 role 分流；不允许/表外返回 null） */
export function referenceRoleFor(
  fromNodeId: string,
  toNodeId: string,
  nodes: CanvasNodeData[],
): CanvasReferenceRole | null {
  const from = nodes.find((node) => node.id === fromNodeId);
  const to = nodes.find((node) => node.id === toNodeId);
  const rule = referenceRuleFor(from?.type, to?.type);
  return rule && rule.allowed ? rule.role : null;
}

/**
 * 参考仓口径的包装：把「连线被拒绝」翻成红框 + blameNotes + 文案。
 * tokenrhythm 在 connect 阶段就拒绝，因此 disconnectConnectionIds 恒为空；
 * M4 切模型时才是它的主战场（那时已有连线，需要点出要断的那几条）。
 */
export function validateCanvasConnection(params: ValidateConnectionParams): CanvasReferenceViolation | null {
  const check = validateConnection(params);
  if (check.ok) return null;
  const reason = check.reason || '连线不可用';
  const blameNodeIds = [...new Set([params.fromNodeId, params.toNodeId].filter(Boolean))];
  const blameNotes: Record<string, string> = {};
  for (const id of blameNodeIds) blameNotes[id] = reason;
  return {
    message: `连线被拒绝\n原因：${reason}\n整改：换一个受支持的节点类型，或先断开冲突的连线后再连`,
    blameNodeIds,
    blameNotes,
    disconnectConnectionIds: [],
  };
}

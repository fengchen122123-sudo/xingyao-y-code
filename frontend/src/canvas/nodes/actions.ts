// 节点 → 画布 的唯一上行通道（M3-T01）。
//
// 约定（沿用星瑶旧画布）：节点组件只 emit action，绝不 import store、不改 project；
// 落库由 CanvasPane（M3-T02 起接 store）统一处理。这样节点就能在单测里独立挂载，
// 也避免了「八个节点各自发一次 save」的写盘风暴。
export type NodeActionPayload = {
  nodeId: string;
  /** 动作名：update-title / update-text / open-director …（M3-T02/M7 逐条接线） */
  action: string;
  args?: Record<string, unknown>;
};

/** defineEmits 的签名片段：统一写法，避免各节点各写一套结构类型 */
export type NodeActionEmits = { action: [payload: NodeActionPayload] };

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

/**
 * 端口拉线的落点（M3-T04）：起点节点已由目标端口的宿主 NodeShell 从
 * connection-drag 单例里读出，这里只带 from/to 两个 id。
 * 合法性不进组件：CanvasPane 收到后调 store.connect（guard 在 store 里过）。
 */
export type CanvasConnectRequest = { fromNodeId: string; toNodeId: string };

/** 只有 NodeShell 需要：目标端口松手时上抛连线请求 */
export type NodeConnectEmits = { 'connect-request': [payload: CanvasConnectRequest] };

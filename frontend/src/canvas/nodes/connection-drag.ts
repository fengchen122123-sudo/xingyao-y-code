// M3-T04：拉线过程中的共享拖拽态（模块级单例）。
//
// 为什么放模块级而不是某组件里：拉线跨两个 NodeShell —— 起点在源节点的右端口（谁按下谁持有
// 手势），终点在目标节点的左端口（另一实例收到 pointerup）。参考仓把这份态放在画布页的
// useState 里，星瑶这里节点组件不 import store（M3-T01 的约定），于是用最小代价的模块单例：
// 只有「谁在拉、拉到屏幕哪儿」三个字段，纯粹是手势帧数据，不进 project、不落盘。
import { reactive } from 'vue';

export type ConnectionDragState = {
  /** 是否正在拉线 */
  active: boolean;
  /** 起点节点 id（源端，节点右侧端口） */
  fromNodeId: string;
  /** 起始指针 id：多指输入时只认这一根手指 */
  pointerId: number;
  /** 指针客户端坐标（clientX/clientY）：世界坐标换算由 CanvasPane 负责 */
  clientX: number;
  clientY: number;
};

export const connectionDrag = reactive<ConnectionDragState>({
  active: false,
  fromNodeId: '',
  pointerId: -1,
  clientX: 0,
  clientY: 0,
});

/** 开始拉线：记下起点节点与按下的屏幕位置 */
export function startConnectionDrag(
  fromNodeId: string,
  pointer: { pointerId?: number; clientX: number; clientY: number },
): void {
  if (!fromNodeId) return;
  connectionDrag.active = true;
  connectionDrag.fromNodeId = fromNodeId;
  connectionDrag.pointerId = typeof pointer.pointerId === 'number' ? pointer.pointerId : -1;
  connectionDrag.clientX = pointer.clientX;
  connectionDrag.clientY = pointer.clientY;
}

/** 跟手：只更新屏幕坐标，CanvasPane 的 computed 会自动重算世界坐标与路径 */
export function moveConnectionDrag(clientX: number, clientY: number): void {
  if (!connectionDrag.active) return;
  if (!Number.isFinite(clientX) || !Number.isFinite(clientY)) return;
  connectionDrag.clientX = clientX;
  connectionDrag.clientY = clientY;
}

/** 结束拉线：返回起点节点 id（供调用方判断是否要补一次「空白处松手」处理），并清空态 */
export function endConnectionDrag(): { fromNodeId: string } | null {
  if (!connectionDrag.active) return null;
  const fromNodeId = connectionDrag.fromNodeId;
  connectionDrag.active = false;
  connectionDrag.fromNodeId = '';
  connectionDrag.pointerId = -1;
  connectionDrag.clientX = 0;
  connectionDrag.clientY = 0;
  return { fromNodeId };
}

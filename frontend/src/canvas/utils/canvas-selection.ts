// M3-T03：框选命中的纯函数层（世界坐标矩形 ↔ 节点包围盒）。
//
// 口径来源：参考仓 `[id]/canvas-client-page.tsx` 的 handleGlobalPointerMove：
//   rectX = min(startWorldX, currentWorldX)、rectW = |Δx|（SelectionBox 同口径），
//   命中判定 `rectX < node.x + node.width && rectX + rectW > node.x`（两条轴都满足才算命中，
//   即「相交」而非「包含」）；阈值 `rectW > 3 || rectH > 3` 才算真正框选。
// 本仓把阈值取整为 4（任务口径 <4px 视为点击清选），其余逐字沿用。
import type { CanvasNodeData, SelectionBoxState } from '../types';

/** 世界坐标矩形（左上角 + 宽高，宽高恒为非负） */
export type SelectionRect = { x: number; y: number; width: number; height: number };

/** InfiniteCanvas → CanvasPane 的框选提交载荷（世界坐标矩形 + Shift 加选） */
export type SelectionCommitPayload = { rect: SelectionRect; additive: boolean };

/** 框选阈值：世界坐标下宽高都小于它＝没真的拖框，按「空白点击」处理 */
export const SELECTION_MIN_DRAG = 4;

/** 选区工具条按钮动作（组件 emit、CanvasPane 落 store） */
export type SelectionToolbarAction = 'delete' | 'copy' | 'group' | 'ungroup';

/** 框选态 → 规范化世界矩形（起点可能在终点右下方，一律折算成左上角 + 非负宽高） */
export function selectionBoxToRect(box: SelectionBoxState): SelectionRect {
  return {
    x: Math.min(box.startWorldX, box.currentWorldX),
    y: Math.min(box.startWorldY, box.currentWorldY),
    width: Math.abs(box.currentWorldX - box.startWorldX),
    height: Math.abs(box.currentWorldY - box.startWorldY),
  };
}

/** 是否已经拖出真正意义上的框（任一维 ≥ 阈值） */
export function isSelectionArmed(rect: SelectionRect, threshold = SELECTION_MIN_DRAG): boolean {
  return rect.width >= threshold || rect.height >= threshold;
}

/**
 * 节点包围盒与矩形是否相交（边界相接不算相交，与参考仓的严格不等号一致）。
 * 纯轴对齐比较：不依赖 DOM，节点尚未渲染也能判定。
 */
export function nodeIntersectsRect(node: CanvasNodeData, rect: SelectionRect): boolean {
  return (
    rect.x < node.position.x + node.width &&
    rect.x + rect.width > node.position.x &&
    rect.y < node.position.y + node.height &&
    rect.y + rect.height > node.position.y
  );
}

/** 命中节点 id 列表（保持 nodes 原顺序，分组垫底不影响 id 集合） */
export function getNodesInSelectionRect(nodes: CanvasNodeData[], rect: SelectionRect): string[] {
  const hit: string[] = [];
  for (const node of nodes) {
    if (nodeIntersectsRect(node, rect)) hit.push(node.id);
  }
  return hit;
}

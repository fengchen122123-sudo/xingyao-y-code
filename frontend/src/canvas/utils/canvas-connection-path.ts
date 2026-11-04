// M3-T04：连线几何（世界坐标）。直译自参考仓 `components/canvas-connections.tsx` 的
// ConnectionPath / connectionIntersectsWorldRect / ActiveConnectionPath 三处公式，抽成纯函数：
//   - 渲染（ConnectionPath.vue）与框选命中（CanvasPane）必须用同一套公式，否则「看到选中了、
//     点下去却没选中」；
//   - 纯函数不依赖 DOM/Vue，可在 jsdom 里逐值断言。
import type { CanvasConnection, CanvasNodeData, Position } from '../types';

/** 世界坐标矩形（宽高用 w/h，与参考仓 WorldRect 同形；canvas-selection 的 SelectionRect 是 width/height） */
export type WorldRect = { x: number; y: number; w: number; h: number };

/** 贝塞尔最小曲率：两点很近时也要拉出弧度，否则连线会和节点边框糊在一起 */
const MIN_CURVATURE = 50;

/** 框选命中放宽量：长横线中段更容易框到 */
const HIT_PADDING = 8;

/** 命中折线拟合步数（参考仓 48 段） */
const HIT_STEPS = 48;

/**
 * 连线两端锚点：源节点右侧中点 → 目标节点左侧中点（参考仓同口径）。
 * from/to 只读 position/width/height，节点尚未渲染也能算。
 */
export function connectionEndpoints(
  from: CanvasNodeData,
  to: CanvasNodeData,
): { startX: number; startY: number; endX: number; endY: number; curvature: number } {
  const startX = from.position.x + from.width;
  const startY = from.position.y + from.height / 2;
  const endX = to.position.x;
  const endY = to.position.y + to.height / 2;
  const curvature = Math.max(Math.abs(endX - startX) * 0.5, MIN_CURVATURE);
  return { startX, startY, endX, endY, curvature };
}

/** 两点 → 水平外延的三次贝塞尔路径（控制点各自水平外推 curvature） */
export function buildBezierPathD(
  startX: number,
  startY: number,
  endX: number,
  endY: number,
  curvature: number,
): string {
  return `M ${startX} ${startY} C ${startX + curvature} ${startY}, ${endX - curvature} ${endY}, ${endX} ${endY}`;
}

/** 已存在连线（from → to）的路径 d */
export function buildConnectionPathD(from: CanvasNodeData, to: CanvasNodeData): string {
  const { startX, startY, endX, endY, curvature } = connectionEndpoints(from, to);
  return buildBezierPathD(startX, startY, endX, endY, curvature);
}

/**
 * 拖拽中的临时连线：起点/终点都是世界坐标点，曲率按水平距离的一半
 * （参考仓 ActiveConnectionPath 用 `distance * 0.5`，没有 50 的下限——拉线短就该是直线段）。
 */
export function buildActiveConnectionPathD(start: Position, end: Position): string {
  const distance = Math.abs(end.x - start.x);
  return buildBezierPathD(start.x, start.y, end.x, end.y, distance * 0.5);
}

function pointInWorldRect(x: number, y: number, rect: WorldRect): boolean {
  return x >= rect.x && x <= rect.x + rect.w && y >= rect.y && y <= rect.y + rect.h;
}

function segmentsCross(
  ax: number,
  ay: number,
  bx: number,
  by: number,
  cx: number,
  cy: number,
  dx: number,
  dy: number,
): boolean {
  const cross = (ox: number, oy: number, px: number, py: number, qx: number, qy: number) =>
    (px - ox) * (qy - oy) - (py - oy) * (qx - ox);
  const d1 = cross(cx, cy, dx, dy, ax, ay);
  const d2 = cross(cx, cy, dx, dy, bx, by);
  const d3 = cross(ax, ay, bx, by, cx, cy);
  const d4 = cross(ax, ay, bx, by, dx, dy);
  return ((d1 > 0 && d2 < 0) || (d1 < 0 && d2 > 0)) && ((d3 > 0 && d4 < 0) || (d3 < 0 && d4 > 0));
}

function segmentIntersectsWorldRect(
  x1: number,
  y1: number,
  x2: number,
  y2: number,
  rect: WorldRect,
): boolean {
  if (pointInWorldRect(x1, y1, rect) || pointInWorldRect(x2, y2, rect)) return true;
  const { x, y, w, h } = rect;
  return (
    segmentsCross(x1, y1, x2, y2, x, y, x + w, y) ||
    segmentsCross(x1, y1, x2, y2, x + w, y, x + w, y + h) ||
    segmentsCross(x1, y1, x2, y2, x + w, y + h, x, y + h) ||
    segmentsCross(x1, y1, x2, y2, x, y + h, x, y)
  );
}

/**
 * 框选命中连线：把贝塞尔按 HIT_STEPS 折线化，逐段与（放宽 8px 的）矩形求交。
 * 先做控制多边形包围盒粗筛，绝大多数连线一眼就排除，不必逐段算。
 */
export function connectionIntersectsWorldRect(
  from: CanvasNodeData,
  to: CanvasNodeData,
  rect: WorldRect,
): boolean {
  if (!(rect.w > 0) || !(rect.h > 0)) return false;
  const hit: WorldRect = {
    x: rect.x - HIT_PADDING,
    y: rect.y - HIT_PADDING,
    w: rect.w + HIT_PADDING * 2,
    h: rect.h + HIT_PADDING * 2,
  };
  const { startX, startY, endX, endY, curvature } = connectionEndpoints(from, to);
  const p0x = startX;
  const p0y = startY;
  const p1x = startX + curvature;
  const p1y = startY;
  const p2x = endX - curvature;
  const p2y = endY;
  const p3x = endX;
  const p3y = endY;
  const minX = Math.min(p0x, p1x, p2x, p3x);
  const maxX = Math.max(p0x, p1x, p2x, p3x);
  const minY = Math.min(p0y, p1y, p2y, p3y);
  const maxY = Math.max(p0y, p1y, p2y, p3y);
  if (maxX < hit.x || minX > hit.x + hit.w || maxY < hit.y || minY > hit.y + hit.h) return false;

  let prevX = p0x;
  let prevY = p0y;
  if (pointInWorldRect(prevX, prevY, hit)) return true;
  for (let i = 1; i <= HIT_STEPS; i += 1) {
    const t = i / HIT_STEPS;
    const u = 1 - t;
    const x = u * u * u * p0x + 3 * u * u * t * p1x + 3 * u * t * t * p2x + t * t * t * p3x;
    const y = u * u * u * p0y + 3 * u * u * t * p1y + 3 * u * t * t * p2y + t * t * t * p3y;
    if (segmentIntersectsWorldRect(prevX, prevY, x, y, hit)) return true;
    prevX = x;
    prevY = y;
  }
  return false;
}

/** 命中的连线 id（保持 connections 原顺序，供框选提交） */
export function getConnectionsInSelectionRect(
  connections: CanvasConnection[],
  nodes: CanvasNodeData[],
  rect: WorldRect,
): string[] {
  if (!Array.isArray(connections) || !connections.length) return [];
  const byId = new Map(nodes.map((node) => [node.id, node]));
  const hit: string[] = [];
  for (const connection of connections) {
    const from = byId.get(connection.fromNodeId);
    const to = byId.get(connection.toNodeId);
    if (!from || !to) continue;
    if (connectionIntersectsWorldRect(from, to, rect)) hit.push(connection.id);
  }
  return hit;
}

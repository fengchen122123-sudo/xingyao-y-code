import { describe, expect, it } from 'vitest';

import {
  isDestructiveCanvasSnapshot,
  isSuspiciousCanvasNodeDrop,
  isSuspiciousCanvasProjectShrink,
  sanitizeCanvasProject,
  shouldPreferRicherCanvasSnapshot,
} from '../canvas-sanitize';
import { CanvasNodeType, type CanvasConnection, type CanvasNodeData, type CanvasProject } from '../../types';

function node(id: string, filled = false): CanvasNodeData {
  return {
    id,
    type: CanvasNodeType.Image,
    title: id,
    position: { x: 0, y: 0 },
    width: 100,
    height: 100,
    metadata: filled ? { content: `http://127.0.0.1/media/${id}.png` } : undefined,
  };
}

function connection(id: string, fromNodeId: string, toNodeId: string): CanvasConnection {
  return { id, fromNodeId, toNodeId };
}

describe('canvas-sanitize', () => {
  it('sanitizeCanvasProject：剔除引用已删除节点的悬空连线并同步 nodeCount/connectionCount', () => {
    const project = {
      nodes: [node('a'), node('b')],
      connections: [connection('c1', 'a', 'b'), connection('c2', 'b', 'ghost'), connection('c3', 'ghost', 'a')],
      nodeCount: 9,
      connectionCount: 9,
    };

    const result = sanitizeCanvasProject(project);

    expect(result.nodes).toHaveLength(2);
    expect(result.connections.map((item) => item.id)).toEqual(['c1']);
    expect(result.nodeCount).toBe(2);
    expect(result.connectionCount).toBe(1);
  });

  it('sanitizeCanvasProject：nodes/connections 非数组时兜底为空数组并清零计数', () => {
    const result = sanitizeCanvasProject({
      nodes: undefined,
      connections: null,
    } as unknown as Pick<CanvasProject, 'nodes' | 'connections' | 'nodeCount' | 'connectionCount'>);

    expect(result.nodes).toEqual([]);
    expect(result.connections).toEqual([]);
    expect(result.nodeCount).toBe(0);
    expect(result.connectionCount).toBe(0);
  });

  it('isDestructiveCanvasSnapshot：仅「节点清空但连线仍在」判定为破坏性快照', () => {
    expect(isDestructiveCanvasSnapshot([], [connection('c1', 'a', 'b')])).toBe(true);
    expect(isDestructiveCanvasSnapshot([], [])).toBe(false);
    expect(isDestructiveCanvasSnapshot([node('a')], [])).toBe(false);
  });

  it('isSuspiciousCanvasNodeDrop：3/8/75% 阈值边界', () => {
    // 直接清零永远可疑
    expect(isSuspiciousCanvasNodeDrop(1, 0)).toBe(true);
    // 少 3 个即可疑；少 2 个不可疑
    expect(isSuspiciousCanvasNodeDrop(5, 2)).toBe(true);
    expect(isSuspiciousCanvasNodeDrop(5, 3)).toBe(false);
    // 节点基数 <3 时只看清零
    expect(isSuspiciousCanvasNodeDrop(2, 1)).toBe(false);
    // ≥8 个节点时跌破 75%：8→5（ceil 6）可疑，8→6 恰在阈值上不可疑，10→7 可疑
    expect(isSuspiciousCanvasNodeDrop(8, 5)).toBe(true);
    expect(isSuspiciousCanvasNodeDrop(8, 6)).toBe(false);
    expect(isSuspiciousCanvasNodeDrop(10, 7)).toBe(true);
    // 增加节点永远不可疑
    expect(isSuspiciousCanvasNodeDrop(4, 8)).toBe(false);
  });

  it('isSuspiciousCanvasProjectShrink：连线骤降（10→0、20→低于半数）判定为收缩', () => {
    const shrink = (prevConns: number, nextConns: number, nextNodes: number) =>
      isSuspiciousCanvasProjectShrink(
        { nodes: new Array(6).fill(0).map((_, index) => node(`n${index}`)), connections: new Array(prevConns).fill(0).map((_, index) => connection(`c${index}`, 'n0', 'n1')) },
        { nodes: new Array(nextNodes).fill(0).map((_, index) => node(`n${index}`)), connections: new Array(nextConns).fill(0).map((_, index) => connection(`c${index}`, 'n0', 'n1')) },
      );

    // 10 条以上连线被清空、但节点还在
    expect(shrink(12, 0, 6)).toBe(true);
    // 20 条以上连线掉到不足一半
    expect(shrink(20, 9, 6)).toBe(true);
    // 恰好一半不算收缩
    expect(shrink(20, 10, 6)).toBe(false);
    // 9 条连线清零：未达 10 条阈值，不按连线收缩处理
    expect(shrink(9, 0, 6)).toBe(false);
  });

  it('shouldPreferRicherCanvasSnapshot：节点数相同且一方内容更满时优先保留更满的一方', () => {
    const previous = {
      nodes: [node('a', true), node('b', true)],
      connections: [connection('c1', 'a', 'b')],
      updatedAt: 1,
    };
    const next = {
      nodes: [node('a', true), node('b', false)],
      connections: [connection('c1', 'a', 'b')],
      updatedAt: 2,
    };

    expect(shouldPreferRicherCanvasSnapshot(previous, next)).toBe(true);
    expect(shouldPreferRicherCanvasSnapshot(next, previous)).toBe(false);
    // 节点数不同且不构成骤降 → 不干预
    const shorter = { nodes: [node('a', true)], connections: [], updatedAt: 3 };
    expect(shouldPreferRicherCanvasSnapshot(shorter, previous)).toBe(false);
  });
});

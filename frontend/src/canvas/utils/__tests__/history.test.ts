// M3-T05 单测：HistoryStack 的合并窗口、容量上限、undo/redo 往返与深拷贝隔离。
// 时间源注入假时钟：合并窗口是行为的一部分，不能靠真实等待来验。
import { describe, expect, it } from 'vitest';

import type { CanvasNodeData } from '../../types';
import { HISTORY_CAPACITY, HistoryStack, type CanvasHistorySnapshot } from '../history';

function makeNode(x: number, id = 'n-1'): CanvasNodeData {
  return { id, type: 'image', title: '图片', position: { x, y: x }, width: 100, height: 80 };
}

function snap(x: number): CanvasHistorySnapshot {
  return { nodes: [makeNode(x)], connections: [] };
}

/** 假时钟：tick 推进毫秒，now 供栈判定合并窗口 */
function makeClock(start = 0) {
  let now = start;
  return {
    now: () => now,
    tick: (ms: number) => {
      now += ms;
    },
  };
}

describe('utils/history HistoryStack', () => {
  it('空栈 undo/redo 返回 null，canUndo/canRedo 为 false', () => {
    const stack = new HistoryStack({ now: makeClock().now });

    expect(stack.canUndo).toBe(false);
    expect(stack.canRedo).toBe(false);
    expect(stack.undo()).toBeNull();
    expect(stack.redo()).toBeNull();
  });

  it('合并窗口：300ms 内连续 push 合成一条（保留首个快照），超窗才独立成记录', () => {
    const clock = makeClock();
    const stack = new HistoryStack({ now: clock.now });

    stack.push(snap(0));
    clock.tick(100);
    stack.push(snap(1));
    clock.tick(100);
    stack.push(snap(2));

    expect(stack.undoSize).toBe(1);
    const restored = stack.undo();
    expect(restored?.nodes[0].position.x).toBe(0); // 撤销目标＝burst 之前的首个快照
    expect(stack.canUndo).toBe(false);

    // 边界：正好 300ms 仍合并，301ms 起独立成记录
    const edge = new HistoryStack({ now: clock.now });
    edge.push(snap(0));
    clock.tick(300);
    edge.push(snap(1));
    expect(edge.undoSize).toBe(1);
    clock.tick(301);
    edge.push(snap(2));
    expect(edge.undoSize).toBe(2);
  });

  it('undo/redo 往返：先回到变更前，再回到变更后', () => {
    const clock = makeClock();
    const stack = new HistoryStack({ now: clock.now });

    stack.push(snap(0)); // 变更前
    clock.tick(1000);

    const back = stack.undo(snap(50)); // 当前状态（已改到 50）
    expect(back?.nodes[0].position.x).toBe(0);
    expect(stack.canRedo).toBe(true);

    const forward = stack.redo(snap(0));
    expect(forward?.nodes[0].position.x).toBe(50);
    expect(stack.canUndo).toBe(true);
  });

  it('新变更让 redo 分支失效（不会重做出一份过期状态）', () => {
    const clock = makeClock();
    const stack = new HistoryStack({ now: clock.now });

    stack.push(snap(0));
    clock.tick(1000);
    stack.push(snap(1));
    clock.tick(1000);

    stack.undo(snap(2));
    expect(stack.canRedo).toBe(true);

    stack.push(snap(3)); // 用户在撤销后又改了东西
    expect(stack.canRedo).toBe(false);
  });

  it('容量上限 50：超出丢最旧的记录', () => {
    const clock = makeClock();
    const stack = new HistoryStack({ now: clock.now });

    for (let i = 0; i < HISTORY_CAPACITY + 5; i += 1) {
      stack.push(snap(i));
      clock.tick(1000);
    }

    expect(stack.undoSize).toBe(HISTORY_CAPACITY);
    // 连续撤销到底时，回的是「第 6 份被 push 的快照」（前 5 份已被挤掉）
    let last: CanvasHistorySnapshot | null = null;
    for (let i = 0; i < HISTORY_CAPACITY; i += 1) last = stack.undo();
    expect(last?.nodes[0].position.x).toBe(5);
    expect(stack.undo()).toBeNull();
  });

  it('快照深拷贝：push 后改原对象不污染栈', () => {
    const stack = new HistoryStack({ now: makeClock().now });
    const node = makeNode(0);
    const snapshot: CanvasHistorySnapshot = { nodes: [node], connections: [] };

    stack.push(snapshot);
    node.position = { x: 999, y: 999 };
    snapshot.connections.push({ id: 'c_1', fromNodeId: 'n-1', toNodeId: 'n-1' });

    const restored = stack.undo();
    expect(restored?.nodes[0].position).toEqual({ x: 0, y: 0 });
    expect(restored?.connections).toEqual([]);
  });
});

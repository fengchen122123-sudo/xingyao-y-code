// M3-T02 store 层单测：moveNodes（组带动/去重）、resizeNode（48 下限与 freeResize）、
// commitGroupSnap（进出组改 groupId）、updateNodeText 落点。
// 桥整体 mock：这一层只验内存语义，落盘由 scheduleSave 断言。
import { createPinia, setActivePinia } from 'pinia';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { CanvasNodeData, CanvasProject } from '../canvas/types';
import { GROUP_PADDING } from '../canvas/utils/canvas-group';
import { __clearCanvasSaveTimer, useCanvasStore } from './canvas';

vi.mock('../canvas/bridge', () => ({
  canvasBridge: {
    listProjects: vi.fn(async () => ({ ok: true, projects: [] })),
    loadProject: vi.fn(async () => ({ ok: false, msg: '旧版数据，不支持' })),
    saveProject: vi.fn(async () => ({ ok: true, updatedAt: 1700000000000 })),
    deleteProject: vi.fn(async () => ({ ok: true })),
    saveMedia: vi.fn(async () => ({ ok: false })),
    resolveMedia: vi.fn(async () => ({ ok: false })),
  },
}));

function makeNode(overrides: Partial<CanvasNodeData> = {}): CanvasNodeData {
  return {
    id: 'n-1',
    type: 'image',
    title: '图片',
    position: { x: 0, y: 0 },
    width: 340,
    height: 240,
    metadata: {},
    ...overrides,
  };
}

const GROUP = makeNode({ id: 'g-1', type: 'group', title: '分组', position: { x: 0, y: 0 }, width: 760, height: 480 });

function makeProject(nodes: CanvasNodeData[]): CanvasProject {
  return {
    id: 'p-test',
    title: '拖拽缩放',
    createdAt: 0,
    updatedAt: 0,
    nodes,
    connections: [],
    viewport: { x: 0, y: 0, k: 1 },
    backgroundMode: 'grid',
    sidePanel: { open: false, width: 320, nodeId: null },
    nodeCount: nodes.length,
    connectionCount: 0,
    schema_version: 2,
  };
}

function setup(nodes: CanvasNodeData[]) {
  const pinia = createPinia();
  setActivePinia(pinia);
  const store = useCanvasStore();
  store.project = makeProject(nodes);
  const scheduleSave = vi.spyOn(store, 'scheduleSave');
  return { store, scheduleSave };
}

function nodeOf(store: ReturnType<typeof useCanvasStore>, id: string): CanvasNodeData {
  return store.project!.nodes.find((n) => n.id === id)!;
}

beforeEach(() => {
  __clearCanvasSaveTimer();
  vi.clearAllMocks();
});

afterEach(() => {
  __clearCanvasSaveTimer();
});

describe('stores/canvas moveNodes', () => {
  it('按世界坐标增量平移单节点并触发 scheduleSave', () => {
    const { store, scheduleSave } = setup([makeNode({ position: { x: 40, y: 60 } })]);

    store.moveNodes(['n-1'], 12, -8);

    expect(nodeOf(store, 'n-1').position).toEqual({ x: 52, y: 52 });
    expect(scheduleSave).toHaveBeenCalledTimes(1);
  });

  it('拖动分组带动 groupId 归属它的子节点，组外节点不动', () => {
    const child = makeNode({ id: 'n-child', position: { x: 100, y: 100 }, metadata: { groupId: 'g-1' } });
    const outsider = makeNode({ id: 'n-out', position: { x: 900, y: 900 } });
    const { store } = setup([GROUP, child, outsider]);

    store.moveNodes(['g-1'], 30, 45);

    expect(nodeOf(store, 'g-1').position).toEqual({ x: 30, y: 45 });
    expect(nodeOf(store, 'n-child').position).toEqual({ x: 130, y: 145 });
    expect(nodeOf(store, 'n-out').position).toEqual({ x: 900, y: 900 });
  });

  it('ids 同时含组与子节点时子节点只位移一次（不叠两遍增量）', () => {
    const child = makeNode({ id: 'n-child', position: { x: 100, y: 100 }, metadata: { groupId: 'g-1' } });
    const { store, scheduleSave } = setup([GROUP, child]);

    store.moveNodes(['g-1', 'n-child'], 10, 0);

    expect(nodeOf(store, 'n-child').position).toEqual({ x: 110, y: 100 });
    // 空增量 / 不存在的 id 不写盘（先清掉上面那次位移的计数）
    scheduleSave.mockClear();
    store.moveNodes(['n-child'], 0, 0);
    store.moveNodes(['missing'], 5, 5);
    expect(scheduleSave).not.toHaveBeenCalled();
  });
});

describe('stores/canvas resizeNode', () => {
  it('freeResize 节点自由缩放，宽高最小 48', () => {
    const free = makeNode({ metadata: { freeResize: true } });
    const { store } = setup([free]);

    store.resizeNode('n-1', { x: 10, y: 20, width: 500, height: 300 });
    expect(nodeOf(store, 'n-1')).toMatchObject({ width: 500, height: 300, position: { x: 10, y: 20 } });

    store.resizeNode('n-1', { width: 5, height: 5 });
    expect(nodeOf(store, 'n-1')).toMatchObject({ width: 48, height: 48 });
  });

  it('未开 freeResize 时锁纵横比（340×240 → 宽主导得 680×480）', () => {
    const { store } = setup([makeNode({ width: 340, height: 240 })]);

    store.resizeNode('n-1', { width: 680, height: 300 });

    expect(nodeOf(store, 'n-1').width).toBe(680);
    expect(nodeOf(store, 'n-1').height).toBe(480);
  });
});

describe('stores/canvas commitGroupSnap', () => {
  it('落点在组内：吸附到 GROUP_PADDING 内并写 metadata.groupId', () => {
    const node = makeNode({ id: 'n-1', position: { x: -100, y: 100 } });
    const { store, scheduleSave } = setup([GROUP, node]);

    store.commitGroupSnap('n-1');

    expect(nodeOf(store, 'n-1').position.x).toBe(GROUP.position.x + GROUP_PADDING);
    expect(nodeOf(store, 'n-1').metadata?.groupId).toBe('g-1');
    expect(scheduleSave).toHaveBeenCalled();
  });

  it('落点在组外：清除原 groupId（出组），分组自身不参与吸附', () => {
    const node = makeNode({ id: 'n-1', position: { x: 2000, y: 2000 }, metadata: { groupId: 'g-1' } });
    const { store } = setup([GROUP, node]);

    store.commitGroupSnap('n-1');

    expect(nodeOf(store, 'n-1').metadata?.groupId).toBeUndefined();
    expect('groupId' in (nodeOf(store, 'n-1').metadata ?? {})).toBe(false);
    // 分组节点自身调用是 no-op（不会把自己塞进别的组）
    store.commitGroupSnap('g-1');
    expect(nodeOf(store, 'g-1').metadata?.groupId).toBeUndefined();
  });
});

describe('stores/canvas updateNodeText', () => {
  it('update-text 落 metadata.content；内容未变不写盘', () => {
    const { store, scheduleSave } = setup([makeNode({ type: 'text', metadata: { content: '旧' } })]);

    store.updateNodeText('n-1', '新文本');
    expect(nodeOf(store, 'n-1').metadata?.content).toBe('新文本');
    expect(scheduleSave).toHaveBeenCalledTimes(1);

    store.updateNodeText('n-1', '新文本');
    expect(scheduleSave).toHaveBeenCalledTimes(1);
  });
});

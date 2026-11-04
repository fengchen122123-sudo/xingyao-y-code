import { createPinia, setActivePinia } from 'pinia';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { canvasBridge } from '../canvas/bridge';
import { CanvasNodeType } from '../canvas/types';
import { __clearCanvasSaveTimer, useCanvasStore } from './canvas';

// 桥整体 mock 掉：本文件只验证 store 骨架的字段/防抖/选中/托盘，
// 桥调用次数用 mock 断言，真实桥参数透传由 bridge 自身的后续测试覆盖
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

const saveProject = vi.mocked(canvasBridge.saveProject);
const loadProject = vi.mocked(canvasBridge.loadProject);

beforeEach(() => {
  setActivePinia(createPinia());
  __clearCanvasSaveTimer();
  vi.clearAllMocks();
});

afterEach(() => {
  __clearCanvasSaveTimer();
  vi.useRealTimers();
});

describe('stores/canvas 骨架', () => {
  it('createProject：schema_version=2、空 nodes/connections、viewport {0,0,1}', async () => {
    const store = useCanvasStore();
    await store.createProject('测试画布');

    const project = store.project;
    expect(project).toBeTruthy();
    expect(project!.schema_version).toBe(2);
    expect(project!.nodes).toEqual([]);
    expect(project!.connections).toEqual([]);
    expect(project!.viewport).toEqual({ x: 0, y: 0, k: 1 });
    expect(project!.title).toBe('测试画布');
    // id 只出字母数字（后端白名单 [A-Za-z0-9_-]）
    expect(project!.id).toMatch(/^p[a-z0-9]+$/i);
    expect(project!.nodeCount).toBe(0);
    expect(project!.connectionCount).toBe(0);
    expect(project!.backgroundMode).toBe('grid');
    expect(store.selectedNodeIds.size).toBe(0);
    expect(store.selectedConnectionIds.size).toBe(0);
    // 新建即落盘一次（新项目要出现在列表里，不能等防抖）
    expect(saveProject).toHaveBeenCalledTimes(1);
    expect(saveProject.mock.calls[0][0].schema_version).toBe(2);
  });

  it('createProject：未给标题时落默认名', async () => {
    const store = useCanvasStore();
    await store.createProject();
    expect(store.project!.title).toBe('未命名画布');
  });

  it('scheduleSave：800ms 内多次改动只保存一次，且保存最后一次视口', async () => {
    vi.useFakeTimers();
    const store = useCanvasStore();
    await store.createProject('防抖');
    saveProject.mockClear();

    store.setViewport({ x: 10, y: 0, k: 1 });
    store.setViewport({ x: 20, y: 0, k: 1 });
    store.setViewport({ x: 30, y: 0, k: 1 });

    await vi.advanceTimersByTimeAsync(799);
    expect(saveProject).not.toHaveBeenCalled();

    await vi.advanceTimersByTimeAsync(1);
    expect(saveProject).toHaveBeenCalledTimes(1);
    expect(saveProject.mock.calls[0][0].viewport).toEqual({ x: 30, y: 0, k: 1 });
  });

  it('focusNode：选中目标节点并清空连线选中（M2 只做选中）', async () => {
    const store = useCanvasStore();
    await store.createProject('聚焦');
    const project = store.project!;
    project.nodes = [
      { id: 'n1', type: CanvasNodeType.Image, title: '图一', position: { x: 0, y: 0 }, width: 120, height: 120 },
      { id: 'n2', type: CanvasNodeType.Text, title: '文本', position: { x: 40, y: 0 }, width: 160, height: 80 },
    ];
    store.selectedNodeIds = new Set(['n2']);
    store.selectedConnectionIds = new Set(['c1']);

    store.focusNode('n1');

    expect([...store.selectedNodeIds]).toEqual(['n1']);
    expect(store.selectedConnectionIds.size).toBe(0);
    // 视口 M2 不动（居中随 InfiniteCanvas 落地）
    expect(project.viewport).toEqual({ x: 0, y: 0, k: 1 });
  });

  it('ingestGeneratedImages：追加去重（同 storageKey 只留一条）', () => {
    const store = useCanvasStore();
    store.ingestGeneratedImages([
      { kind: 'image', dataUrl: 'file:///C:/a.png', title: 'a', storageKey: 'C:/a.png' },
    ]);
    store.ingestGeneratedImages([
      { kind: 'image', dataUrl: 'file:///C:/a.png', title: 'a', storageKey: 'C:/a.png' },
      { kind: 'image', dataUrl: 'file:///C:/b.png', title: 'b', storageKey: 'C:/b.png' },
      { kind: 'text', content: '提示词', title: '文本' },
    ]);

    expect(store.recentOutputs).toHaveLength(3);
    expect(store.recentOutputs.map((item) => item.title)).toEqual(['a', 'b', '文本']);
  });

  it('openProject：v1 旧项目被拒时落 loadError，不覆盖当前项目', async () => {
    const store = useCanvasStore();
    await store.createProject('当前');
    const current = store.project;

    await store.openProject('old-v1');

    expect(loadProject).toHaveBeenCalledWith('old-v1');
    expect(store.loadError).toBe('旧版数据，不支持');
    expect(store.project).toBe(current);
  });
});

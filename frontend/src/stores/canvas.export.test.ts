// M3-T07：项目导出 / 导入的 store 单测。
// 覆盖计划里点名的四件事：导入 id 重生成 + 连线重映射、悬空连线剔除、schema_version≠2 拒绝、
// 导出剥离运行期字段。桥整体 mock 掉（与 canvas.test.ts 同口径），只验证纯逻辑与落盘调用。
import { createPinia, setActivePinia } from 'pinia';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { canvasBridge } from '../canvas/bridge';
import { CanvasNodeType } from '../canvas/types';
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

const saveProject = vi.mocked(canvasBridge.saveProject);

/** 一份形状合法的 v2 导出文件（节点字段按 deserializeNodes 的校验口径给全） */
function importableJson(overrides: Record<string, unknown> = {}): string {
  return JSON.stringify({
    id: 'p_exported',
    title: '导入的画布',
    createdAt: 1700000000000,
    updatedAt: 1700000000001,
    viewport: { x: 10, y: 20, k: 1.5 },
    backgroundMode: 'dots',
    sidePanel: { open: true, width: 420, nodeId: 'n_a' },
    nodes: [
      {
        id: 'n_a',
        type: CanvasNodeType.Image,
        title: '图A',
        position: { x: 0, y: 0 },
        width: 120,
        height: 90,
        metadata: { storageKey: 'media/a.png', prompt: '一只猫' },
      },
      {
        id: 'n_b',
        type: CanvasNodeType.Text,
        title: '文B',
        position: { x: 200, y: 0 },
        width: 100,
        height: 60,
      },
    ],
    connections: [{ id: 'c_1', fromNodeId: 'n_a', toNodeId: 'n_b' }],
    nodeCount: 2,
    connectionCount: 1,
    schema_version: 2,
    ...overrides,
  });
}

beforeEach(() => {
  setActivePinia(createPinia());
  __clearCanvasSaveTimer();
  vi.clearAllMocks();
});

afterEach(() => {
  __clearCanvasSaveTimer();
});

describe('stores/canvas 导入项目（M3-T07）', () => {
  it('导入：节点与连线 id 全部重生成，连线端点跟随映射、布局与视口保留', async () => {
    const store = useCanvasStore();
    const res = await store.importProject(importableJson());

    expect(res).toEqual({ ok: true });
    const project = store.project;
    expect(project).toBeTruthy();
    // 项目 id 重新生成（不复用导出件里的 id，避免和现有存档撞车）
    expect(project!.id).not.toBe('p_exported');
    expect(project!.id).toMatch(/^p[a-z0-9]+$/i);
    expect(project!.schema_version).toBe(2);
    expect(project!.title).toBe('导入的画布');
    expect(project!.viewport).toEqual({ x: 10, y: 20, k: 1.5 });
    expect(project!.backgroundMode).toBe('dots');
    // 侧栏是纯 UI 态，导入后收起
    expect(project!.sidePanel).toEqual({ open: false, width: 320, nodeId: null });

    // 节点 id 全换、类型/坐标/尺寸/metadata 保留
    const nodeIds = project!.nodes.map((node) => node.id);
    expect(nodeIds).toHaveLength(2);
    expect(nodeIds).not.toContain('n_a');
    expect(nodeIds).not.toContain('n_b');
    expect(new Set(nodeIds).size).toBe(2);
    for (const id of nodeIds) expect(id).toMatch(/^n_/);
    const image = project!.nodes.find((node) => node.type === CanvasNodeType.Image)!;
    expect(image.position).toEqual({ x: 0, y: 0 });
    expect(image.width).toBe(120);
    expect(image.metadata?.storageKey).toBe('media/a.png');

    // 连线按 idMap 重映射：from 仍指向图节点、to 仍指向文本节点
    expect(project!.connections).toHaveLength(1);
    const [connection] = project!.connections;
    const text = project!.nodes.find((node) => node.type === CanvasNodeType.Text)!;
    expect(connection.id).not.toBe('c_1');
    expect(connection.fromNodeId).toBe(image.id);
    expect(connection.toNodeId).toBe(text.id);
    expect(project!.nodeCount).toBe(2);
    expect(project!.connectionCount).toBe(1);

    // 新建项目语义：立刻落盘一次 + 刷新列表一次
    expect(saveProject).toHaveBeenCalledTimes(1);
    expect(saveProject.mock.calls[0][0].nodes).toHaveLength(2);
  });

  it('导入：悬空连线剔除，metadata 内的分组引用按新 id 重映射', async () => {
    const store = useCanvasStore();
    const res = await store.importProject(
      importableJson({
        nodes: [
          {
            id: 'n_group',
            type: CanvasNodeType.Group,
            title: '分组',
            position: { x: -20, y: -20 },
            width: 400,
            height: 300,
          },
          {
            id: 'n_child',
            type: CanvasNodeType.Text,
            title: '子节点',
            position: { x: 0, y: 0 },
            width: 100,
            height: 60,
            metadata: { groupId: 'n_group' },
          },
        ],
        // 一条合法（组内两节点之间）+ 一条端点不存在
        connections: [
          { id: 'c_ok', fromNodeId: 'n_child', toNodeId: 'n_group' },
          { id: 'c_dangling', fromNodeId: 'n_child', toNodeId: 'n_ghost' },
        ],
      }),
    );

    expect(res.ok).toBe(true);
    const project = store.project!;
    expect(project.nodes).toHaveLength(2);
    // 悬空连线被剔除，只剩那条两端都在的
    expect(project.connections).toHaveLength(1);
    expect(project.connections[0].fromNodeId).not.toBe('n_child');
    expect(project.connections[0].toNodeId).not.toBe('n_group');
    expect(project.nodeCount).toBe(2);
    expect(project.connectionCount).toBe(1);

    // 分组归属跟着新分组 id 走（否则子节点会指向一个不存在的分组）
    const group = project.nodes.find((node) => node.type === CanvasNodeType.Group)!;
    const child = project.nodes.find((node) => node.type === CanvasNodeType.Text)!;
    expect(child.metadata?.groupId).toBe(group.id);
  });

  it('导入：schema_version 不是 2 → 明确回「旧版数据，不支持」，不新建、不落盘', async () => {
    const store = useCanvasStore();
    const res = await store.importProject(importableJson({ schema_version: 1 }));

    expect(res.ok).toBe(false);
    expect(res.msg).toBe('旧版数据，不支持');
    expect(store.project).toBeNull();
    expect(saveProject).not.toHaveBeenCalled();
  });

  it('导入：非 JSON / 缺节点列表 / 全是非法节点 → 拒绝且不留半截状态', async () => {
    const store = useCanvasStore();

    expect(await store.importProject('not json at all')).toEqual({
      ok: false,
      msg: '不是有效的 JSON 文件',
    });
    expect(await store.importProject(JSON.stringify({ schema_version: 2, title: 'x' }))).toEqual({
      ok: false,
      msg: '画布数据缺少节点列表',
    });
    const broken = await store.importProject(
      importableJson({ nodes: [{ id: 'n_bad', type: 'text', position: { x: 'a' } }] }),
    );
    expect(broken.ok).toBe(false);
    expect(store.project).toBeNull();
    expect(saveProject).not.toHaveBeenCalled();
  });
});

describe('stores/canvas 导出项目（M3-T07）', () => {
  it('导出：剥掉 jobId / progress / startedAt，status=loading 归 idle，其余 metadata 原样', async () => {
    const store = useCanvasStore();
    await store.createProject('导出测试');
    const project = store.project!;
    project.nodes = [
      {
        id: 'n_1',
        type: CanvasNodeType.Image,
        title: '生成中',
        position: { x: 12, y: 34 },
        width: 100,
        height: 80,
        metadata: {
          status: 'loading',
          jobId: 'job_abc',
          progress: 0.4,
          startedAt: 1700000000000,
          prompt: '一只猫',
          storageKey: 'media/a.png',
        },
      },
    ];
    project.connections = [];

    const raw = store.exportProjectJson();
    expect(raw).not.toBe('');
    const parsed = JSON.parse(raw) as {
      schema_version: number;
      nodeCount: number;
      nodes: Array<{ id: string; position: { x: number; y: number }; metadata: Record<string, unknown> }>;
    };

    expect(parsed.schema_version).toBe(2);
    expect(parsed.nodeCount).toBe(1);
    const meta = parsed.nodes[0].metadata;
    expect(meta.status).toBe('idle');
    expect(meta).not.toHaveProperty('jobId');
    expect(meta).not.toHaveProperty('progress');
    expect(meta).not.toHaveProperty('startedAt');
    expect(meta.prompt).toBe('一只猫');
    expect(meta.storageKey).toBe('media/a.png');
    // 坐标等结构字段照旧
    expect(parsed.nodes[0].position).toEqual({ x: 12, y: 34 });

    // 导出是纯读操作：内存里的运行态不该被改掉
    expect(project.nodes[0].metadata?.jobId).toBe('job_abc');
    expect(project.nodes[0].metadata?.status).toBe('loading');
  });

  it('导出：悬空连线剔除并同步计数', async () => {
    const store = useCanvasStore();
    await store.createProject('导出连线');
    const project = store.project!;
    project.nodes = [
      { id: 'n_1', type: CanvasNodeType.Image, title: 'A', position: { x: 0, y: 0 }, width: 50, height: 50 },
      { id: 'n_2', type: CanvasNodeType.Text, title: 'B', position: { x: 80, y: 0 }, width: 50, height: 50 },
    ];
    project.connections = [
      { id: 'c_1', fromNodeId: 'n_1', toNodeId: 'n_2' },
      { id: 'c_2', fromNodeId: 'n_1', toNodeId: 'n_ghost' },
    ];
    project.nodeCount = 2;
    project.connectionCount = 2;

    const parsed = JSON.parse(store.exportProjectJson()) as {
      connections: Array<{ id: string }>;
      connectionCount: number;
    };
    expect(parsed.connections).toHaveLength(1);
    expect(parsed.connections[0].id).toBe('c_1');
    expect(parsed.connectionCount).toBe(1);
  });

  it('导出 → 导入往返：结构还原、id 全新、悬空连线不会回流', async () => {
    const store = useCanvasStore();
    await store.importProject(importableJson());
    const before = store.project!;
    const exported = store.exportProjectJson();
    expect(exported).not.toBe('');

    const res = await store.importProject(exported);
    expect(res.ok).toBe(true);
    const after = store.project!;
    expect(after.id).not.toBe(before.id);
    expect(after.nodes).toHaveLength(2);
    expect(after.connections).toHaveLength(1);
    expect(new Set(after.nodes.map((node) => node.id))).not.toContain(before.nodes[0].id);
    expect(after.title).toBe('导入的画布');
  });

  it('导出：没有打开项目时回空串（调用方据此提示，不写坏文件）', () => {
    const store = useCanvasStore();
    expect(store.project).toBeNull();
    expect(store.exportProjectJson()).toBe('');
  });
});
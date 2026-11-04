// M3-T05 单测：剪贴板序列化标记、坏数据容错、复制时 id 重生成/偏移/连线重映射/组关系保留。
import { describe, expect, it } from 'vitest';

import type { CanvasConnection, CanvasNodeData } from '../../types';
import {
  CANVAS_CLIPBOARD_PREFIX,
  PASTE_OFFSET,
  cloneNodes,
  deserializeNodes,
  serializeNodes,
} from '../clipboard';

function makeNode(overrides: Partial<CanvasNodeData> = {}): CanvasNodeData {
  return {
    id: 'n-a',
    type: 'image',
    title: '图片A',
    position: { x: 100, y: 200 },
    width: 320,
    height: 240,
    metadata: {},
    ...overrides,
  };
}

function makeConnection(id: string, from: string, to: string): CanvasConnection {
  return { id, fromNodeId: from, toNodeId: to };
}

describe('utils/clipboard 序列化', () => {
  it('带标记前缀；只收两端都在选区内的连线；空选区不产内容', () => {
    const nodes = [makeNode({ id: 'n-a' }), makeNode({ id: 'n-b' })];
    const connections = [
      makeConnection('c_1', 'n-a', 'n-b'), // 内部连线：要
      makeConnection('c_2', 'n-a', 'n-out'), // 指向选区外：丢
      makeConnection('c_3', 'n-out', 'n-b'), // 从选区外来：丢
    ];

    const raw = serializeNodes(nodes, connections);
    expect(raw.startsWith(CANVAS_CLIPBOARD_PREFIX)).toBe(true);

    const parsed = deserializeNodes(raw);
    expect(parsed?.nodes.map((n) => n.id)).toEqual(['n-a', 'n-b']);
    expect(parsed?.connections.map((c) => c.id)).toEqual(['c_1']);

    // 序列化是深拷贝：改原节点不影响已写好的内容
    nodes[0].position = { x: -1, y: -1 };
    expect(deserializeNodes(raw)?.nodes[0].position).toEqual({ x: 100, y: 200 });

    expect(serializeNodes([], connections)).toBe('');
  });

  it('坏数据返回 null；只是连线端点缺失则只剔除该线', () => {
    expect(deserializeNodes('')).toBeNull();
    expect(deserializeNodes('随便一段文本')).toBeNull();
    expect(deserializeNodes(`${CANVAS_CLIPBOARD_PREFIX}{不是JSON`)).toBeNull();
    expect(deserializeNodes(`${CANVAS_CLIPBOARD_PREFIX}{"nodes":[]}`)).toBeNull();
    expect(deserializeNodes(`${CANVAS_CLIPBOARD_PREFIX}{"nodes":[{"id":"n-a"}]}`)).toBeNull();
    expect(
      deserializeNodes(
        `${CANVAS_CLIPBOARD_PREFIX}{"nodes":[{"id":"n-a","type":"image","title":"x","position":{"x":0,"y":0},"width":0,"height":10}]}`,
      ),
    ).toBeNull();

    const withBadConnection = `${CANVAS_CLIPBOARD_PREFIX}${JSON.stringify({
      version: 1,
      nodes: [makeNode({ id: 'n-a' })],
      connections: [makeConnection('c_1', 'n-a', 'n-zzz')],
    })}`;
    const parsed = deserializeNodes(withBadConnection);
    expect(parsed?.nodes).toHaveLength(1);
    expect(parsed?.connections).toEqual([]);
  });
});

describe('utils/clipboard cloneNodes', () => {
  it('id 全部重生成且唯一，位置右下偏移 24px，job 态被抹平', () => {
    const payload = {
      nodes: [
        makeNode({ id: 'n-a', metadata: { jobId: 'job-1', status: 'loading', prompt: '猫' } }),
        makeNode({ id: 'n-b' }),
      ],
      connections: [],
    };

    const cloned = cloneNodes(payload);

    expect(cloned.nodes.map((n) => n.id)).not.toContain('n-a');
    expect(new Set(cloned.nodes.map((n) => n.id)).size).toBe(2);
    for (const node of cloned.nodes) {
      expect(node.id.startsWith('n_')).toBe(true);
    }
    expect(cloned.nodes[0].position).toEqual({
      x: 100 + PASTE_OFFSET,
      y: 200 + PASTE_OFFSET,
    });
    expect(cloned.nodes[0].metadata?.jobId).toBeUndefined();
    expect(cloned.nodes[0].metadata?.status).toBe('idle');
    expect(cloned.nodes[0].metadata?.prompt).toBe('猫');
  });

  it('连线按新 id 重映射，指向选区外的连线丢弃', () => {
    const payload = {
      nodes: [makeNode({ id: 'n-a' }), makeNode({ id: 'n-b' })],
      connections: [makeConnection('c_1', 'n-a', 'n-b'), makeConnection('c_2', 'n-a', 'n-out')],
    };

    const cloned = cloneNodes(payload);
    const newIds = cloned.nodes.map((n) => n.id);

    expect(cloned.connections).toHaveLength(1);
    expect(cloned.connections[0].id).not.toBe('c_1');
    expect(newIds).toContain(cloned.connections[0].fromNodeId);
    expect(newIds).toContain(cloned.connections[0].toNodeId);
    expect(cloned.connections[0].fromNodeId).not.toBe('n-a');
  });

  it('组关系：组在选区内保留并指向新组 id，组在选区外则清除 groupId', () => {
    const withGroup = cloneNodes({
      nodes: [
        makeNode({ id: 'g-1', type: 'group', title: '分组' }),
        makeNode({ id: 'n-a', metadata: { groupId: 'g-1' } }),
      ],
      connections: [],
    });
    const group = withGroup.nodes.find((n) => n.type === 'group');
    const child = withGroup.nodes.find((n) => n.id !== group?.id);
    expect(group).toBeDefined();
    expect(child?.metadata?.groupId).toBe(group?.id);

    const withoutGroup = cloneNodes({
      nodes: [makeNode({ id: 'n-a', metadata: { groupId: 'g-out' } })],
      connections: [],
    });
    expect(withoutGroup.nodes[0].metadata?.groupId).toBeUndefined();
    expect('groupId' in (withoutGroup.nodes[0].metadata ?? {})).toBe(false);
  });

  it('连续复制两次的 id 不冲突（可反复粘贴）', () => {
    const payload = { nodes: [makeNode({ id: 'n-a' })], connections: [] };

    const first = cloneNodes(payload);
    const second = cloneNodes(payload);

    expect(first.nodes[0].id).not.toBe(second.nodes[0].id);
    expect(first.nodes[0].position).toEqual(second.nodes[0].position);
  });
});

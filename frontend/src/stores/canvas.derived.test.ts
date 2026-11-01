// 画布改图溯源（A → B 血缘）的数据层契约。
//
// 这条链路跨一次完整对话回合：点「送去修改」→ 后端发进对话 → 模型改图 →
// 工具结果回到前端。发起与产出之间只有 store 活得下来，所以「源节点」
// 必须记在 store 里而不是组件里，这里把该约定钉死。
import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, describe, expect, it } from 'vitest';
import { DERIVED_NODE_GAP_X, useCanvasStore } from './canvas';

beforeEach(() => setActivePinia(createPinia()));

const SRC_FILE = 'C:\\tmp\\a.png';
const DERIVED_FILE = 'C:\\tmp\\b.png';

describe('canvas 改图溯源', () => {
  it('改图产物落成源图右侧节点，并连一条「改图」边', () => {
    const store = useCanvasStore();
    const src = store.addNode({ type: 'image', x: 100, y: 40, file: SRC_FILE, title: 'a.png' });
    store.markEditSource({ nodeId: src.id, file: SRC_FILE, title: 'a.png' });

    // 模拟 appStore 认领「送去修改」的源节点后推送产出
    const source = store.pendingEditSource;
    expect(source?.nodeId).toBe(src.id);
    store.pushRecentOutput({ path: DERIVED_FILE, sourceNodeId: src.id, sourceTitle: 'a.png' });

    expect(store.nodes).toHaveLength(2);
    const derived = store.nodes[1];
    expect(derived.file).toBe(DERIVED_FILE);
    expect(derived.title).toBe('b.png');
    // 落点在源图右侧一格的间距、同一行起手
    expect(derived.x).toBe(src.x + src.width + DERIVED_NODE_GAP_X);
    expect(derived.y).toBe(src.y);
    // meta 里另存一份来历，节点被单独拷走也不丢
    expect(derived.meta?.source_file).toBe(SRC_FILE);
    expect(derived.meta?.source_node_id).toBe(src.id);

    // 边的方向必须是源 → 产物，标签「改图」；画布靠它表达溯源
    expect(store.edges).toHaveLength(1);
    expect(store.edges[0]).toMatchObject({
      fromNode: src.id,
      toNode: derived.id,
      label: '改图',
    });
    // 已落成节点就不再进托盘，避免用户再点一次落出重复节点
    expect(store.recentOutputs).toHaveLength(0);
  });

  it('没有源节点时仍只进托盘，不落节点也不建边', () => {
    const store = useCanvasStore();
    store.pushRecentOutput({ path: DERIVED_FILE });

    expect(store.nodes).toHaveLength(0);
    expect(store.edges).toHaveLength(0);
    expect(store.recentOutputs.map((r) => r.path)).toEqual([DERIVED_FILE]);
  });

  it('源节点已被删除时降级为托盘条目，不抛错也不留悬空边', () => {
    const store = useCanvasStore();
    const src = store.addNode({ type: 'image', file: SRC_FILE });
    store.removeNode(src.id);

    store.pushRecentOutput({ path: DERIVED_FILE, sourceNodeId: src.id });

    expect(store.nodes).toHaveLength(0);
    expect(store.edges).toHaveLength(0);
    expect(store.recentOutputs.map((r) => r.path)).toEqual([DERIVED_FILE]);
  });

  it('同一条改图链路可以继续往下传（A→B→C 各自成边）', () => {
    const store = useCanvasStore();
    const a = store.addNode({ type: 'image', x: 0, y: 0, file: SRC_FILE, title: 'a.png' });
    store.pushRecentOutput({ path: DERIVED_FILE, sourceNodeId: a.id });
    const b = store.nodes[1];

    store.markEditSource({ nodeId: b.id, file: DERIVED_FILE, title: 'b.png' });
    store.pushRecentOutput({ path: 'C:\\tmp\\c.png', sourceNodeId: b.id });

    expect(store.nodes).toHaveLength(3);
    expect(store.edges.map((e) => [e.fromNode, e.toNode])).toEqual([
      [a.id, b.id],
      [b.id, store.nodes[2].id],
    ]);
  });
});

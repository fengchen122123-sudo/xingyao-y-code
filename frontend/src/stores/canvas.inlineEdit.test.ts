// 画布「就地改图」的 pending 状态与结果落地契约。
//
// 这条链路的时序是：点「生成」→ 后端立即返回 job_id（节点进入 pending）→
// 几十秒后 canvas_edit_result 事件到达。中间隔着一次网络往返和一次完整的
// 守护线程任务，组件可能已经重建，所以「谁在改」与「结果怎么落」都必须
// 由 store 承担，这里把约定钉死。
import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, describe, expect, it } from 'vitest';
import { DERIVED_NODE_GAP_X, useCanvasStore } from './canvas';
import { parseCanvasEditResult, useAppStore } from './appStore';

beforeEach(() => setActivePinia(createPinia()));

const SRC_FILE = 'C:\\tmp\\a.png';
// 后端落盘口径：相对 workdir 的媒体目录路径（见设计文档 §4.2）
const DERIVED_FILE = '.ycode/canvas/media/edit-1.png';

describe('canvas 就地改图 pending', () => {
  it('markEditPending 写入 pendingEdits，但不进 toDoc()、也不点亮未保存', () => {
    const store = useCanvasStore();
    const src = store.addNode({ type: 'image', x: 100, y: 40, file: SRC_FILE, title: 'a.png' });
    // addNode 自己会置脏，先清掉才能验证 pending 不碰 dirty
    store.dirty = false;

    store.markEditPending(src.id, 'job-1');

    expect(store.pendingEdits[src.id]).toBe('job-1');
    // pendingEdits 是纯 UI 态：落到画布文件里只会留下一堆过期 jobId
    const doc = store.toDoc();
    expect(Object.keys(doc).sort()).toEqual(['edges', 'nodes', 'schema_version']);
    expect(doc).not.toHaveProperty('pendingEdits');
    expect(store.dirty).toBe(false);

    // clearEditPending 只摘这一个键，其它节点不受影响
    store.markEditPending('node-other', 'job-2');
    store.clearEditPending(src.id);
    expect(store.pendingEdits[src.id]).toBeUndefined();
    expect(store.pendingEdits['node-other']).toBe('job-2');
  });
});

describe('canvas 就地改图结果落地', () => {
  it('成功且源节点还在：落下游节点、「改图」边、meta.source_file，并清 pending', () => {
    const store = useCanvasStore();
    const src = store.addNode({ type: 'image', x: 100, y: 40, file: SRC_FILE, title: 'a.png' });
    store.markEditPending(src.id, 'job-1');

    const ok = store.applyEditResult({
      ok: true,
      source_node_id: src.id,
      path: DERIVED_FILE,
      url: 'http://127.0.0.1:1/media/edit-1.png',
    });

    expect(ok).toBe(true);
    expect(store.nodes).toHaveLength(2);
    const derived = store.nodes[1];
    expect(derived.type).toBe('image');
    expect(derived.file).toBe(DERIVED_FILE);
    expect(derived.title).toBe('edit-1.png');
    // 落点与「送去修改」的下游节点同一套规则：源图右侧留一条边的间距、同一行
    expect(derived.x).toBe(src.x + src.width + DERIVED_NODE_GAP_X);
    expect(derived.y).toBe(src.y);
    // 溯源写进 meta：节点被单独拷到别的画布也不丢来历
    expect(derived.meta?.source_node_id).toBe(src.id);
    expect(derived.meta?.source_file).toBe(SRC_FILE);

    expect(store.edges).toHaveLength(1);
    expect(store.edges[0]).toMatchObject({
      fromNode: src.id,
      toNode: derived.id,
      label: '改图',
    });

    // 这一轮结束，遮罩必须收掉，否则节点会一直显示「改图中…」
    expect(store.pendingEdits[src.id]).toBeUndefined();
    // 已落成节点就不再重复进托盘
    expect(store.recentOutputs).toHaveLength(0);
  });

  it('成功但源节点已被删除：只进托盘，不抛错也不留悬空边', () => {
    const store = useCanvasStore();
    const src = store.addNode({ type: 'image', file: SRC_FILE, title: 'a.png' });
    store.markEditPending(src.id, 'job-1');
    // 用户在等结果的几十秒里删掉了这张图
    store.removeNode(src.id);

    let ok = false;
    expect(() => {
      ok = store.applyEditResult({ ok: true, source_node_id: src.id, path: DERIVED_FILE });
    }).not.toThrow();

    // 产物不丢：降级成托盘条目，用户还能自己点进画布
    expect(ok).toBe(true);
    expect(store.nodes).toHaveLength(0);
    expect(store.edges).toHaveLength(0);
    expect(store.recentOutputs.map((r) => r.path)).toEqual([DERIVED_FILE]);
    // 节点都没了，pending 也没有残留（clearEditPending 对不存在的键静默返回）
    expect(store.pendingEdits[src.id]).toBeUndefined();
  });

  it('失败：不落节点、不建边，pending 清空且返回 false', () => {
    const store = useCanvasStore();
    const src = store.addNode({ type: 'image', x: 100, y: 40, file: SRC_FILE, title: 'a.png' });
    store.markEditPending(src.id, 'job-1');
    const nodesBefore = store.nodes.length;

    // 失败事件只带 ok:false + error，没有 path/url，正好验证「只清 pending」这条分支
    const ok = store.applyEditResult({ ok: false, source_node_id: src.id });

    // false 让组件层知道要 showTip
    expect(ok).toBe(false);
    expect(store.nodes).toHaveLength(nodesBefore);
    expect(store.edges).toHaveLength(0);
    expect(store.recentOutputs).toHaveLength(0);
    // 失败也要退 pending，否则按钮永远禁用
    expect(store.pendingEdits[src.id]).toBeUndefined();
  });

  it('成功但没带 path（空产物）：同样只清 pending 并返回 false', () => {
    const store = useCanvasStore();
    const src = store.addNode({ type: 'image', file: SRC_FILE });
    store.markEditPending(src.id, 'job-1');

    expect(store.applyEditResult({ ok: true, source_node_id: src.id, path: '   ' })).toBe(false);
    expect(store.nodes).toHaveLength(1);
    expect(store.recentOutputs).toHaveLength(0);
    expect(store.pendingEdits[src.id]).toBeUndefined();
  });
});

// 桥事件载荷的解包：这是「后端 → 画布」的唯一入口，畸形数据不能把
// handleBridgeEvent 整条链路打断（同一处理器里还有 token_delta 这类高频流）
describe('parseCanvasEditResult', () => {
  it('JSON 字符串能解包成结果对象', () => {
    const raw = JSON.stringify({
      ok: true,
      source_node_id: 'node-1',
      path: DERIVED_FILE,
      url: 'http://127.0.0.1:1/media/edit-1.png',
      error: '',
    });
    expect(parseCanvasEditResult(raw)).toEqual({
      ok: true,
      source_node_id: 'node-1',
      path: DERIVED_FILE,
      url: 'http://127.0.0.1:1/media/edit-1.png',
      error: '',
    });
    // 已经是对象时直接读，不做二次解析
    expect(parseCanvasEditResult({ ok: false, source_node_id: 'n2', error: '模型超时' })).toEqual({
      ok: false,
      source_node_id: 'n2',
      path: '',
      url: '',
      error: '模型超时',
    });
  });

  it('坏 JSON / 非对象一律返回全默认值且不抛', () => {
    const empty = { ok: false, source_node_id: '', path: '', url: '', error: '' };
    expect(parseCanvasEditResult('{不是 json')).toEqual(empty);
    expect(parseCanvasEditResult(null)).toEqual(empty);
    expect(parseCanvasEditResult(undefined)).toEqual(empty);
    expect(parseCanvasEditResult(42)).toEqual(empty);
  });
});

// 端到端走一遍分发函数：事件 → 解包 → applyEditResult，验证源节点落下游、
// pending 清空。这条路径上任何一环写错（事件名、字段名）都会让用户点了
// 「生成」之后永远停在「改图中…」
describe('appStore 分发 canvas_edit_result', () => {
  it('成功事件落下游节点并清 pending', () => {
    const canvas = useCanvasStore();
    const app = useAppStore();
    const src = canvas.addNode({ type: 'image', x: 100, y: 40, file: SRC_FILE, title: 'a.png' });
    canvas.markEditPending(src.id, 'job-1');

    app.handleBridgeEvent({
      type: 'canvas_edit_result',
      data: {
        ok: true,
        source_node_id: src.id,
        path: DERIVED_FILE,
        url: 'http://127.0.0.1:1/media/edit-1.png',
      },
    } as any);

    expect(canvas.nodes).toHaveLength(2);
    expect(canvas.nodes[1].file).toBe(DERIVED_FILE);
    expect(canvas.edges[0]).toMatchObject({ fromNode: src.id, toNode: canvas.nodes[1].id });
    expect(canvas.pendingEdits[src.id]).toBeUndefined();
  });

  it('失败事件只清 pending，不落节点、不建边', () => {
    const canvas = useCanvasStore();
    const app = useAppStore();
    const src = canvas.addNode({ type: 'image', x: 100, y: 40, file: SRC_FILE, title: 'a.png' });
    canvas.markEditPending(src.id, 'job-1');

    app.handleBridgeEvent({
      type: 'canvas_edit_result',
      data: JSON.stringify({ ok: false, source_node_id: src.id, error: '守护线程崩溃' }),
    } as any);

    expect(canvas.nodes).toHaveLength(1);
    expect(canvas.edges).toHaveLength(0);
    expect(canvas.pendingEdits[src.id]).toBeUndefined();
  });
});

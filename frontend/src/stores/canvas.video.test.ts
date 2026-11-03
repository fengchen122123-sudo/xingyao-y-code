// 画布「图片 → 视频」链路的前端契约。
//
// 与就地改图（canvas.inlineEdit.test.ts）同一套时序假设：点「生成视频」→
// 后端立即返回 job_id → 画布先落一个 pending 视频节点（用户能看见任务已受理）
// → 1~5 分钟后 canvas_video_result 事件到达，把产物回填进那个节点。
// 中间应用可能重启、节点可能被删，所以「谁在生成」「结果怎么落」都由 store
// 承担，这里把约定钉死。
import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, describe, expect, it } from 'vitest';
import { DERIVED_NODE_GAP_X, useCanvasStore } from './canvas';
import { parseCanvasVideoResult, useAppStore } from './appStore';

beforeEach(() => setActivePinia(createPinia()));

const SRC_FILE = 'C:\\tmp\\a.png';
const VIDEO_FILE = '.ycode/canvas/media/tokenrhythm_video_20261103_150000.mp4';
const VIDEO_URL = 'http://127.0.0.1:1/media/b.mp4';

describe('canvas 视频任务 pending', () => {
  it('addVideoJobNode 在源图右侧落 pending 视频节点 +「视频」边，jobId 进 pendingVideos', () => {
    const store = useCanvasStore();
    const src = store.addNode({ type: 'image', x: 100, y: 40, file: SRC_FILE, title: 'a.png' });

    const created = store.addVideoJobNode(src.id, 'job-v1', { prompt: '推近', duration: 5 });

    expect(created).not.toBeNull();
    expect(created!.type).toBe('video');
    // 落点与改图下游同一套规则：源图右侧留一条边的间距、同一行
    expect(created!.x).toBe(src.x + src.width + DERIVED_NODE_GAP_X);
    expect(created!.y).toBe(src.y);
    // 任务受理时就建节点，但产物还没回来：没有 file
    expect(created!.file).toBeUndefined();
    expect(created!.meta?.job_id).toBe('job-v1');
    expect(created!.meta?.source_node_id).toBe(src.id);
    expect(created!.meta?.source_file).toBe(SRC_FILE);
    expect(created!.meta?.duration_seconds).toBe(5);

    expect(store.edges).toHaveLength(1);
    expect(store.edges[0]).toMatchObject({ fromNode: src.id, toNode: created!.id, label: '视频' });
    expect(store.pendingVideos[created!.id]).toBe('job-v1');
  });

  it('pendingVideos 是纯 UI 态：不进 toDoc()，标记它也不点亮未保存', () => {
    const store = useCanvasStore();
    const src = store.addNode({ type: 'image', file: SRC_FILE });
    store.dirty = false;

    const created = store.addVideoJobNode(src.id, 'job-v1');
    // 建节点本身是内容变化（要落盘），但 pendingVideos 映射只是它的影子
    expect(store.dirty).toBe(true);
    store.dirty = false;

    const doc = store.toDoc();
    expect(doc).not.toHaveProperty('pendingVideos');
    // 节点上的 meta.job_id 会落盘（重启后靠它认领迟到的结果事件）
    const saved = doc.nodes.find((n) => n.id === created!.id);
    expect(saved?.meta?.job_id).toBe('job-v1');
  });

  it('markVideoPending / clearVideoPending 只动这一个键', () => {
    const store = useCanvasStore();
    store.markVideoPending('n-a', 'job-1');
    store.markVideoPending('n-b', 'job-2');
    store.clearVideoPending('n-a');
    expect(store.pendingVideos['n-a']).toBeUndefined();
    expect(store.pendingVideos['n-b']).toBe('job-2');
  });
});

describe('canvas 视频结果落地 applyVideoResult', () => {
  function setup() {
    const store = useCanvasStore();
    const src = store.addNode({ type: 'image', x: 100, y: 40, file: SRC_FILE, title: 'a.png' });
    const created = store.addVideoJobNode(src.id, 'job-v1', { prompt: '推近', duration: 5 });
    if (!created) throw new Error('addVideoJobNode 不该失败');
    return { store, src, created };
  }

  it('成功：把产物回填进 pending 视频节点（按 meta.job_id 认领），清 pending', () => {
    const { store, created } = setup();

    const ok = store.applyVideoResult({
      ok: true,
      job_id: 'job-v1',
      source_node_id: 'whatever',
      path: VIDEO_FILE,
      url: VIDEO_URL,
      duration: 5,
      model: 'wan3.0-video',
    });

    expect(ok).toBe(true);
    // 不新建节点：回填的就是当初那个 pending 节点
    expect(store.nodes.filter((n) => n.type === 'video')).toHaveLength(1);
    const node = store.nodes.find((n) => n.id === created.id)!;
    expect(node.file).toBe(VIDEO_FILE);
    expect(node.url).toBe(VIDEO_URL);
    expect(node.title).toBe('tokenrhythm_video_20261103_150000.mp4');
    expect(node.meta?.duration_seconds).toBe(5);
    expect(node.meta?.model).toBe('wan3.0-video');
    expect(node.meta?.error).toBeUndefined();
    expect(store.pendingVideos[created.id]).toBeUndefined();
  });

  it('失败：节点留痕 meta.error、退 pending、返回 false（节点不删，用户能看到失败原因）', () => {
    const { store, created } = setup();

    const ok = store.applyVideoResult({ ok: false, job_id: 'job-v1', error: '内容审核未通过' });

    expect(ok).toBe(false);
    const node = store.nodes.find((n) => n.id === created.id)!;
    expect(node.file).toBeUndefined();
    expect(node.meta?.error).toBe('内容审核未通过');
    expect(store.pendingVideos[created.id]).toBeUndefined();
  });

  it('结果事件先到、节点已删（应用重启丢 pending 节点）：按 source_node_id 重建下游视频节点', () => {
    const store = useCanvasStore();
    const src = store.addNode({ type: 'image', x: 100, y: 40, file: SRC_FILE, title: 'a.png' });

    const ok = store.applyVideoResult({
      ok: true,
      job_id: 'job-v1',
      source_node_id: src.id,
      path: VIDEO_FILE,
      url: VIDEO_URL,
      duration: 5,
      model: 'wan3.0-video',
    });

    expect(ok).toBe(true);
    const videos = store.nodes.filter((n) => n.type === 'video');
    expect(videos).toHaveLength(1);
    expect(videos[0].file).toBe(VIDEO_FILE);
    expect(store.edges[0]).toMatchObject({ fromNode: src.id, toNode: videos[0].id, label: '视频' });
  });

  it('失败且找不到 pending 节点：不抛错、不建节点，返回 false', () => {
    const store = useCanvasStore();
    const src = store.addNode({ type: 'image', file: SRC_FILE });

    const ok = store.applyVideoResult({ ok: false, job_id: 'job-x', source_node_id: src.id, error: '超时' });

    expect(ok).toBe(false);
    expect(store.nodes).toHaveLength(1);
    expect(store.edges).toHaveLength(0);
  });

  it('成功但没拿到 path：视为失败，不清 meta、不落产物', () => {
    const { store, created } = setup();
    const ok = store.applyVideoResult({ ok: true, job_id: 'job-v1', path: '  ' });
    expect(ok).toBe(false);
    expect(store.nodes.find((n) => n.id === created.id)!.file).toBeUndefined();
    expect(store.pendingVideos[created.id]).toBeUndefined();
  });
});

// 桥事件载荷解包：canvas_video_result 的字段比改图事件多 job_id/duration/model，
// 容错口径与 parseCanvasEditResult 一致（畸形数据不抛、不打断后续事件分发）
describe('parseCanvasVideoResult', () => {
  it('对象与 JSON 字符串都能解包', () => {
    const full = {
      ok: true,
      job_id: 'job-1',
      source_node_id: 'node-1',
      path: VIDEO_FILE,
      url: VIDEO_URL,
      duration: 5,
      model: 'wan3.0-video',
      error: '',
    };
    expect(parseCanvasVideoResult(full)).toEqual(full);
    expect(parseCanvasVideoResult(JSON.stringify(full))).toEqual(full);

    const parsed = parseCanvasVideoResult({ ok: false, job_id: 'j2', error: '余额不足' });
    expect(parsed.ok).toBe(false);
    expect(parsed.job_id).toBe('j2');
    expect(parsed.path).toBe('');
    expect(parsed.duration).toBe(0);
  });

  it('坏 JSON / 非对象返回全默认值且不抛', () => {
    const empty = {
      ok: false,
      job_id: '',
      source_node_id: '',
      path: '',
      url: '',
      duration: 0,
      model: '',
      error: '',
    };
    expect(parseCanvasVideoResult('{不是 json')).toEqual(empty);
    expect(parseCanvasVideoResult(null)).toEqual(empty);
    expect(parseCanvasVideoResult(42)).toEqual(empty);
  });
});

// 端到端走一遍分发：canvas_video_result 事件 → 解包 → applyVideoResult。
// 事件名/字段名任何一环写错，用户点「生成视频」后节点会永远停在 pending
describe('appStore 分发 canvas_video_result', () => {
  it('成功事件回填 pending 视频节点并清 pending', () => {
    const canvas = useCanvasStore();
    const app = useAppStore();
    const src = canvas.addNode({ type: 'image', x: 100, y: 40, file: SRC_FILE, title: 'a.png' });
    const created = canvas.addVideoJobNode(src.id, 'job-v1', { duration: 5 });
    if (!created) throw new Error('addVideoJobNode 不该失败');

    app.handleBridgeEvent({
      type: 'canvas_video_result',
      data: {
        ok: true,
        job_id: 'job-v1',
        source_node_id: src.id,
        path: VIDEO_FILE,
        url: VIDEO_URL,
        duration: 5,
        model: 'wan3.0-video',
      },
    } as any);

    const node = canvas.nodes.find((n) => n.id === created.id)!;
    expect(node.file).toBe(VIDEO_FILE);
    expect(node.url).toBe(VIDEO_URL);
    expect(canvas.pendingVideos[created.id]).toBeUndefined();
  });

  it('失败事件留痕 meta.error 并退 pending', () => {
    const canvas = useCanvasStore();
    const app = useAppStore();
    const src = canvas.addNode({ type: 'image', file: SRC_FILE });
    const created = canvas.addVideoJobNode(src.id, 'job-v1');
    if (!created) throw new Error('addVideoJobNode 不该失败');

    app.handleBridgeEvent({
      type: 'canvas_video_result',
      data: JSON.stringify({ ok: false, job_id: 'job-v1', error: '云端排队超时' }),
    } as any);

    const node = canvas.nodes.find((n) => n.id === created.id)!;
    expect(node.meta?.error).toBe('云端排队超时');
    expect(canvas.pendingVideos[created.id]).toBeUndefined();
  });
});

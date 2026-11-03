// 画布「角色库与角色带入」（P2）的前端契约。
//
// 钉死的约定：
// 1) 「存为角色参考」成功后节点 meta.character_ids 去重合并（mergeNodeMeta，
//    置 dirty 随画布落盘）；
// 2) 带角色的定妆图结果走 canvas_edit_result 通道：oss_url / character_ids /
//    character_ref_snapshot / final_prompt 全部落进产物节点 meta；
// 3) 带角色的视频结果：受理时遮罩节点先带 character_ids，回填时补
//    character_ref_snapshot / final_prompt / cost_cny / oss_direct；
// 4) 多角色节点在 UI 侧只是数据（ImageNode 提示与入口），拒绝逻辑在后端，
//    前端不做假裁剪——但 meta 口径必须能支撑它。
import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, describe, expect, it } from 'vitest';
import { useCanvasStore } from './canvas';

beforeEach(() => setActivePinia(createPinia()));

const SRC_FILE = 'C:\\tmp\\a.png';
const OUT_FILE = '.ycode/canvas/media/tokenrhythm_edit_20261103_160000.png';

describe('mergeNodeMeta（存为角色参考后的 character_ids 回写）', () => {
  it('只合并传入的键、保留其它 meta 字段、置 dirty', () => {
    const store = useCanvasStore();
    const node = store.addNode({
      type: 'image',
      file: SRC_FILE,
      meta: { created_at: 123, model: 'qwen-image-2.0' },
    });
    store.dirty = false;

    store.mergeNodeMeta(node.id, { character_ids: [7] });

    const updated = store.nodes.find((n) => n.id === node.id)!;
    expect(updated.meta?.character_ids).toEqual([7]);
    // 其它字段原样保留
    expect(updated.meta?.model).toBe('qwen-image-2.0');
    expect(store.dirty).toBe(true);
  });

  it('character_ids 去重合并：同一角色不重复、顺序稳定', () => {
    const store = useCanvasStore();
    const node = store.addNode({ type: 'image', file: SRC_FILE });
    store.mergeNodeMeta(node.id, { character_ids: [7] });
    store.mergeNodeMeta(node.id, { character_ids: [7, 9] });
    expect(store.nodes.find((n) => n.id === node.id)!.meta?.character_ids).toEqual([7, 9]);
  });

  it('节点不存在时静默跳过', () => {
    const store = useCanvasStore();
    expect(() => store.mergeNodeMeta('nope', { character_ids: [1] })).not.toThrow();
  });
});

describe('带角色的定妆图结果（canvas_edit_result 通道）', () => {
  it('oss_url / character_ids / character_ref_snapshot / final_prompt 全落产物节点 meta', () => {
    const store = useCanvasStore();
    const src = store.addNode({ type: 'image', file: SRC_FILE, title: 'a.png' });

    const ok = store.applyEditResult({
      ok: true,
      source_node_id: src.id,
      path: OUT_FILE,
      url: 'http://127.0.0.1:1/media/a.png',
      oss_url: 'https://oss.example/signed.png',
      oss_url_saved_at: 1767000000000,
      character_ids: [7],
      character_ref_snapshot: ['a'.repeat(64)],
      final_prompt: '定妆照\n【角色】小满：黑发\n【画风】写实',
    });

    expect(ok).toBe(true);
    const out = store.nodes.find((n) => n.id !== src.id)!;
    expect(out.meta?.oss_url).toBe('https://oss.example/signed.png');
    expect(out.meta?.oss_url_saved_at).toBe(1767000000000);
    expect(out.meta?.character_ids).toEqual([7]);
    expect(out.meta?.character_ref_snapshot).toEqual(['a'.repeat(64)]);
    expect(out.meta?.final_prompt).toContain('【角色】小满');
  });

  it('普通改图（无新字段）：meta 不长出角色/OSS 字段，行为与 P1 一致', () => {
    const store = useCanvasStore();
    const src = store.addNode({ type: 'image', file: SRC_FILE, title: 'a.png' });

    store.applyEditResult({ ok: true, source_node_id: src.id, path: OUT_FILE });

    const out = store.nodes.find((n) => n.id !== src.id)!;
    expect(out.meta).not.toHaveProperty('character_ids');
    expect(out.meta).not.toHaveProperty('oss_url');
    expect(out.meta).not.toHaveProperty('final_prompt');
  });
});

describe('带角色的视频结果（canvas_video_result 通道）', () => {
  const VIDEO_FILE = '.ycode/canvas/media/tokenrhythm_video_20261103_160500.mp4';

  it('受理时遮罩节点带 character_ids，回填补 snapshot / final_prompt / cost_cny / oss_direct', () => {
    const store = useCanvasStore();
    const src = store.addNode({ type: 'image', file: SRC_FILE, title: 'a.png' });

    const created = store.addVideoJobNode(src.id, 'job-c1', {
      prompt: '推近',
      duration: 5,
      character_ids: [7],
    });
    expect(created!.meta?.character_ids).toEqual([7]);

    const ok = store.applyVideoResult({
      ok: true,
      job_id: 'job-c1',
      source_node_id: src.id,
      path: VIDEO_FILE,
      duration: 5,
      prompt: '推近',
      final_prompt: '推近\n【角色】小满：黑发\n【画风】写实',
      character_ids: [7],
      character_ref_snapshot: ['a'.repeat(64)],
      oss_direct: true,
      cost_cny: 0.6,
    });

    expect(ok).toBe(true);
    const node = store.nodes.find((n) => n.id === created!.id)!;
    expect(node.meta?.character_ids).toEqual([7]);
    expect(node.meta?.character_ref_snapshot).toEqual(['a'.repeat(64)]);
    expect(node.meta?.final_prompt).toContain('【角色】小满');
    expect(node.meta?.prompt).toBe('推近');
    expect(node.meta?.oss_direct).toBe(true);
    expect(node.meta?.cost_cny).toBe(0.6);
  });

  it('无角色字段的视频结果：meta 不长出角色键（P1 行为回归）', () => {
    const store = useCanvasStore();
    const src = store.addNode({ type: 'image', file: SRC_FILE, title: 'a.png' });
    const created = store.addVideoJobNode(src.id, 'job-v1', { duration: 5 });

    store.applyVideoResult({
      ok: true,
      job_id: 'job-v1',
      source_node_id: src.id,
      path: VIDEO_FILE,
      duration: 5,
    });

    const node = store.nodes.find((n) => n.id === created!.id)!;
    expect(node.meta).not.toHaveProperty('character_ids');
    expect(node.meta).not.toHaveProperty('final_prompt');
    expect(node.meta).not.toHaveProperty('cost_cny');
  });
});

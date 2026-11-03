import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { mount } from '@vue/test-utils';
import VideoNode from './VideoNode.vue';
import type { CanvasNode } from '../../stores/canvas';

// Handle 需要 vue-flow 的 NodeId 注入上下文，单测里不提供，直接换成空组件
// （与 ImageNode.click.test.ts 同一套桩）
vi.mock('@vue-flow/core', () => ({
  Handle: { name: 'Handle', render: () => null },
  Position: { Left: 'left', Right: 'right' },
}));

function makeNode(overrides: Partial<CanvasNode> = {}): CanvasNode {
  return {
    id: 'v1',
    type: 'video',
    x: 0,
    y: 0,
    width: 264,
    title: '雨夜街道',
    meta: {},
    ...overrides,
  } as CanvasNode;
}

describe('VideoNode 生成态与播放', () => {
  beforeEach(() => {
    // jsdom 的媒体元素没有实现 play/pause，桩掉后才能断言「点播放真的在播」
    vi.spyOn(HTMLMediaElement.prototype, 'play').mockResolvedValue(undefined);
    vi.spyOn(HTMLMediaElement.prototype, 'pause').mockImplementation(() => undefined);
  });
  afterEach(() => vi.restoreAllMocks());

  it('videoPending=true：盖「生成中」遮罩，不显示播放按钮', () => {
    const w = mount(VideoNode, {
      props: { id: 'v1', videoPending: true, data: { node: makeNode() } },
    });
    expect(w.find('[data-test="canvas-video-pending"]').exists()).toBe(true);
    expect(w.find('[data-test="canvas-video-play"]').exists()).toBe(false);
  });

  it('生成失败（meta.error）：显示失败原因而不是占位播放', () => {
    const w = mount(VideoNode, {
      props: {
        id: 'v1',
        data: { node: makeNode({ meta: { error: '内容审核未通过' } }) },
      },
    });
    expect(w.find('[data-test="canvas-video-error"]').exists()).toBe(true);
    expect(w.find('[data-test="canvas-video-error"]').text()).toContain('内容审核未通过');
  });

  it('有产物（url）：点播放按钮真的调 video.play()', async () => {
    const w = mount(VideoNode, {
      props: {
        id: 'v1',
        data: { node: makeNode({ file: 'a.mp4', url: 'http://127.0.0.1:1/a.mp4' }) },
      },
    });
    expect(w.find('[data-test="canvas-video-pending"]').exists()).toBe(false);
    await w.find('[data-test="canvas-video-play"]').trigger('click');
    // 不再是纯视觉按钮：点击必须触达媒体元素
    expect(HTMLMediaElement.prototype.play).toHaveBeenCalled();
  });
});

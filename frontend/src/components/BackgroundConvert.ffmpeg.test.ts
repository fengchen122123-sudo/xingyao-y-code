import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, expect, it, vi } from 'vitest';
import { flushPromises, mount } from '@vue/test-utils';

import BackgroundConvert from './BackgroundConvert.vue';
import { useAppStore } from '../stores/appStore';

// 前端测试一律 mock window.pywebview.api（无真实 Bridge）
const emptyComponent = {
  installed: false,
  version: '',
  downloading: false,
  percent: 0,
  downloaded_bytes: 0,
  total_bytes: 0,
  error: '',
  stage: '',
  dir: '',
};

const api = {
  get_ffmpeg_component_status: vi.fn(),
  download_ffmpeg_component: vi.fn(),
  cancel_ffmpeg_component_download: vi.fn(),
  describe_background_video: vi.fn(),
};

beforeEach(() => {
  vi.clearAllMocks();
  setActivePinia(createPinia());
  api.get_ffmpeg_component_status.mockResolvedValue({
    ok: true,
    component: { ...emptyComponent },
  });
  api.download_ffmpeg_component.mockResolvedValue({
    ok: true,
    component: { ...emptyComponent, downloading: true, stage: 'downloading' },
  });
  (window as any).pywebview = { api };
});

it('未安装时展示缺失态与下载按钮，点击触发下载并回写 Store 快照', async () => {
  const pinia = createPinia();
  setActivePinia(pinia);
  const wrapper = mount(BackgroundConvert, { global: { plugins: [pinia] } });
  await flushPromises();

  expect(wrapper.find('[data-test="ffmpeg-component-missing"]').exists()).toBe(true);
  expect(wrapper.find('[data-test="ffmpeg-component-progress"]').exists()).toBe(false);

  await wrapper.find('[data-test="ffmpeg-component-download"]').trigger('click');
  await flushPromises();

  expect(api.download_ffmpeg_component).toHaveBeenCalledTimes(1);
  const store = useAppStore();
  expect(store.ffmpegComponent.downloading).toBe(true);
});

it('已就绪展示版本号，进度事件驱动进度条宽度', async () => {
  api.get_ffmpeg_component_status.mockResolvedValue({
    ok: true,
    component: { ...emptyComponent, installed: true, version: '6.1.1' },
  });
  const pinia = createPinia();
  setActivePinia(pinia);
  const store = useAppStore();
  const wrapper = mount(BackgroundConvert, { global: { plugins: [pinia] } });
  await flushPromises();

  const ready = wrapper.find('[data-test="ffmpeg-component-ready"]');
  expect(ready.exists()).toBe(true);
  expect(ready.text()).toContain('6.1.1');

  // 进度事件只更新 Store；组件从 Store 读出来渲染（不做乐观进度）。
  // 这里带 installed=false 模拟「重新下载中」：installed 优先级高于进度条
  store.handleBridgeEvent({
    type: 'ffmpeg_component_progress',
    data: {
      stage: 'downloading',
      percent: 42,
      downloaded_bytes: 1048576,
      total_bytes: 2097152,
      installed: false,
    },
  } as any);
  await flushPromises();

  const bar = wrapper.find('[data-test="ffmpeg-component-progress"] .component-progress-bar');
  expect(bar.attributes('style')).toContain('width: 42%');
});

it('下载完成事件会在已选视频时自动重探预检信息（无需重选视频）', async () => {
  const pinia = createPinia();
  setActivePinia(pinia);
  const store = useAppStore();
  const wrapper = mount(BackgroundConvert, { global: { plugins: [pinia] } });
  await flushPromises();

  store.bgConvert.video = {
    path: 'C:\\clip.mp4',
    name: 'clip.mp4',
    bytes: 1024,
    ffmpeg_available: false,
    max_bytes: 10485760,
    max_dimension: 8192,
    duration: 5,
    width: 1280,
    height: 720,
    can_convert: false,
    blockers: ['未检测到 ffmpeg，无法转换'],
    warnings: [],
  };
  api.describe_background_video.mockResolvedValue({
    ok: true,
    ...store.bgConvert.video,
    ffmpeg_available: true,
    can_convert: true,
    blockers: [],
  });
  api.get_ffmpeg_component_status.mockResolvedValue({
    ok: true,
    component: { ...emptyComponent, installed: true, version: '6.1.1' },
  });

  store.handleBridgeEvent({
    type: 'ffmpeg_component_progress',
    data: { stage: 'done', percent: 100, installed: true, version: '6.1.1' },
  } as any);
  await flushPromises();

  expect(api.describe_background_video).toHaveBeenCalledWith('C:\\clip.mp4');
  expect(store.bgConvert.video?.can_convert).toBe(true);
  expect(store.bgConvert.video?.blockers).toEqual([]);
  // 装好后没有「未检测到 ffmpeg」的旧提示
  expect(wrapper.find('[data-test="ffmpeg-component-error"]').exists()).toBe(false);
});

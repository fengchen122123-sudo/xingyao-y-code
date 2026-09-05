import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, expect, it, vi } from 'vitest';
import { nextTick } from 'vue';
import { mount } from '@vue/test-utils';

import RightPanel from './RightPanel.vue';
import { useAppStore } from '../stores/appStore';

beforeEach(() => {
  setActivePinia(createPinia());
  delete (window as any).pywebview;
});

it('桌面端根元素内联宽度跟随 store.rightPanelWidth，折叠时负 margin 跟随实际宽度', async () => {
  const pinia = createPinia();
  setActivePinia(pinia);
  const store = useAppStore();
  store.rightPanelWidth = 400;
  const wrapper = mount(RightPanel, { global: { plugins: [pinia] } });

  const root = wrapper.find('#rightbar');
  expect(root.attributes('style')).toContain('width: 400px');
  expect(root.attributes('style')).not.toContain('margin-right');

  store.rightPanelCollapsed = true;
  await nextTick();
  expect(root.attributes('style')).toContain('margin-right: -400px');
  wrapper.unmount();
});

it('图片预览：kind=image 时弹窗正文渲染 <img>，文本代码块不出现', async () => {
  const store = useAppStore();
  store.filePreview = {
    show: true,
    path: 'D:\\proj\\pic.png',
    kind: 'image',
    content: '',
    dataUrl: 'data:image/png;base64,iVBORw0KGgo=',
    truncated: false,
    loading: false,
  };
  // 弹窗 Teleport 到 body：stub 掉 Teleport 让内容留在 wrapper 内可查
  const wrapper = mount(RightPanel, { global: { stubs: { teleport: true } } });

  const img = wrapper.find('.file-preview-modal img.fp-image');
  expect(img.exists()).toBe(true);
  expect(img.attributes('src')).toBe('data:image/png;base64,iVBORw0KGgo=');
  // 图片分支下不应渲染文本代码块
  expect(wrapper.find('.file-preview-modal pre.fp-pre').exists()).toBe(false);
  wrapper.unmount();
});

it('文本预览：kind=text 时仍渲染代码块，不出现 <img>', async () => {
  const store = useAppStore();
  store.filePreview = {
    show: true,
    path: 'D:\\proj\\foo.txt',
    kind: 'text',
    content: 'hello',
    dataUrl: '',
    truncated: false,
    loading: false,
  };
  const wrapper = mount(RightPanel, { global: { stubs: { teleport: true } } });

  expect(wrapper.find('.file-preview-modal pre.fp-pre').exists()).toBe(true);
  expect(wrapper.find('.file-preview-modal img.fp-image').exists()).toBe(false);
  wrapper.unmount();
});

// ── 电脑操控“操控”页签（Task 13b，设计 16.2） ──

function tabLabels(wrapper: ReturnType<typeof mount>) {
  return wrapper.findAll('.rtab').map(t => t.text().trim());
}

function computerTab(wrapper: ReturnType<typeof mount>) {
  return wrapper.findAll('.rtab').find(t => t.text().trim() === '操控')!;
}

it('run 出现前不渲染操控页签，出现后动态追加并自动切过去一次', async () => {
  (window as any).pywebview = {
    api: { get_computer_control_status: vi.fn().mockResolvedValue({ run_id: 'run-1', status: 'starting' }) },
  };
  const store = useAppStore();
  const wrapper = mount(RightPanel);

  expect(tabLabels(wrapper)).toEqual(['计划', '任务', '终端', '文件']);

  store.reduceComputerEvent({ type: 'computer_state', run_id: 'run-1', event_seq: 1, status: 'starting' });
  await nextTick();

  expect(tabLabels(wrapper)).toEqual(['计划', '任务', '终端', '文件', '操控']);
  expect(computerTab(wrapper).classes()).toContain('active');
  expect(wrapper.find('[data-test="computer-pane"]').isVisible()).toBe(true);
  expect(store.computerLastAutoOpenedRunId).toBe('run-1');
  wrapper.unmount();
});

it('用户切走后同一 run 的后续事件不抢回页签，新 run 再自动开一次', async () => {
  (window as any).pywebview = {
    api: { get_computer_control_status: vi.fn().mockResolvedValue({ run_id: 'run-1', status: 'running' }) },
  };
  const store = useAppStore();
  const wrapper = mount(RightPanel);

  store.reduceComputerEvent({ type: 'computer_state', run_id: 'run-1', event_seq: 1, status: 'starting' });
  await nextTick();
  expect(computerTab(wrapper).classes()).toContain('active');

  // 用户手动切到“文件”页签
  const filesTab = wrapper.findAll('.rtab').find(t => t.text().trim() === '文件')!;
  await filesTab.trigger('click');
  expect(computerTab(wrapper).classes()).not.toContain('active');

  // 同一 run 的后续事件（observation/finished）不把页签抢回来
  store.reduceComputerEvent({ type: 'computer_observation', run_id: 'run-1', event_seq: 2, action_ordinal: 1, preview_url: '/computer-preview/t/1.png', origin_or_window: 'example.com' });
  store.reduceComputerEvent({ type: 'computer_action_finished', run_id: 'run-1', event_seq: 3, action_ordinal: 1, action_kind: 'click', ok: true, error_code: '' });
  await nextTick();
  expect(computerTab(wrapper).classes()).not.toContain('active');
  // run 结束也保留在当前页签
  store.reduceComputerEvent({ type: 'computer_state', run_id: 'run-1', event_seq: 4, status: 'stopped' });
  await nextTick();
  expect(computerTab(wrapper).classes()).not.toContain('active');

  // 新 run 开始：再自动切到操控页签一次
  store.reduceComputerEvent({ type: 'computer_state', run_id: 'run-2', event_seq: 1, status: 'starting' });
  await nextTick();
  expect(computerTab(wrapper).classes()).toContain('active');
  expect(store.computerLastAutoOpenedRunId).toBe('run-2');
  wrapper.unmount();
});

it('手动切到操控页签时拉取快照校准 run 状态', async () => {
  const getStatus = vi.fn().mockResolvedValue({ run_id: 'run-1', status: 'paused', pause_reason: 'user_pause' });
  (window as any).pywebview = { api: { get_computer_control_status: getStatus } };
  const store = useAppStore();
  const wrapper = mount(RightPanel);

  // run 结束态（无自动开页签），用户手动点开操控页签
  store.reduceComputerEvent({ type: 'computer_state', run_id: 'run-1', event_seq: 1, status: 'stopped', stop_reason: 'user_stop' });
  await nextTick();
  expect(computerTab(wrapper).classes()).not.toContain('active');
  await computerTab(wrapper).trigger('click');

  expect(getStatus).toHaveBeenCalledWith();
  await vi.waitFor(() => expect(store.computerRun.status).toBe('paused'));
  expect(store.computerRun.pause_reason).toBe('user_pause');
  wrapper.unmount();
});

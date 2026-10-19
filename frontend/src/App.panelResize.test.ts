import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, expect, it, vi } from 'vitest';
import { mount } from '@vue/test-utils';

import App from './App.vue';
import { useAppStore } from './stores/appStore';

// 子组件全部 stub：手柄渲染与拖拽接线都在 App 模板内，不依赖子组件行为
const stubs = {
  Topbar: true, Sidebar: true, KimiEmptyState: true, TurnGroup: true,
  InputArea: true, RightPanel: true, ConfirmModal: true, UpdateModal: true,
  PermCard: true, ImageLightbox: true, Subpage: true, SettingsPage: true,
  AppIcon: true, MessageNavRail: true, RemoteDirBrowser: true,
};

beforeEach(() => {
  localStorage.clear();
  setActivePinia(createPinia());
  delete (window as any).pywebview;
});

// jsdom 无 matchMedia → useIsMobile 恒为 false，即桌面端行为
it('桌面端渲染左右两个面板拖拽手柄', () => {
  const store = useAppStore();
  store.initialized = true;
  // 右侧面板默认收起（appStore.rightPanelDefault 测试锁定）；手柄渲染属于展开态行为
  store.rightPanelCollapsed = false;
  const wrapper = mount(App, { global: { stubs } });

  expect(wrapper.find('[data-test="sidebar-resizer"]').exists()).toBe(true);
  expect(wrapper.find('[data-test="right-panel-resizer"]').exists()).toBe(true);
  wrapper.unmount();
});

it('面板折叠时对应手柄隐藏，展开后恢复', async () => {
  const store = useAppStore();
  store.initialized = true;
  store.sidebarCollapsed = true;
  store.rightPanelCollapsed = true;
  const wrapper = mount(App, { global: { stubs } });

  expect(wrapper.find('[data-test="sidebar-resizer"]').exists()).toBe(false);
  expect(wrapper.find('[data-test="right-panel-resizer"]').exists()).toBe(false);

  store.sidebarCollapsed = false;
  store.rightPanelCollapsed = false;
  await wrapper.vm.$nextTick();
  expect(wrapper.find('[data-test="sidebar-resizer"]').exists()).toBe(true);
  expect(wrapper.find('[data-test="right-panel-resizer"]').exists()).toBe(true);
  wrapper.unmount();
});

// 右侧面板必须走 right-panel-slide 过场：v-show 单独使用时 display: none 与
// margin 在同一帧切换，负 margin 的过渡来不及生效，面板会"直接出现"
it('右侧面板套在 right-panel-slide 过场里', () => {
  const store = useAppStore();
  store.initialized = true;
  const wrapper = mount(App, { global: { stubs } });

  const slides = wrapper
    .findAllComponents({ name: 'Transition' })
    .filter(item => item.props('name') === 'right-panel-slide');
  expect(slides).toHaveLength(1);
  wrapper.unmount();
});

// ── 电脑操控安全条接线（Task 13b，设计 16.1 / 16.4） ──

it('电脑操控安全条渲染在聊天标题栏下方，idle 时不渲染', async () => {
  const store = useAppStore();
  store.initialized = true;
  const wrapper = mount(App, { global: { stubs } });

  // idle run：安全条不渲染
  expect(wrapper.find('[data-test="computer-control-bar"]').exists()).toBe(false);

  store.computerRun = { ...store.computerRun, run_id: 'run-1', status: 'running' };
  await wrapper.vm.$nextTick();
  const header = wrapper.find('#chat-header').element;
  const bar = wrapper.find('[data-test="computer-control-bar"]').element;
  expect(bar).toBeTruthy();
  // 插在聊天标题栏下方（标题栏之后、滚动区之前，滚动时保持可见）
  expect((header.compareDocumentPosition(bar) & Node.DOCUMENT_POSITION_FOLLOWING) !== 0).toBe(true);
  const viewport = wrapper.find('.chat-viewport').element;
  expect((bar.compareDocumentPosition(viewport) & Node.DOCUMENT_POSITION_FOLLOWING) !== 0).toBe(true);
  wrapper.unmount();
});

it('窗口重新聚焦时拉取快照校准电脑操控 run', async () => {
  const getStatus = vi.fn().mockResolvedValue({ run_id: 'run-1', status: 'stopped', stop_reason: 'hotkey' });
  (window as any).pywebview = { api: { get_computer_control_status: getStatus } };
  const store = useAppStore();
  store.initialized = true;
  store.computerRun = { ...store.computerRun, run_id: 'run-1', status: 'running' };
  const wrapper = mount(App, { global: { stubs } });

  window.dispatchEvent(new Event('focus'));

  expect(getStatus).toHaveBeenCalledWith();
  await vi.waitFor(() => expect(store.computerRun.status).toBe('stopped'));
  expect(store.computerRun.stop_reason).toBe('hotkey');
  wrapper.unmount();
});

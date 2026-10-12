import { mount } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, expect, it, vi } from 'vitest';

import App from './App.vue';
import { useAppStore } from './stores/appStore';

/**
 * IDE 模式的「外壳」接线：模式切换时哪些全局侧栏必须退场、对话头部该长出什么。
 *
 * 这些断言只能在 App 级做——它们跨组件（Sidebar / IdeLayout / 对话头部同属 App 模板），
 * 组件各自的用例看不到彼此。第一版改版把 Sidebar 和 RightPanel 留在了 IDE 模式里，
 * 编辑器区被挤成一条缝，就是这个接线漏了；本用例专门守住这条线。
 */

// Sidebar / IdeLayout 用带锚点的轻量 stub：本用例只关心「它们在不在」，
// 不关心它们内部渲染（各自有专门用例）。其余子组件照常 stub 掉。
const stubs = {
  Topbar: true, KimiEmptyState: true, TurnGroup: true, InputArea: true,
  ConfirmModal: true, UpdateModal: true, PermCard: true, ImageLightbox: true,
  Subpage: true, SettingsPage: true, AppIcon: true, MessageNavRail: true,
  RemoteDirBrowser: true, TerminalPane: true, BackgroundTasksBar: true,
  teleport: true,
  Sidebar: { template: '<div data-test="sidebar-stub" />' },
  IdeLayout: { template: '<div data-test="ide-layout-stub" />' },
  RightPanel: { template: '<div data-test="right-panel-stub" />' },
};

/** pywebview 桥 stub：必须在 mount() 之前装好，App 挂载会立刻用到桥 */
function installBridge() {
  (window as any).pywebview = {
    api: {
      ide_save_layout: vi.fn(async () => ({ ok: true })),
      save_session_ui: vi.fn(async () => ({ ok: true, ui: { version: 1, ide_mode: false } })),
      get_session_ui: vi.fn(async () => ({ ok: true, ui: { version: 1, ide_mode: false } })),
      get_file_tree: vi.fn(async () => ({ ok: true, tree: [] })),
    },
  };
}

beforeEach(() => {
  localStorage.clear();
  setActivePinia(createPinia());
  installBridge();
});

function mountApp() {
  const store = useAppStore();
  store.initialized = true;
  const wrapper = mount(App, { global: { stubs } });
  return { store, wrapper };
}

it('对话模式：会话侧栏与控制面板在，对话头部不带星瑶头像与会话操作', () => {
  const { wrapper } = mountApp();

  expect(wrapper.find('[data-test="sidebar-stub"]').exists()).toBe(true);
  expect(wrapper.find('[data-test="ide-layout-stub"]').exists()).toBe(false);
  expect(wrapper.find('[data-test="ai-head-avatar"]').exists()).toBe(false);
  expect(wrapper.find('[data-test="ide-chat-new-session"]').exists()).toBe(false);
  expect(wrapper.get('.crumb').text()).toContain('会话 /');

  wrapper.unmount();
});

it('IDE 模式：会话侧栏与控制面板退场，对话头部补上星瑶头像与会话操作', async () => {
  const { store, wrapper } = mountApp();

  store.toggleIdeMode();
  await wrapper.vm.$nextTick();

  // 侧栏退场是本次修复的核心：它按固定宽度占左边，不退场就把编辑器区挤没
  expect(wrapper.find('[data-test="sidebar-stub"]').exists()).toBe(false);
  expect(wrapper.find('[data-test="ide-layout-stub"]').exists()).toBe(true);
  // 控制面板用 v-show 隐藏而不是 v-if 卸载：卸载会重建组件，它内部的原生 WebView2
  // 表面来不及 hide 会以白块留在屏幕上。所以这里断言「还在 DOM 里但不可见」，
  // 顺便把「不许改回 v-if」这条约束钉住
  expect(wrapper.get('[data-test="right-panel-stub"]').attributes('style') ?? '').toContain('display: none');
  // 它两侧的拖拽热区是纯 HTML 元素，没有原生表面问题，继续用 v-if 摘掉即可
  expect(wrapper.find('[data-test="right-panel-resizer"]').exists()).toBe(false);
  expect(wrapper.find('[data-test="sidebar-resizer"]').exists()).toBe(false);

  // 对话头部：头像 + 会话名（不再重复「会话 /」前缀）+ 三个会话操作
  expect(wrapper.find('[data-test="ai-head-avatar"]').exists()).toBe(true);
  expect(wrapper.get('.crumb').text()).toBe(store.currentSessionDisplayName);
  expect(wrapper.find('[data-test="ide-chat-new-session"]').exists()).toBe(true);
  expect(wrapper.find('[data-test="ide-chat-history"]').exists()).toBe(true);

  // 对话区靠 CSS 停靠到 #main 右侧，不再用 Teleport 搬 DOM——Teleport 只在挂载时
  // 解析一次目标，而宿主在 v-if 的 IdeLayout 里：对话模式下首次挂载时目标不存在，
  // target 解析成 null，之后切到 IDE 模式也不会重新解析，对话区就会留在原地按
  // flex:1 与编辑器抢宽度（实测 #center 705px，368px 的宿主一直空置）
  const center = wrapper.get('#center');
  expect(center.classes()).toContain('ide-docked');
  expect(center.classes()).not.toContain('ide-docked-hidden');

  wrapper.unmount();
});

it('IDE 模式：收起按钮置共享状态（IdeLayout 侧读同一字段隐藏宿主）', async () => {
  const { store, wrapper } = mountApp();

  store.toggleIdeMode();
  await wrapper.vm.$nextTick();
  expect(store.ideChatCollapsed).toBe(false);

  await wrapper.get('[data-test="ide-chat-collapse"]').trigger('click');
  expect(store.ideChatCollapsed).toBe(true);
  // 收起后对话区整块让位（display:none），编辑器区自然全屏
  expect(wrapper.get('#center').classes()).toContain('ide-docked-hidden');

  wrapper.unmount();
});

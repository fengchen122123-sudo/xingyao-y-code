import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, expect, it } from 'vitest';
import { mount } from '@vue/test-utils';

import App from './App.vue';
import { useAppStore } from './stores/appStore';
import { useGitStore } from './stores/gitStore';

// 子组件全部 stub：只断言对话标题栏自身的按钮，不依赖子组件行为
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

it('对话标题栏不再渲染「清空」按钮，导出入口保留', () => {
  const store = useAppStore();
  store.initialized = true;
  const wrapper = mount(App, { global: { stubs } });

  const labels = wrapper.findAll('#chat-header .chip-btn').map((b) => b.text());

  expect(labels).not.toContain('清空');
  expect(labels.some((t) => t.includes('导出'))).toBe(true);
  wrapper.unmount();
});

it('对话标题栏渲染 Git 分支 chip，点击展开 Git 面板', async () => {
  const store = useAppStore();
  store.initialized = true;
  const gitStore = useGitStore();
  gitStore.snapshot = {
    branch_name: 'dev', head_state: 'clean', head_oid: 'abcd1234',
    ahead: 0, behind: 0, staged: [], unstaged: [], untracked: [],
    conflicts: [], operation_kind: 'idle',
  } as any;

  const wrapper = mount(App, { global: { stubs } });
  const chip = wrapper.get('[data-test="chat-header-git-branch"]');
  // 复用状态栏文案（去 ⎇ 前缀）：分支名 + 脏标记
  expect(chip.text()).toContain('dev');

  store.rightPanelCollapsed = true;
  const nonceBefore = gitStore.panelOpenNonce;
  await chip.trigger('click');
  expect(gitStore.panelOpenNonce).toBe(nonceBefore + 1);
  expect(store.rightPanelCollapsed).toBe(false);
  wrapper.unmount();
});

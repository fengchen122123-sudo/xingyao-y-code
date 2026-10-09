import { mount } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import { nextTick } from 'vue';
import { beforeEach, expect, it, vi } from 'vitest';

import Topbar from './Topbar.vue';
import { useAppStore } from '../stores/appStore';

// pywebview 桥必须在 mount 之前装好：Topbar 挂载时读 window_is_maximized，
// toggleIdeMode 之后还会走 save_session_ui 落盘（缺桥会进降级分支刷噪音日志）。
// 写法与 IdeLayout.test.ts 的 installBridge 一致。
function installBridge() {
  (window as any).pywebview = {
    api: {
      window_is_maximized: vi.fn(async () => ({ ok: true, maximized: false })),
      save_session_ui: vi.fn(async () => ({ ok: true, ui: { version: 1, ide_mode: false } })),
    },
  };
}

// 当前会话名 / 模式都可能从 localStorage 恢复，用例之间必须清干净，
// 否则第二个用例一挂载就是 IDE 模式，断言全部失真。
beforeEach(() => {
  setActivePinia(createPinia());
  localStorage.clear();
  installBridge();
});

it('对话模式下按钮显示 IDE，且不带激活类', () => {
  const wrapper = mount(Topbar);

  const btn = wrapper.get('[data-test="btn-ide-mode"]');
  expect(btn.text()).toBe('IDE');
  expect(btn.classes()).not.toContain('is-active');
  // title 是"点下去会发生什么"，所以对话模式里应提示切到 IDE
  expect(btn.attributes('title')).toBe('切换到 IDE 模式');
  wrapper.unmount();
});

it('切到 IDE 模式后按钮变强调色胶囊激活态：文案「对话」+ title 提示切回', async () => {
  const wrapper = mount(Topbar);
  const store = useAppStore();

  store.toggleIdeMode();
  await nextTick();

  const btn = wrapper.get('[data-test="btn-ide-mode"]');
  expect(btn.classes()).toContain('is-active');
  expect(btn.text()).toBe('对话');
  expect(btn.attributes('title')).toBe('切换到对话模式');
  wrapper.unmount();
});

it('再切一次回到对话模式：激活类与文案一起还原', async () => {
  const wrapper = mount(Topbar);
  const store = useAppStore();

  store.toggleIdeMode();
  await nextTick();
  store.toggleIdeMode();
  await nextTick();

  const btn = wrapper.get('[data-test="btn-ide-mode"]');
  expect(store.ideMode()).toBe(false);
  expect(btn.classes()).not.toContain('is-active');
  expect(btn.text()).toBe('IDE');
  expect(btn.attributes('title')).toBe('切换到 IDE 模式');
  wrapper.unmount();
});

it('点击按钮等价于 toggleIdeMode：一次点击即进入 IDE 模式', async () => {
  const wrapper = mount(Topbar);
  const store = useAppStore();

  await wrapper.get('[data-test="btn-ide-mode"]').trigger('click');

  expect(store.ideMode()).toBe(true);
  expect(wrapper.get('[data-test="btn-ide-mode"]').classes()).toContain('is-active');
  wrapper.unmount();
});

import { beforeEach, expect, it, vi } from 'vitest';
import { createPinia, setActivePinia } from 'pinia';
import { nextTick } from 'vue';
import { mount } from '@vue/test-utils';

// jsdom 下真 xterm 起不来，且本组用例只关心页签与面板的挂载关系
vi.mock('./TerminalPane.vue', () => ({
  default: { name: 'TerminalPane', template: '<div class="terminal-stub" />' },
}));

import BottomPanel from './BottomPanel.vue';
import { useIdeStore } from '../stores/ideStore';
import { emitDapOutput } from '../utils/dapBus';

beforeEach(() => {
  setActivePinia(createPinia());
  (window as any).pywebview = { api: {} };
});

it('lists the debug console tab and switches to it', async () => {
  const ide = useIdeStore();
  const wrapper = mount(BottomPanel);

  const tab = wrapper.get('[data-test="bottom-tab-debug"]');
  expect(tab.text()).toContain('调试控制台');

  await tab.trigger('click');

  expect(ide.bottomTab).toBe('debug');
  wrapper.unmount();
});

it('keeps the debug pane mounted while another tab is active', async () => {
  const ide = useIdeStore();
  ide.setBottomTab('terminal');
  const wrapper = mount(BottomPanel);

  emitDapOutput({ session_id: 'dap-1', category: 'stdout', text: 'kept while hidden\n' });

  // v-show 常驻挂载：切到别的页签只是隐藏（v-if 会连本地行缓冲一起销毁）
  const pane = wrapper.find('[data-test="debug-pane"]');
  expect(pane.exists()).toBe(true);
  expect(pane.isVisible()).toBe(false);
  wrapper.unmount();
});

it('shows the debug pane when the tab is active', async () => {
  const ide = useIdeStore();
  ide.setBottomTab('debug');
  const wrapper = mount(BottomPanel);

  expect(wrapper.get('[data-test="debug-pane"]').isVisible()).toBe(true);
  wrapper.unmount();
});

it('在终端与调试控制台之间来回切，控制台输出保留', async () => {
  const ide = useIdeStore();
  ide.setBottomTab('debug');
  const wrapper = mount(BottomPanel);

  emitDapOutput({ session_id: 'dap-1', category: 'stdout', text: 'kept\n' });
  await nextTick();

  await wrapper.get('[data-test="bottom-tab-terminal"]').trigger('click');
  await nextTick();
  await wrapper.get('[data-test="bottom-tab-debug"]').trigger('click');
  await nextTick();

  expect(wrapper.get('[data-test="debug-console-output"]').text()).toContain('kept');
  expect(ide.bottomTab).toBe('debug');
  wrapper.unmount();
});

it('调试控制台页签不再显示「S3 接入后显示」占位文案', async () => {
  const ide = useIdeStore();
  ide.setBottomTab('debug');
  const wrapper = mount(BottomPanel);

  expect(wrapper.text()).not.toContain('调试控制台将在 S3 接入后显示');
  wrapper.unmount();
});

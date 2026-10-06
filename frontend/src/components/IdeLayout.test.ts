import { mount } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, expect, it, vi } from 'vitest';

vi.mock('./EditorPane.vue', () => ({ default: { name: 'EditorPane', template: '<div class="editor-pane-stub" />' } }));
vi.mock('./TerminalPane.vue', () => ({ default: { name: 'TerminalPane', template: '<div class="terminal-stub" />' } }));

import IdeLayout from './IdeLayout.vue';
import { useIdeStore } from '../stores/ideStore';

beforeEach(() => {
  setActivePinia(createPinia());
  (window as any).pywebview = {
    api: {
      ide_read_file: vi.fn(async (path: string) => ({
        ok: true, path, content: 'x = 1\n', encoding: 'utf-8', bom: false, eol: 'lf', hash: 'h1',
      })),
      ide_set_open_files: vi.fn(async () => ({ ok: true, count: 1 })),
      ide_write_file: vi.fn(async () => ({ ok: true, hash: 'h2' })),
    },
  };
});

it('没有打开标签时显示空状态，不挂载编辑器', () => {
  const wrapper = mount(IdeLayout);
  expect(wrapper.find('.editor-pane-stub').exists()).toBe(false);
  expect(wrapper.text()).toContain('打开文件');
  wrapper.unmount();
});

it('打开文件后挂载编辑器，底部面板可切换到终端', async () => {
  const store = useIdeStore();
  await store.openFile('a.py');
  const wrapper = mount(IdeLayout);

  expect(wrapper.find('.editor-pane-stub').exists()).toBe(true);
  expect(wrapper.text()).toContain('终端');

  await wrapper.find('.bottom-tab.terminal').trigger('click');
  expect(wrapper.find('.terminal-stub').exists()).toBe(true);
  wrapper.unmount();
});

import { mount } from '@vue/test-utils';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, expect, it, vi } from 'vitest';

import EditorTabs from './EditorTabs.vue';
import editorTabsSource from './EditorTabs.vue?raw';
import { useIdeStore } from '../stores/ideStore';

// vitest 默认 css: false 会把 `../style.css?raw` 清成空串（jsdom 下 import.meta.url
// 也不是 file:// ），这里按 frontend 工作目录直接读原文，好把
// 「组件只复用既有主题变量」这条钉死。
const styleCss = readFileSync(resolve(process.cwd(), 'src/style.css'), 'utf8');

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

it('渲染已打开文件的文件名，脏标签带圆点', async () => {
  const store = useIdeStore();
  await store.openFile('E:\\p\\a.py');
  await store.openFile('E:\\p\\b.ts');
  store.updateContent('E:\\p\\b.ts', 'y');

  const wrapper = mount(EditorTabs);
  expect(wrapper.text()).toContain('a.py');
  expect(wrapper.text()).toContain('b.ts');
  expect(wrapper.findAll('.tab-dot').length).toBe(1);
  // 脏标记只在脏的那个标签上渲染
  expect(wrapper.findAll('.tab')[1].find('.tab-dot').exists()).toBe(true);
  expect(wrapper.findAll('.tab')[0].find('.tab-dot').exists()).toBe(false);
  wrapper.unmount();
});

it('点击标签切换活动文件', async () => {
  const store = useIdeStore();
  await store.openFile('a.py');
  await store.openFile('b.py');
  const wrapper = mount(EditorTabs);

  await wrapper.findAll('.tab')[0].trigger('click');
  expect(store.activePath()).toBe('a.py');
  wrapper.unmount();
});

it('点击关闭按钮移除标签且不触发切换', async () => {
  const store = useIdeStore();
  await store.openFile('a.py');
  await store.openFile('b.py');
  const wrapper = mount(EditorTabs);

  await wrapper.findAll('.tab-close')[0].trigger('click');
  expect(store.tabs().map((t) => t.path)).toEqual(['b.py']);
  // 关闭 a.py 不得顺带把活动标签切到 a.py（关闭按钮 stop 冒泡）
  expect(store.activePath()).toBe('b.py');
  wrapper.unmount();
});

it('关闭活动标签只走关闭逻辑，不因冒泡误触发切换', async () => {
  const store = useIdeStore();
  await store.openFile('a.py');
  await store.openFile('b.py');
  store.activate('a.py');
  const wrapper = mount(EditorTabs);

  const first = wrapper.findAll('.tab')[0];
  expect(first.classes()).toContain('active');
  await first.find('.tab-close').trigger('click');

  expect(store.tabs().map((t) => t.path)).toEqual(['b.py']);
  // 若关闭的点击冒泡到标签本身，activate('a.py') 会把活动路径写回已关闭的 a.py
  expect(store.activePath()).toBe('b.py');
  wrapper.unmount();
});

it('活动标签用 samePath 口径判定：分隔符写法不同也认作同一个标签（不再是裸 === 比较）', async () => {
  const store = useIdeStore();
  await store.openFile('E:\\p\\a.py');
  await store.openFile('E:\\p\\b.py');
  // 活动路径以另一种分隔符写法落进 state（跳转定义 / 布局恢复都可能带进这种写法）：
  // 裸 `tab.path === store.activePath()` 会把活动标签判成非活动，标签条就不高亮了
  store.activeBySession[store.session] = 'E:/p/b.py';

  const wrapper = mount(EditorTabs);
  const active = wrapper.find('.tab.active');
  expect(active.exists()).toBe(true);
  expect(active.text()).toContain('b.py');
  wrapper.unmount();

  // 口径锁死：模板里不得再出现裸路径比较
  expect(editorTabsSource).not.toContain('tab.path === store.activePath()');
  expect(editorTabsSource).toContain('samePath(tab.path, store.activePath())');
});

it('活动标签用既有主题变量着色，不引入新变量', async () => {
  const store = useIdeStore();
  await store.openFile('a.py');
  await store.openFile('b.py');
  const wrapper = mount(EditorTabs);

  const active = wrapper.find('.tab.active');
  expect(active.exists()).toBe(true);
  expect(active.text()).toContain('b.py');

  const used = [...new Set([...editorTabsSource.matchAll(/var\(\s*(--[a-z0-9-]+)/gi)].map((m) => m[1]))];
  expect(used.length).toBeGreaterThan(0);
  // 用到的每个变量都必须在 style.css 里真实声明过（不许自造变量名）
  for (const name of used) expect(styleCss).toContain(`${name}:`);
  // 活动标签的高亮色走既有 --accent 变量
  expect(editorTabsSource).toMatch(/\.tab\.active\s*\{[^}]*var\(--accent\)/);
  wrapper.unmount();
});

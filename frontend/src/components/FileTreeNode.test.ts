import { mount } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, expect, it } from 'vitest';

import FileTreeNode from './FileTreeNode.vue';
import { useAppStore } from '../stores/appStore';
import type { FileNode } from '../types/pywebview';

beforeEach(() => setActivePinia(createPinia()));

function mountFile(name: string, parentPath: string) {
  const store = useAppStore();
  const panelCalls: string[] = [];
  const previewCalls: string[] = [];
  store.openInRightPanel = (async (p: string) => { panelCalls.push(p); }) as any;
  store.openFilePreview = (async (p: string) => { previewCalls.push(p); }) as any;
  const node = { n: name, d: 0 } as FileNode;
  const wrapper = mount(FileTreeNode, { props: { node, parentPath } });
  return { wrapper, panelCalls, previewCalls };
}

it('文件树里的 html 点击走右栏内嵌浏览器（不是源码预览弹窗）', async () => {
  const { wrapper, panelCalls, previewCalls } = mountFile('index.html', 'D:\\proj');

  await wrapper.get('.file-row').trigger('click');

  expect(panelCalls).toEqual(['D:\\proj\\index.html']);
  expect(previewCalls).toEqual([]);
  wrapper.unmount();
});

it('文件树里的 pdf 点击走右栏（WebView2 内置 PDF 查看器）', async () => {
  const { wrapper, panelCalls, previewCalls } = mountFile('doc.pdf', 'D:\\proj\\docs');

  await wrapper.get('.file-row').trigger('click');

  expect(panelCalls).toEqual(['D:\\proj\\docs\\doc.pdf']);
  expect(previewCalls).toEqual([]);
  wrapper.unmount();
});

it('文件树里的 md 点击走右栏（渲染成阅读样式）', async () => {
  const { wrapper, panelCalls, previewCalls } = mountFile('readme.md', 'D:\\proj');

  await wrapper.get('.file-row').trigger('click');

  expect(panelCalls).toEqual(['D:\\proj\\readme.md']);
  expect(previewCalls).toEqual([]);
  wrapper.unmount();
});

it('文件树里的其它类型点击仍走源码预览弹窗', async () => {
  const { wrapper, panelCalls, previewCalls } = mountFile('app.py', 'D:\\proj');

  await wrapper.get('.file-row').trigger('click');

  expect(previewCalls).toEqual(['D:\\proj\\app.py']);
  expect(panelCalls).toEqual([]);
  wrapper.unmount();
});

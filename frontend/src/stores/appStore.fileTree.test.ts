import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { createPinia, setActivePinia } from 'pinia';

import { useAppStore } from './appStore';


beforeEach(() => setActivePinia(createPinia()));

afterEach(() => {
  (window as any).pywebview = undefined;
  vi.restoreAllMocks();
});

it('refreshFileTree 重拉整树并恢复展开目录的子节点，已删除目录从展开记录清除', async () => {
  const getFileTree = vi.fn().mockResolvedValue({
    ok: true,
    file_tree: [
      { n: 'new.txt', d: 0 },
      { n: 'src', d: 1 },
    ],
  });
  const getDirChildren = vi.fn().mockResolvedValue({
    children: [{ n: 'Main.java', d: 0 }],
  });
  (window as any).pywebview = {
    api: { get_file_tree: getFileTree, get_dir_children: getDirChildren },
  };
  const store = useAppStore();
  store.workdir = 'E:\\projects\\demo';
  store.fileTree = [
    { n: 'src', d: 1, children: [{ n: 'Old.java', d: 0 }] },
    { n: 'old_dir', d: 1, children: [] },
  ];
  store.fileTreeExpanded = ['E:\\projects\\demo\\src', 'E:\\projects\\demo\\old_dir'];

  await store.refreshFileTree();

  expect(store.fileTree.map(n => n.n)).toEqual(['new.txt', 'src']);
  expect(getDirChildren).toHaveBeenCalledWith('E:\\projects\\demo\\src');
  const src = store.fileTree.find(n => n.n === 'src')!;
  expect(src.children).toEqual([{ n: 'Main.java', d: 0 }]);
  expect(store.fileTreeExpanded).toEqual(['E:\\projects\\demo\\src']);
  expect(store.fileTreeRefreshing).toBe(false);
});

it('refreshFileTree 在没有工作区或刷新进行中时不发请求', async () => {
  const getFileTree = vi.fn();
  (window as any).pywebview = { api: { get_file_tree: getFileTree } };
  const store = useAppStore();

  store.workdir = '';
  await store.refreshFileTree();
  expect(getFileTree).not.toHaveBeenCalled();

  store.workdir = 'E:\\projects\\demo';
  store.fileTreeRefreshing = true;
  await store.refreshFileTree();
  expect(getFileTree).not.toHaveBeenCalled();
});

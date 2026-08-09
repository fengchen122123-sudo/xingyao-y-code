import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { createPinia, setActivePinia } from 'pinia';

import { useAppStore } from './appStore';


beforeEach(() => setActivePinia(createPinia()));

afterEach(() => {
  (window as any).pywebview = undefined;
  vi.restoreAllMocks();
});

it('loads files when entering a pending project conversation', async () => {
  const getWorkspaceState = vi.fn().mockResolvedValue({
    ok: true,
    workdir: 'E:\\projects\\demo',
    file_tree: [{ n: 'README.md', d: 0 }],
  });
  (window as any).pywebview = {
    api: { get_workspace_state: getWorkspaceState },
  };
  const store = useAppStore();

  await store.createSessionInGroup('E:\\projects\\demo');

  expect(getWorkspaceState).toHaveBeenCalledWith('E:\\projects\\demo');
  expect(store.pendingWorkdir).toBe('E:\\projects\\demo');
  expect(store.workdir).toBe('E:\\projects\\demo');
  expect(store.currentWorkdirLabel).toBe('demo');
  expect(store.fileTree).toEqual([{ n: 'README.md', d: 0 }]);
  expect(store.currentSessionName).toBe('');
});

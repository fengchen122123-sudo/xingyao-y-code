import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { createPinia, setActivePinia } from 'pinia';

import { useAppStore } from './appStore';


beforeEach(() => setActivePinia(createPinia()));

afterEach(() => {
  (window as any).pywebview = undefined;
  vi.restoreAllMocks();
});

it('openFilePreview 收到图片响应时按 image 类型填充 dataUrl', async () => {
  const readFilePreview = vi.fn().mockResolvedValue({
    ok: true,
    kind: 'image',
    data_url: 'data:image/png;base64,iVBORw0KGgo=',
    path: 'D:\\proj\\pic.png',
  });
  (window as any).pywebview = { api: { read_file_preview: readFilePreview } };
  const store = useAppStore();

  await store.openFilePreview('D:\\proj\\pic.png');

  expect(store.filePreview.show).toBe(true);
  expect(store.filePreview.kind).toBe('image');
  expect(store.filePreview.dataUrl).toBe('data:image/png;base64,iVBORw0KGgo=');
  expect(store.filePreview.loading).toBe(false);
});

it('openFilePreview 收到文本响应时 kind 为 text 且无 dataUrl', async () => {
  const readFilePreview = vi.fn().mockResolvedValue({
    ok: true,
    content: 'hello',
    truncated: false,
    path: 'D:\\proj\\foo.md',
  });
  (window as any).pywebview = { api: { read_file_preview: readFilePreview } };
  const store = useAppStore();

  await store.openFilePreview('D:\\proj\\foo.md');

  expect(store.filePreview.show).toBe(true);
  expect(store.filePreview.kind).toBe('text');
  expect(store.filePreview.dataUrl).toBe('');
  expect(store.filePreview.content).toBe('hello');
});

it('closeFilePreview 清空 dataUrl 释放 base64 内存', async () => {
  const readFilePreview = vi.fn().mockResolvedValue({
    ok: true,
    kind: 'image',
    data_url: 'data:image/png;base64,iVBORw0KGgo=',
    path: 'D:\\proj\\pic.png',
  });
  (window as any).pywebview = { api: { read_file_preview: readFilePreview } };
  const store = useAppStore();

  await store.openFilePreview('D:\\proj\\pic.png');
  store.closeFilePreview();

  expect(store.filePreview.show).toBe(false);
  expect(store.filePreview.dataUrl).toBe('');
});

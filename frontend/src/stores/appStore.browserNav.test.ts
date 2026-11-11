import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { createPinia, setActivePinia } from 'pinia';

import { useAppStore } from './appStore';

beforeEach(() => setActivePinia(createPinia()));

afterEach(() => {
  (window as any).pywebview = undefined;
  vi.restoreAllMocks();
});

it('openLocalFileInBrowser 写导航请求、nonce 自增并摊开浏览器页签', () => {
  const store = useAppStore();
  store.rightPanelCollapsed = true;

  store.openLocalFileInBrowser('D:\\proj\\index.html');

  expect(store.browserNavRequest).toEqual({
    url: 'file:///D:/proj/index.html',
    path: 'D:\\proj\\index.html',
  });
  expect(store.browserNavNonce).toBe(1);
  expect(store.rightPanelTabs).toContain('browser');
  expect(store.rightPanelActiveTab).toBe('browser');
  expect(store.rightPanelCollapsed).toBe(false);
});

it('同一文件重复点击 nonce 继续自增（组件侧 watch 才会重复触发）', () => {
  const store = useAppStore();
  store.openLocalFileInBrowser('D:\\proj\\index.html');
  store.openLocalFileInBrowser('D:\\proj\\index.html');

  expect(store.browserNavNonce).toBe(2);
  expect(store.browserNavRequest?.url).toBe('file:///D:/proj/index.html');
});

it('中文与空格路径按段百分号编码（与后端 quote(safe="") 同口径）', () => {
  const store = useAppStore();
  store.openLocalFileInBrowser('D:\\文档\\首 页.html');

  expect(store.browserNavRequest?.url).toBe(
    'file:///D:/%E6%96%87%E6%A1%A3/%E9%A6%96%20%E9%A1%B5.html',
  );
});

it('相对路径转不成 file:// URL 时回退源码预览，不开浏览器页签', () => {
  const store = useAppStore();
  const previewCalls: string[] = [];
  store.openFilePreview = (async (p: string) => { previewCalls.push(p); }) as any;

  store.openLocalFileInBrowser('docs/index.html');

  expect(previewCalls).toEqual(['docs/index.html']);
  expect(store.browserNavRequest).toBeNull();
  expect(store.browserNavNonce).toBe(0);
  expect(store.rightPanelTabs).not.toContain('browser');
});

it('md 先渲染成预览页，再把生成的 HTML 交右栏浏览器打开', async () => {
  const store = useAppStore();
  const readFilePreview = vi.fn().mockResolvedValue({ ok: true, content: '# 标题\n\n正文' });
  const writePreviewPage = vi.fn().mockResolvedValue({
    ok: true,
    path: 'C:\\Users\\x\\.ycode\\exports\\preview\\preview_abc.html',
  });
  (window as any).pywebview = {
    api: { read_file_preview: readFilePreview, write_preview_page: writePreviewPage },
  };

  await store.openInRightPanel('D:\\proj\\docs\\a.md');

  expect(readFilePreview).toHaveBeenCalledWith('D:\\proj\\docs\\a.md');
  const [html, sourcePath] = writePreviewPage.mock.calls[0];
  expect(sourcePath).toBe('D:\\proj\\docs\\a.md');
  expect(String(html)).toContain('<h');
  expect(store.browserNavRequest?.url).toBe(
    'file:///C:/Users/x/.ycode/exports/preview/preview_abc.html',
  );
  expect(store.rightPanelTabs).toContain('browser');
});

it('md 读文件失败时回退源码预览弹窗', async () => {
  const store = useAppStore();
  const previewCalls: string[] = [];
  store.openFilePreview = (async (p: string) => { previewCalls.push(p); }) as any;
  (window as any).pywebview = {
    api: {
      read_file_preview: vi.fn().mockResolvedValue({ ok: false, msg: '读取失败' }),
      write_preview_page: vi.fn(),
    },
  };

  await store.openInRightPanel('D:\\proj\\docs\\a.md');

  expect(previewCalls).toEqual(['D:\\proj\\docs\\a.md']);
  expect(store.browserNavNonce).toBe(0);
});

it('缺桥（没有 write_preview_page）时 md 直接回退预览弹窗', async () => {
  const store = useAppStore();
  const previewCalls: string[] = [];
  store.openFilePreview = (async (p: string) => { previewCalls.push(p); }) as any;
  (window as any).pywebview = { api: { read_file_preview: vi.fn() } };

  await store.openInRightPanel('D:\\proj\\docs\\a.md');

  expect(previewCalls).toEqual(['D:\\proj\\docs\\a.md']);
});

it('pdf 进「文档」页签由 pdf.js 渲染，不写浏览器导航请求', async () => {
  const store = useAppStore();
  store.rightPanelCollapsed = true;

  await store.openInRightPanel('D:\\proj\\doc.pdf');

  expect(store.docPreviewRequest).toEqual({ path: 'D:\\proj\\doc.pdf', name: 'doc.pdf' });
  expect(store.docPreviewNonce).toBe(1);
  expect(store.rightPanelTabs).toContain('doc');
  expect(store.rightPanelActiveTab).toBe('doc');
  expect(store.rightPanelCollapsed).toBe(false);
  // 不再经过内嵌浏览器
  expect(store.browserNavRequest).toBeNull();
  expect(store.browserNavNonce).toBe(0);
  expect(store.rightPanelTabs).not.toContain('browser');
});

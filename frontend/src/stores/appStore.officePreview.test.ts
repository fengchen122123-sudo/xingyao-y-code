import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { createPinia, setActivePinia } from 'pinia';

import { isLoopbackHostName, useAppStore } from './appStore';

// Office 文档右栏预览：docx 由前端 docx-preview 直接渲染，不经过 PDF 转换；
// 其余 doc/xls/ppt 由后端转成 PDF（首次 10~25 秒），转换期间置
// officePreviewPending 供 BrowserPane 显示提示浮层，成功后把缓存 PDF 交
// 内嵌浏览器，失败则回退系统默认程序。

const CACHED_PDF = 'C:\\Users\\x\\.ycode\\cache\\office\\preview_abc.pdf';

beforeEach(() => setActivePinia(createPinia()));

afterEach(() => {
  (window as any).pywebview = undefined;
  vi.restoreAllMocks();
});

it('xlsx 文件：先调 preview_office_doc 转换，成功后用缓存 PDF 路径开浏览器页签', async () => {
  const store = useAppStore();
  const openLocal = vi.fn();
  store.openDocumentInRightPanel = openLocal as any;
  const previewOffice = vi.fn().mockResolvedValue({
    ok: true,
    path: CACHED_PDF,
    source: 'D:\\proj\\报告.xlsx',
  });
  (window as any).pywebview = { api: { preview_office_doc: previewOffice } };

  await store.openInRightPanel('D:\\proj\\报告.xlsx');

  expect(previewOffice).toHaveBeenCalledTimes(1);
  expect(previewOffice).toHaveBeenCalledWith('D:\\proj\\报告.xlsx');
  // 交出去的是转换后的 PDF，不是源文件；第三个参数带上源文件路径，
  // 「在文件夹中打开」才能定位到用户点的那个源文件
  expect(openLocal).toHaveBeenCalledTimes(1);
  expect(openLocal).toHaveBeenCalledWith(CACHED_PDF, '报告.xlsx', 'D:\\proj\\报告.xlsx');
  expect(store.officePreviewPending).toBeNull();
});

it('相对路径 office 链接也走同一入口（pptx / xlsx 同样被识别）', async () => {
  const store = useAppStore();
  const openLocal = vi.fn();
  store.openDocumentInRightPanel = openLocal as any;
  const previewOffice = vi.fn().mockResolvedValue({ ok: true, path: CACHED_PDF });
  (window as any).pywebview = { api: { preview_office_doc: previewOffice } };

  await store.openInRightPanel('docs/方案.pptx');
  await store.openInRightPanel('assets/表格.xlsx');

  expect(previewOffice.mock.calls).toEqual([['docs/方案.pptx'], ['assets/表格.xlsx']]);
  expect(openLocal).toHaveBeenCalledTimes(2);
});

it('转换失败：不打断用户，改用系统默认程序打开源文件，并清空进行中提示', async () => {
  const store = useAppStore();
  const openSys = vi.fn();
  store.openInSystem = openSys as any;
  const openLocal = vi.fn();
  store.openDocumentInRightPanel = openLocal as any;
  const openPath = vi.fn().mockResolvedValue({ ok: true });
  (window as any).pywebview = {
    api: {
      preview_office_doc: vi.fn().mockResolvedValue({ ok: false, msg: '转换失败：未安装 Office' }),
      open_path: openPath,
    },
  };

  await store.openInRightPanel('D:\\proj\\报告.xlsx');

  expect(openSys).toHaveBeenCalledWith('D:\\proj\\报告.xlsx');
  expect(openLocal).not.toHaveBeenCalled();
  expect(store.officePreviewPending).toBeNull();
  // 失败路径不开浏览器页签导航（没有 PDF 可开）
  expect(store.browserNavNonce).toBe(0);
});

it('转换抛异常：同样兜底系统程序打开，提示不残留', async () => {
  const store = useAppStore();
  const openSys = vi.fn();
  store.openInSystem = openSys as any;
  (window as any).pywebview = {
    api: {
      preview_office_doc: vi.fn().mockRejectedValue(new Error('COM 调用超时')),
      open_path: vi.fn(),
    },
  };

  await store.openInRightPanel('D:\\proj\\报告.ppt');

  expect(openSys).toHaveBeenCalledWith('D:\\proj\\报告.ppt');
  expect(store.officePreviewPending).toBeNull();
});

it('桥没有 preview_office_doc（旧后端）：回退源码预览弹窗，不发起转换', async () => {
  const store = useAppStore();
  const previewCalls: string[] = [];
  store.openFilePreview = (async (p: string) => { previewCalls.push(p); }) as any;
  const openLocal = vi.fn();
  store.openDocumentInRightPanel = openLocal as any;
  (window as any).pywebview = { api: { open_path: vi.fn() } };

  await store.openInRightPanel('D:\\proj\\报告.pptx');

  expect(previewCalls).toEqual(['D:\\proj\\报告.pptx']);
  expect(openLocal).not.toHaveBeenCalled();
  expect(store.officePreviewPending).toBeNull();
});

it('转换失败且宿主没有 open_path：退化为一句模态提示', async () => {
  const store = useAppStore();
  const notices: string[] = [];
  store.notifyModal = ((title: string, msg: string) => { notices.push(`${title}|${msg}`); }) as any;
  (window as any).pywebview = {
    api: { preview_office_doc: vi.fn().mockResolvedValue({ ok: false, msg: '转换失败' }) },
  };

  await store.openInRightPanel('D:\\proj\\报告.xlsx');

  expect(notices.length).toBe(1);
  expect(notices[0]).toContain('Office 预览');
  expect(notices[0]).toContain('转换失败');
  expect(store.officePreviewPending).toBeNull();
});

it('非桌面宿主（embeddedBrowser.supported=false）：不转换，提示仅桌面端可用', async () => {
  const store = useAppStore();
  store.embeddedBrowser.supported = false;
  const previewCalls: string[] = [];
  store.openFilePreview = (async (p: string) => { previewCalls.push(p); }) as any;
  const notices: string[] = [];
  store.notifyModal = ((_t: string, m: string) => { notices.push(m); }) as any;
  const previewOffice = vi.fn();
  (window as any).pywebview = { api: { preview_office_doc: previewOffice } };

  await store.openInRightPanel('D:\\proj\\报告.xlsx');

  expect(previewOffice).not.toHaveBeenCalled();
  expect(previewCalls).toEqual([]);
  expect(notices).toHaveLength(1);
  expect(notices[0]).toContain('仅支持在本机桌面端使用');
  expect(store.officePreviewPending).toBeNull();
});

it('回环主机名判定：本机形态放行，局域网 IP / 域名判为非桌面宿主', () => {
  // 桌面 pywebview 宿主：无主机名或回环地址
  expect(isLoopbackHostName('')).toBe(true);
  expect(isLoopbackHostName(null)).toBe(true);
  expect(isLoopbackHostName('localhost')).toBe(true);
  expect(isLoopbackHostName('127.0.0.1')).toBe(true);
  expect(isLoopbackHostName('::1')).toBe(true);
  expect(isLoopbackHostName('[::1]')).toBe(true);
  expect(isLoopbackHostName('LOCALHOST')).toBe(true);
  // 手机经局域网访问：桌面专属桥被 LAN fail-closed 封禁，不能走桌面分支
  expect(isLoopbackHostName('192.168.1.50')).toBe(false);
  expect(isLoopbackHostName('10.0.0.8')).toBe(false);
  expect(isLoopbackHostName('my-pc.local')).toBe(false);
});

it('转换期间 officePreviewPending 有值并摊开右栏，结束后清空', async () => {
  const store = useAppStore();
  store.rightPanelCollapsed = true;
  let resolveConv: (v: unknown) => void = () => {};
  const previewOffice = vi.fn().mockReturnValue(
    new Promise((resolve) => { resolveConv = resolve; }),
  );
  (window as any).pywebview = { api: { preview_office_doc: previewOffice } };

  const running = store.openInRightPanel('D:\\proj\\文档\\季度报告.xlsx');

  // 转换挂起期间：提示已置位（界面据此显示「正在转换」浮层），右栏已摊开
  expect(store.officePreviewPending).toEqual({
    path: 'D:\\proj\\文档\\季度报告.xlsx',
    name: '季度报告.xlsx',
  });
  expect(store.rightPanelTabs).toContain('doc');
  expect(store.rightPanelActiveTab).toBe('doc');
  expect(store.rightPanelCollapsed).toBe(false);

  resolveConv({ ok: true, path: CACHED_PDF });
  await running;

  // 结束后一定清空，浮层不会永远转圈；转出的 PDF 已写进「文档」页签的打开请求
  expect(store.officePreviewPending).toBeNull();
  expect(store.docPreviewRequest?.path).toBe(CACHED_PDF);
  expect(store.docPreviewRequest?.name).toBe('季度报告.xlsx');
  expect(store.docPreviewNonce).toBe(1);
});

it('转换失败时进行中提示同样被清空（finally 保证）', async () => {
  const store = useAppStore();
  let rejectConv: (e: unknown) => void = () => {};
  const previewOffice = vi.fn().mockReturnValue(
    new Promise((_resolve, reject) => { rejectConv = reject; }),
  );
  store.openInSystem = vi.fn() as any;
  (window as any).pywebview = { api: { preview_office_doc: previewOffice, open_path: vi.fn() } };

  const running = store.openInRightPanel('D:\\proj\\a.pptx');
  expect(store.officePreviewPending).not.toBeNull();

  rejectConv(new Error('boom'));
  await running;

  expect(store.officePreviewPending).toBeNull();
});

it('docx 不转 PDF：直接把源文件交给「文档」页签，由前端 docx-preview 渲染', async () => {
  const store = useAppStore();
  const openLocal = vi.fn();
  store.openDocumentInRightPanel = openLocal as any;
  const previewOffice = vi.fn();
  (window as any).pywebview = { api: { preview_office_doc: previewOffice } };

  await store.openInRightPanel('D:\\proj\\方案.docx');

  // 关键：不调转换桥、不置「正在转换」提示，直接把源文件开进「文档」页签
  expect(previewOffice).not.toHaveBeenCalled();
  expect(store.officePreviewPending).toBeNull();
  expect(openLocal).toHaveBeenCalledTimes(1);
  expect(openLocal).toHaveBeenCalledWith('D:\\proj\\方案.docx', '方案.docx');
});
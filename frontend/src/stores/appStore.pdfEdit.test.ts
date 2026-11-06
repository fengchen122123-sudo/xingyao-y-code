import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { createPinia, setActivePinia } from 'pinia';

import { useAppStore } from './appStore';

// PDF 编辑保存链路：DocumentPane 产出的 ops（PDF point 坐标契约）交给 store，
// store 负责目标路径判定（普通 PDF vs Office 转换文档）、桥调用与保存后重载。
// Office 文档的 overwrite 必须拦下（会写坏用户的 .docx），copy 的副本要落在
// 源文档同目录，不能落到后端转换缓存目录里。

const SOURCE_PDF = 'D:\\proj\\计划.pdf';
const EDITED_PDF = 'D:\\proj\\计划_已编辑_1.pdf';
const SOURCE_DOCX = 'D:\\docs\\季度报告.docx';
const CACHED_PDF = 'C:\\Users\\x\\.ycode\\exports\\preview\\office_ab12.pdf';

// 一条最小的合法高亮操作（validateOps 口径）
const OPS = [
  {
    kind: 'annot',
    type: 'highlight',
    page: 1,
    color: '#ffd400',
    opacity: 0.35,
    rect: { x: 10, y: 20, w: 120, h: 16 },
  },
];

beforeEach(() => setActivePinia(createPinia()));

afterEach(() => {
  (window as any).pywebview = undefined;
  vi.restoreAllMocks();
});

it('普通 PDF 保存副本：out_path 交给后端默认命名，成功后重载新文件', async () => {
  const save = vi.fn().mockResolvedValue({ ok: true, path: EDITED_PDF, pages: 2 });
  (window as any).pywebview = { api: { save_pdf_edits: save } };

  const store = useAppStore();
  store.openDocumentInRightPanel(SOURCE_PDF);
  const nonceBefore = store.docPreviewNonce;

  const res = await store.savePdfEdits(OPS, 'copy');

  // 普通 PDF：第四个参数留空，后端在源文件同目录按时间戳命名
  expect(save).toHaveBeenCalledTimes(1);
  expect(save).toHaveBeenCalledWith(SOURCE_PDF, OPS, 'copy', '');
  expect(res).toMatchObject({ ok: true, path: EDITED_PDF, pages: 2 });
  // 保存后把新文件重新开进「文档」页签：后续编辑针对的是新 PDF，不是原来那份
  expect(store.docPreviewRequest?.path).toBe(EDITED_PDF);
  expect(store.docPreviewRequest?.name).toBe('计划_已编辑_1.pdf');
  expect(store.docPreviewNonce).toBe(nonceBefore + 1);
});

it('普通 PDF 覆盖保存：第三/第四参数为 overwrite 与空串，backup 原样透传', async () => {
  const backup = 'D:\\proj\\计划.bak_20260101_101010.pdf';
  const save = vi.fn().mockResolvedValue({
    ok: true, path: SOURCE_PDF, pages: 3, backup,
  });
  (window as any).pywebview = { api: { save_pdf_edits: save } };

  const store = useAppStore();
  store.openDocumentInRightPanel(SOURCE_PDF);

  const res = await store.savePdfEdits(OPS, 'overwrite');

  expect(save).toHaveBeenCalledWith(SOURCE_PDF, OPS, 'overwrite', '');
  expect(res?.backup).toBe(backup);
});

it('Office 转换文档禁止覆盖：不发起桥调用，提示用「保存副本」', async () => {
  const save = vi.fn();
  (window as any).pywebview = { api: { save_pdf_edits: save } };

  const store = useAppStore();
  const notices: string[] = [];
  store.notifyModal = ((_title: string, msg: string) => { notices.push(msg); }) as any;
  store.openDocumentInRightPanel(CACHED_PDF, '季度报告.docx', SOURCE_DOCX);

  const res = await store.savePdfEdits(OPS, 'overwrite');

  expect(res).toBeNull();
  expect(save).not.toHaveBeenCalled();
  expect(notices).toHaveLength(1);
  expect(notices[0]).toContain('不能覆盖');
});

it('Office 转换文档保存副本：副本落在源 docx 同目录，而不是缓存目录', async () => {
  const save = vi.fn().mockResolvedValue({
    ok: true, path: 'D:\\docs\\季度报告_已编辑_20260101_101010.pdf', pages: 5,
  });
  (window as any).pywebview = { api: { save_pdf_edits: save } };

  const store = useAppStore();
  store.openDocumentInRightPanel(CACHED_PDF, '季度报告.docx', SOURCE_DOCX);

  const res = await store.savePdfEdits(OPS, 'copy');

  expect(res?.ok).toBe(true);
  // 源文档路径（不是缓存 PDF）作为编辑目标，out_path 前端拼到源文档目录
  expect(save).toHaveBeenCalledWith(
    SOURCE_DOCX,
    OPS,
    'copy',
    expect.stringMatching(/^D:\\docs\\季度报告_已编辑_\d{8}_\d{6}\.pdf$/),
  );
});

it('桥没有 save_pdf_edits（旧后端）：返回 null 并提示重启应用', async () => {
  (window as any).pywebview = { api: {} };

  const store = useAppStore();
  const notices: string[] = [];
  store.notifyModal = ((_title: string, msg: string) => { notices.push(msg); }) as any;
  store.openDocumentInRightPanel(SOURCE_PDF);

  const res = await store.savePdfEdits(OPS, 'copy');

  expect(res).toBeNull();
  expect(notices).toHaveLength(1);
  expect(notices[0]).toContain('不支持保存编辑');
});

it('保存失败：返回 null、提示失败原因、不重载文档', async () => {
  const save = vi.fn().mockResolvedValue({ ok: false, msg: 'PDF 加密' });
  (window as any).pywebview = { api: { save_pdf_edits: save } };

  const store = useAppStore();
  const notices: string[] = [];
  store.notifyModal = ((_title: string, msg: string) => { notices.push(msg); }) as any;
  store.openDocumentInRightPanel(SOURCE_PDF);
  const nonceBefore = store.docPreviewNonce;

  const res = await store.savePdfEdits(OPS, 'copy');

  expect(res).toBeNull();
  expect(store.docPreviewNonce).toBe(nonceBefore);
  expect(store.docPreviewRequest?.path).toBe(SOURCE_PDF);
  expect(notices).toHaveLength(1);
  expect(notices[0]).toContain('保存失败');
  expect(notices[0]).toContain('PDF 加密');
});
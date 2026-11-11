// 右栏「文档」页签：pdf.js 自绘 + 自建工具栏。
// pdf.js 用 vi.mock 顶掉（真渲染要 canvas/worker，jsdom 里跑不了），这里验证的是
// 数据流：请求 → 桥读文件 → 解析 PDF → 逐页画 canvas → 工具栏交互/错误兜底。
import { beforeEach, afterEach, expect, it, vi } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import { nextTick } from 'vue';

import DocumentPane from './DocumentPane.vue';
import { useAppStore } from '../stores/appStore';

vi.mock('pdfjs-dist', () => ({
  GlobalWorkerOptions: { workerSrc: '' },
  getDocument: vi.fn(),
}));
vi.mock('pdfjs-dist/build/pdf.worker.min.mjs?url', () => ({ default: 'pdf.worker.min.mjs' }));
// docx-preview 同样顶掉：jsdom 里没有真实 docx 二进制可解析，这里只验数据流
vi.mock('docx-preview', () => ({ renderAsync: vi.fn().mockResolvedValue(undefined) }));

const PDF_DATA_URL = 'data:application/pdf;base64,JVBERi0xLjQK';

function stubBridge(api: Record<string, unknown>) {
  (window as any).pywebview = { api };
}

function makePageProxy() {
  return {
    getViewport: ({ scale }: { scale: number }) => ({ width: 600 * scale, height: 800 * scale }),
    // 真渲染这里会往 canvas 里画，jsdom 无 2d 上下文，只要 promise 兑现即可
    render: () => ({ promise: Promise.resolve() }),
  };
}

async function stubPdfDocument(pages: number) {
  const pdfjs: any = await import('pdfjs-dist');
  const doc = {
    numPages: pages,
    getPage: vi.fn(async () => makePageProxy()),
    destroy: vi.fn(async () => undefined),
  };
  pdfjs.getDocument.mockReturnValue({ promise: Promise.resolve(doc) });
  return { pdfjs, doc };
}

beforeEach(() => setActivePinia(createPinia()));

afterEach(() => {
  (window as any).pywebview = undefined;
  vi.restoreAllMocks();
});

it('打开 PDF：读文件 → pdf.js 解析 → 按页生成 canvas，工具栏显示文件名与页码', async () => {
  const { pdfjs } = await stubPdfDocument(2);
  const readFilePreview = vi.fn().mockResolvedValue({
    ok: true, kind: 'pdf', data_url: PDF_DATA_URL, path: 'D:\\proj\\计划.pdf',
  });
  stubBridge({ read_file_preview: readFilePreview });

  const store = useAppStore();
  store.openDocumentInRightPanel('D:\\proj\\计划.pdf');
  const wrapper = mount(DocumentPane, { props: { active: true } });
  await flushPromises();
  await nextTick();
  await flushPromises();

  expect(readFilePreview).toHaveBeenCalledWith('D:\\proj\\计划.pdf');
  expect(pdfjs.getDocument).toHaveBeenCalledTimes(1);
  // 没随包带 cmaps：用系统字体兜底非嵌入字体的中文
  expect(pdfjs.getDocument.mock.calls[0][0]).toMatchObject({
    url: PDF_DATA_URL,
    useSystemFonts: true,
  });
  expect(wrapper.get('[data-test="dc-name"]').text()).toBe('计划.pdf');
  expect(wrapper.get('[data-test="dc-pager"]').text()).toBe('1 / 2');
  expect(wrapper.find('[data-test="dc-page-1"]').exists()).toBe(true);
  expect(wrapper.find('[data-test="dc-page-2"]').exists()).toBe(true);
  // 占位块按 scale=1 的页面尺寸给出，图片出来前高度不跳动
  expect((wrapper.get('[data-test="dc-page-1"]').element as HTMLElement).style.height).toBe('800px');
  // 位图尺寸必须按 viewport 像素定好：留默认 300×150 的话，pdf.js 会把整页画进
  // 那一小片，再被 CSS 拉伸成一片糊字（2026-11-06 用户反馈的"乱码"就是这个）
  const canvasEl = wrapper.get('[data-test="dc-page-1"] canvas').element as HTMLCanvasElement;
  expect(canvasEl.width).toBe(600);
  expect(canvasEl.height).toBe(800);

  await wrapper.unmount();
});

it('翻页与缩放：页码前进到末页、缩放百分比按 20% 步进', async () => {
  await stubPdfDocument(2);
  stubBridge({
    read_file_preview: vi.fn().mockResolvedValue({ ok: true, kind: 'pdf', data_url: PDF_DATA_URL }),
  });

  const store = useAppStore();
  store.openDocumentInRightPanel('D:\\proj\\计划.pdf');
  const wrapper = mount(DocumentPane, { props: { active: true } });
  await flushPromises();
  await nextTick();
  await flushPromises();

  // jsdom 里量不到容器宽度，适应宽度比例保持 1 → 初始 100%
  expect(wrapper.get('[data-test="dc-zoom"]').text()).toBe('100%');

  await wrapper.get('[data-test="dc-next"]').trigger('click');
  expect(wrapper.get('[data-test="dc-pager"]').text()).toBe('2 / 2');

  await wrapper.get('[data-test="dc-zoom-in"]').trigger('click');
  await flushPromises();
  expect(wrapper.get('[data-test="dc-zoom"]').text()).toBe('120%');

  await wrapper.get('[data-test="dc-zoom-out"]').trigger('click');
  await flushPromises();
  expect(wrapper.get('[data-test="dc-zoom"]').text()).toBe('100%');

  await wrapper.get('[data-test="dc-zoom-out"]').trigger('click');
  await flushPromises();
  // 缩小后「适应宽度」把比例拉回 1
  await wrapper.get('[data-test="dc-fit"]').trigger('click');
  await flushPromises();
  expect(wrapper.get('[data-test="dc-zoom"]').text()).toBe('100%');

  await wrapper.unmount();
});

it('读文件失败：显示后端返回的原因，并给「用系统默认程序打开」兜底', async () => {
  await stubPdfDocument(1);
  const openPath = vi.fn().mockResolvedValue({ ok: true });
  stubBridge({
    read_file_preview: vi.fn().mockResolvedValue({ ok: false, msg: 'PDF 过大（64.0 MB，上限 32 MB）' }),
    open_path: openPath,
  });

  const store = useAppStore();
  store.openDocumentInRightPanel('D:\\proj\\大文件.pdf');
  const wrapper = mount(DocumentPane, { props: { active: true } });
  await flushPromises();
  await nextTick();

  expect(wrapper.get('[data-test="dc-error"]').text()).toContain('PDF 过大');
  const fallback = wrapper.findAll('button').find(b => b.text().includes('用系统默认程序打开'));
  expect(fallback).toBeTruthy();
  await fallback!.trigger('click');
  await flushPromises();
  expect(openPath).toHaveBeenCalledWith('D:\\proj\\大文件.pdf');

  await wrapper.unmount();
});

it('非 PDF 返回（比如后端没给 kind）：报不可渲染而不是白屏', async () => {
  stubBridge({
    read_file_preview: vi.fn().mockResolvedValue({ ok: true, content: '纯文本' }),
  });

  const store = useAppStore();
  store.openDocumentInRightPanel('D:\\proj\\a.pdf');
  const wrapper = mount(DocumentPane, { props: { active: true } });
  await flushPromises();
  await nextTick();

  expect(wrapper.get('[data-test="dc-error"]').text()).toContain('不是可渲染的 PDF');
  await wrapper.unmount();
});

it('缺桥（非桌面宿主）：提示不支持应用内预览', async () => {
  const store = useAppStore();
  store.openDocumentInRightPanel('D:\\proj\\a.pdf');
  const wrapper = mount(DocumentPane, { props: { active: true } });
  await flushPromises();
  await nextTick();

  expect(wrapper.get('[data-test="dc-error"]').text()).toContain('不支持应用内文档预览');
  await wrapper.unmount();
});

it('没有打开请求时是引导空态；Office 转换期间显示转换提示', async () => {
  const wrapper = mount(DocumentPane, { props: { active: true } });
  await flushPromises();
  expect(wrapper.find('[data-test="dc-empty"]').exists()).toBe(true);

  const store = useAppStore();
  store.officePreviewPending = { path: 'D:\\proj\\报告.docx', name: '报告.docx' };
  await nextTick();
  expect(wrapper.find('[data-test="dc-office-pending"]').exists()).toBe(true);
  expect(wrapper.get('[data-test="dc-office-pending"]').text()).toContain('报告.docx');

  await wrapper.unmount();
});

it('「在文件夹中打开」定位源文件：Office 文档用 sourcePath，不落到转换缓存目录', async () => {
  await stubPdfDocument(1);
  const reveal = vi.fn().mockResolvedValue({ ok: true });
  stubBridge({
    read_file_preview: vi.fn().mockResolvedValue({ ok: true, kind: 'pdf', data_url: PDF_DATA_URL }),
    reveal_in_explorer: reveal,
  });

  const store = useAppStore();
  // Office 文档：path 是后端转出的缓存 PDF，sourcePath 才是用户点开的那个 .docx
  store.openDocumentInRightPanel(
    'C:\\Users\\x\\.ycode\\exports\\preview\\office_ab12.pdf',
    '季度报告.docx',
    'D:\\docs\\季度报告.docx',
  );
  const wrapper = mount(DocumentPane, { props: { active: true } });
  await flushPromises();
  await nextTick();

  await wrapper.get('[data-test="dc-reveal"]').trigger('click');
  await flushPromises();
  expect(reveal).toHaveBeenCalledWith('D:\\docs\\季度报告.docx');
  // 标题显示源文档名，用户认的是 .docx
  expect(wrapper.get('[data-test="dc-name"]').text()).toBe('季度报告.docx');

  await wrapper.unmount();
});

it('宿主没有 reveal_in_explorer（旧后端）：退回只打开所在目录', async () => {
  await stubPdfDocument(1);
  const openFolder = vi.fn().mockResolvedValue({ ok: true });
  stubBridge({
    read_file_preview: vi.fn().mockResolvedValue({ ok: true, kind: 'pdf', data_url: PDF_DATA_URL }),
    open_folder_in_explorer: openFolder,
  });

  const store = useAppStore();
  store.openDocumentInRightPanel('D:\\proj\\计划.pdf');
  const wrapper = mount(DocumentPane, { props: { active: true } });
  await flushPromises();
  await nextTick();

  await wrapper.get('[data-test="dc-reveal"]').trigger('click');
  await flushPromises();
  expect(openFolder).toHaveBeenCalledWith('D:\\proj');

  await wrapper.unmount();
});

// ── 编辑模式 ──
// jsdom 的 getBoundingClientRect 全返回 0，编辑层的坐标换算会除零，
// 所以统一 stub 成一块 600×800（与 makePageProxy 的 scale=1 尺寸一致）的页面框。
const EDIT_LAYER_BOX = {
  left: 0, top: 0, width: 600, height: 800,
  right: 600, bottom: 800, x: 0, y: 0,
  toJSON: () => ({}),
} as unknown as DOMRect;

function stubLayout() {
  vi.spyOn(Element.prototype, 'getBoundingClientRect').mockReturnValue(EDIT_LAYER_BOX);
}

// jsdom 没有 PointerEvent，手动挂 clientX/clientY（VTU 的 trigger 在无 PointerEvent 时
// 会退化成 Event，坐标会被丢掉）
function pointerEvent(type: string, x: number, y: number): Event {
  const ev = new Event(type, { bubbles: true, cancelable: true });
  Object.defineProperty(ev, 'clientX', { value: x });
  Object.defineProperty(ev, 'clientY', { value: y });
  return ev;
}

function dragOn(layer: Element, from: [number, number], to: [number, number]) {
  layer.dispatchEvent(pointerEvent('pointerdown', from[0], from[1]));
  layer.dispatchEvent(pointerEvent('pointermove', to[0], to[1]));
  layer.dispatchEvent(pointerEvent('pointerup', to[0], to[1]));
}

async function mountEditable(pages = 2) {
  await stubPdfDocument(pages);
  stubBridge({
    read_file_preview: vi.fn().mockResolvedValue({ ok: true, kind: 'pdf', data_url: PDF_DATA_URL }),
  });
  const store = useAppStore();
  store.openDocumentInRightPanel('D:\\proj\\计划.pdf');
  const wrapper = mount(DocumentPane, { props: { active: true } });
  await flushPromises();
  await nextTick();
  await flushPromises();
  return { wrapper, store };
}

async function enterEditMode(wrapper: Awaited<ReturnType<typeof mountEditable>>['wrapper']) {
  await wrapper.get('[data-test="dc-edit-toggle"]').trigger('click');
  await nextTick();
}

function layerEl(wrapper: Awaited<ReturnType<typeof mountEditable>>['wrapper'], page = 1) {
  return wrapper.get(`[data-test="dc-edit-layer-${page}"]`).element;
}

it('编辑模式：第二行工具栏出现，每页挂一层覆盖 svg（viewBox 用 PDF point 尺寸）', async () => {
  stubLayout();
  const { wrapper } = await mountEditable(2);
  expect(wrapper.find('[data-test="dc-edit-toolbar"]').exists()).toBe(false);

  await enterEditMode(wrapper);
  expect(wrapper.find('[data-test="dc-edit-toolbar"]').exists()).toBe(true);
  expect(wrapper.find('[data-test="dc-edit-layer-1"]').exists()).toBe(true);
  expect(wrapper.find('[data-test="dc-edit-layer-2"]').exists()).toBe(true);
  expect(layerEl(wrapper, 1).getAttribute('viewBox')).toBe('0 0 600 800');

  await wrapper.unmount();
});

it('高亮：拖出一块矩形后生成 highlight 标注，并亮起脏标记', async () => {
  stubLayout();
  const { wrapper } = await mountEditable(2);
  await enterEditMode(wrapper);
  await wrapper.get('[data-test="dc-tool-highlight"]').trigger('click');

  dragOn(layerEl(wrapper), [100, 100], [300, 140]);
  await nextTick();

  const annots = wrapper.findAll('[data-test="dc-annot"]');
  expect(annots).toHaveLength(1);
  expect(annots[0].attributes('data-annot-type')).toBe('highlight');
  expect(wrapper.find('[data-test="dc-dirty"]').exists()).toBe(true);
  // 拖拽预览已收尾
  expect(wrapper.find('[data-test="dc-edit-draft"]').exists()).toBe(false);

  await wrapper.unmount();
});

it('撤销 / 重做：标注随之消失与恢复', async () => {
  stubLayout();
  const { wrapper } = await mountEditable(2);
  await enterEditMode(wrapper);
  await wrapper.get('[data-test="dc-tool-highlight"]').trigger('click');
  dragOn(layerEl(wrapper), [100, 100], [300, 140]);
  await nextTick();
  expect(wrapper.findAll('[data-test="dc-annot"]')).toHaveLength(1);

  await wrapper.get('[data-test="dc-edit-undo"]').trigger('click');
  await nextTick();
  expect(wrapper.findAll('[data-test="dc-annot"]')).toHaveLength(0);
  expect(wrapper.find('[data-test="dc-dirty"]').exists()).toBe(false);

  await wrapper.get('[data-test="dc-edit-redo"]').trigger('click');
  await nextTick();
  expect(wrapper.findAll('[data-test="dc-annot"]')).toHaveLength(1);

  await wrapper.unmount();
});

it('便签：拖完矩形弹内联编辑器，输入文本并确定后生成 note 标注', async () => {
  stubLayout();
  const { wrapper } = await mountEditable(2);
  await enterEditMode(wrapper);
  await wrapper.get('[data-test="dc-tool-note"]').trigger('click');

  dragOn(layerEl(wrapper), [100, 100], [300, 160]);
  await nextTick();
  expect(wrapper.find('[data-test="dc-text-editor"]').exists()).toBe(true);

  await wrapper.get('[data-test="dc-text-editor"] textarea').setValue('这条要复核');
  await wrapper.get('[data-test="dc-text-confirm"]').trigger('click');
  await nextTick();

  expect(wrapper.find('[data-test="dc-text-editor"]').exists()).toBe(false);
  const annots = wrapper.findAll('[data-test="dc-annot"]');
  expect(annots).toHaveLength(1);
  expect(annots[0].attributes('data-annot-type')).toBe('note');
  expect(annots[0].text()).toContain('这条要复核');

  await wrapper.unmount();
});

it('白块遮盖：拖出一块后生成 redact 标注', async () => {
  stubLayout();
  const { wrapper } = await mountEditable(1);
  await enterEditMode(wrapper);
  await wrapper.get('[data-test="dc-tool-redact"]').trigger('click');

  dragOn(layerEl(wrapper), [120, 200], [400, 320]);
  await nextTick();

  const annots = wrapper.findAll('[data-test="dc-annot"]');
  expect(annots).toHaveLength(1);
  expect(annots[0].attributes('data-annot-type')).toBe('redact');

  await wrapper.unmount();
});

it('页面操作：删页后显示「保存时生效」提示条，预览不重排', async () => {
  stubLayout();
  const { wrapper } = await mountEditable(2);
  await enterEditMode(wrapper);
  expect(wrapper.find('[data-test="dc-pageops-pending"]').exists()).toBe(false);

  await wrapper.get('[data-test="dc-page-delete"]').trigger('click');
  await nextTick();

  const bar = wrapper.get('[data-test="dc-pageops-pending"]');
  expect(bar.text()).toContain('1');
  expect(bar.text()).toContain('删除页');
  expect(bar.text()).toContain('保存时生效');
  // 页面块数量不变：页数重排要等后端保存时才发生
  expect(wrapper.findAll('[data-page]')).toHaveLength(2);
  expect(wrapper.get('[data-test="dc-pager"]').text()).toBe('1 / 2');

  await wrapper.unmount();
});

it('保存副本：把 ops 数组和 copy 模式交给 store.savePdfEdits', async () => {
  stubLayout();
  const { wrapper, store } = await mountEditable(1);
  const savePdfEdits = vi.fn().mockResolvedValue({ ok: true, path: 'D:\\proj\\x.pdf' });
  (store as any).savePdfEdits = savePdfEdits;

  await enterEditMode(wrapper);
  await wrapper.get('[data-test="dc-tool-highlight"]').trigger('click');
  dragOn(layerEl(wrapper), [100, 100], [300, 140]);
  await nextTick();

  await wrapper.get('[data-test="dc-edit-save-copy"]').trigger('click');
  await flushPromises();

  expect(savePdfEdits).toHaveBeenCalledTimes(1);
  const [list, mode] = savePdfEdits.mock.calls[0];
  expect(mode).toBe('copy');
  expect(Array.isArray(list)).toBe(true);
  expect(list.length).toBeGreaterThanOrEqual(1);
  expect(list[0]).toMatchObject({ kind: 'annot', type: 'highlight', page: 1 });

  await wrapper.unmount();
});

it('保存副本成功：清空标注并退出编辑模式', async () => {
  stubLayout();
  const { wrapper, store } = await mountEditable(1);
  const savePdfEdits = vi.fn().mockResolvedValue({ ok: true, path: 'D:\\proj\\x.pdf' });
  (store as any).savePdfEdits = savePdfEdits;

  await enterEditMode(wrapper);
  await wrapper.get('[data-test="dc-tool-highlight"]').trigger('click');
  dragOn(layerEl(wrapper), [100, 100], [300, 140]);
  await nextTick();
  expect(wrapper.findAll('[data-test="dc-annot"]')).toHaveLength(1);

  await wrapper.get('[data-test="dc-edit-save-copy"]').trigger('click');
  await flushPromises();

  // 已落盘：历史与标注清空，编辑模式一并退出（第二行工具栏消失）
  expect(wrapper.findAll('[data-test="dc-annot"]')).toHaveLength(0);
  expect(wrapper.find('[data-test="dc-edit-toolbar"]').exists()).toBe(false);
  expect(wrapper.find('[data-test="dc-dirty"]').exists()).toBe(false);

  await wrapper.unmount();
});

it('便签文本为空时点确定：不入历史，不产生标注', async () => {
  stubLayout();
  const { wrapper } = await mountEditable(1);
  await enterEditMode(wrapper);
  await wrapper.get('[data-test="dc-tool-note"]').trigger('click');

  dragOn(layerEl(wrapper), [100, 100], [300, 160]);
  await nextTick();
  await wrapper.get('[data-test="dc-text-editor"] textarea').setValue('   ');
  await wrapper.get('[data-test="dc-text-confirm"]').trigger('click');
  await nextTick();

  expect(wrapper.find('[data-test="dc-text-editor"]').exists()).toBe(false);
  expect(wrapper.findAll('[data-test="dc-annot"]')).toHaveLength(0);
  expect(wrapper.find('[data-test="dc-dirty"]').exists()).toBe(false);

  await wrapper.unmount();
});

it('打开 docx：docx-preview 直接渲染，不出页码也不给 PDF 编辑入口', async () => {
  const mod: any = await import('docx-preview');
  const readFilePreview = vi.fn().mockResolvedValue({
    ok: true,
    kind: 'docx',
    data_url: 'data:application/vnd.openxmlformats-officedocument.wordprocessingml.document;base64,AAAA',
    path: 'D:\\proj\\计划.docx',
  });
  stubBridge({ read_file_preview: readFilePreview });

  const store = useAppStore();
  store.openDocumentInRightPanel('D:\\proj\\计划.docx');
  const wrapper = mount(DocumentPane, { props: { active: true } });
  await flushPromises();
  await nextTick();

  // 渲染交给 docx-preview，容器按 kind=docx 出现，没有任何 canvas 页
  expect(mod.renderAsync).toHaveBeenCalledTimes(1);
  expect(wrapper.find('[data-test="dc-docx"]').exists()).toBe(true);
  expect(wrapper.find('.dc-page').exists()).toBe(false);
  // Word 文档没有可翻的 PDF 页，也不支持 PDF 编辑
  expect(wrapper.find('[data-test="dc-pager"]').exists()).toBe(false);
  expect(wrapper.get('[data-test="dc-edit-toggle"]').attributes('disabled')).toBe('');

  await wrapper.unmount();
});

it('选择工具：点选标注后拖动会平移，松手才落历史', async () => {
  stubLayout();
  const { wrapper } = await mountEditable(2);
  await enterEditMode(wrapper);

  // 先画一个方框（100,200 → 200,260，scale=1 时显示坐标就等于 PDF 点）
  await wrapper.get('[data-test="dc-tool-rect"]').trigger('click');
  dragOn(layerEl(wrapper, 1), [100, 200], [200, 260]);
  await nextTick();
  const annotSel = '[data-test="dc-annot"]';
  expect(wrapper.findAll(annotSel)).toHaveLength(1);

  // 切到「选择」工具，点中这条标注
  await wrapper.get('[data-test="dc-tool-select"]').trigger('click');
  const annot = wrapper.get(annotSel);
  expect(annot.attributes('data-annot-index')).toBe('0');
  annot.element.dispatchEvent(pointerEvent('pointerdown', 120, 210));
  await nextTick();
  expect(annot.classes()).toContain('is-selected');

  // 拖动：window 上收 move/up（跨出页面也能拖）
  window.dispatchEvent(pointerEvent('pointermove', 160, 230));
  await nextTick();
  expect(annot.attributes('transform')).toBe('translate(40 20)');

  window.dispatchEvent(pointerEvent('pointerup', 160, 230));
  await nextTick();
  // 落盘后 transform 清掉，矩形本身平移了 40/20
  const box = wrapper.get(`${annotSel} rect`);
  expect(box.attributes('x')).toBe('140');
  expect(box.attributes('y')).toBe('220');

  await wrapper.unmount();
});

it('选择工具：Delete 删除选中标注，「删除选中」按钮无选中时禁用', async () => {
  stubLayout();
  const { wrapper } = await mountEditable(2);
  await enterEditMode(wrapper);
  expect(wrapper.get('[data-test="dc-edit-delete"]').attributes('disabled')).toBeDefined();

  await wrapper.get('[data-test="dc-tool-highlight"]').trigger('click');
  dragOn(layerEl(wrapper, 1), [100, 200], [300, 240]);
  await nextTick();
  expect(wrapper.findAll('[data-test="dc-annot"]')).toHaveLength(1);

  await wrapper.get('[data-test="dc-tool-select"]').trigger('click');
  wrapper.get('[data-test="dc-annot"]').element.dispatchEvent(pointerEvent('pointerdown', 150, 210));
  await nextTick();
  expect(wrapper.get('[data-test="dc-edit-delete"]').attributes('disabled')).toBeUndefined();

  window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Delete' }));
  await nextTick();
  expect(wrapper.findAll('[data-test="dc-annot"]')).toHaveLength(0);

  await wrapper.unmount();
});

it('覆盖保存：确认框点「取消」时不发起保存', async () => {
  stubLayout();
  const { wrapper, store } = await mountEditable(2);
  await enterEditMode(wrapper);
  await wrapper.get('[data-test="dc-tool-rect"]').trigger('click');
  dragOn(layerEl(wrapper, 1), [100, 200], [200, 260]);
  await nextTick();

  const savePdfEdits = vi.fn().mockResolvedValue({ ok: true, path: 'D:\\proj\\x.pdf' });
  (store as any).savePdfEdits = savePdfEdits;
  const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false);

  await wrapper.get('[data-test="dc-edit-save-overwrite"]').trigger('click');
  await flushPromises();

  expect(confirmSpy).toHaveBeenCalled();
  expect(savePdfEdits).not.toHaveBeenCalled();

  confirmSpy.mockRestore();
  await wrapper.unmount();
});

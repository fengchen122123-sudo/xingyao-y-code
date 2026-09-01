import { flushPromises } from '@vue/test-utils';
import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest';
import { setActivePinia, createPinia } from 'pinia';
import { useAttachments } from './useAttachments';
import { useAppStore } from '../stores/appStore';

function makeFile(name: string, size: number): File {
  const f = new File(['x'], name);
  Object.defineProperty(f, 'size', { value: size });
  return f;
}

function dispatchDrop(el: HTMLElement, file: File) {
  const event = new Event('drop', { bubbles: true, cancelable: true }) as DragEvent;
  Object.defineProperty(event, 'dataTransfer', {
    value: {
      files: [file],
      types: ['Files'],
      getData: () => '',
    },
  });
  el.dispatchEvent(event);
}

afterEach(() => {
  vi.useRealTimers();
  delete (window as any).pywebview;
});

describe('useAttachments.uploadFiles', () => {
  beforeEach(() => {
    setActivePinia(createPinia());
  });

  it('超过 50MB 的文件被预检拦截，不发起上传', async () => {
    const store = useAppStore();
    const spy = vi.spyOn(store, 'uploadFile').mockResolvedValue('/tmp/x');
    const notify = vi.spyOn(store, 'notifyChat').mockImplementation(() => {});
    const { uploadFiles, attachments } = useAttachments();
    await uploadFiles([makeFile('big.jpg', 50 * 1024 * 1024 + 1)]);
    expect(spy).not.toHaveBeenCalled();
    expect(notify).toHaveBeenCalled();
    expect(attachments.value).toHaveLength(0);
  });

  it('上传成功后路径加入附件列表', async () => {
    const store = useAppStore();
    vi.spyOn(store, 'uploadFile').mockResolvedValue('C:/tmp/upload_a.jpg');
    vi.spyOn(store, 'getFilesInfo').mockResolvedValue([
      { path: 'C:/tmp/upload_a.jpg', name: 'a.jpg', ext: '.jpg', size: 3, ok: true },
    ]);
    vi.spyOn(store, 'fetchAttachmentThumbnails').mockResolvedValue(new Map());
    const { uploadFiles, attachments } = useAttachments();
    await uploadFiles([makeFile('a.jpg', 3)]);
    expect(attachments.value.map(a => a.path)).toContain('C:/tmp/upload_a.jpg');
  });
});

describe('useAttachments 会话草稿归属', () => {
  beforeEach(() => {
    setActivePinia(createPinia());
  });

  it('切换会话时附件列表互相隔离，切回后恢复', async () => {
    const store = useAppStore();
    store.currentSessionName = 'session-a';
    vi.spyOn(store, 'getFilesInfo').mockImplementation(async paths =>
      paths.map(path => ({ path, name: 'a.txt', ext: '.txt', size: 1, ok: true })));
    const { addPaths, attachments } = useAttachments();

    await addPaths(['C:/tmp/a.txt']);
    expect(attachments.value.map(a => a.path)).toEqual(['C:/tmp/a.txt']);

    store.currentSessionName = 'session-b';
    expect(attachments.value).toEqual([]);

    store.currentSessionName = 'session-a';
    expect(attachments.value.map(a => a.path)).toEqual(['C:/tmp/a.txt']);
  });

  it('输入组件卸载后重新创建附件逻辑仍恢复当前会话附件', async () => {
    const store = useAppStore();
    store.currentSessionName = 'session-a';
    vi.spyOn(store, 'getFilesInfo').mockResolvedValue([
      { path: 'C:/tmp/a.txt', name: 'a.txt', ext: '.txt', size: 1, ok: true },
    ]);

    const first = useAttachments();
    await first.addPaths(['C:/tmp/a.txt']);
    const remounted = useAttachments();

    expect(remounted.attachments.value.map(a => a.path)).toEqual(['C:/tmp/a.txt']);
  });

  it('附件信息异步返回时仍写回发起操作的会话', async () => {
    const store = useAppStore();
    store.currentSessionName = 'session-a';
    let release!: (value: any[]) => void;
    vi.spyOn(store, 'getFilesInfo').mockImplementation(() => new Promise(resolve => { release = resolve; }));
    const { addPaths, attachments } = useAttachments();

    const adding = addPaths(['C:/tmp/slow.txt']);
    store.currentSessionName = 'session-b';
    release([{ path: 'C:/tmp/slow.txt', name: 'slow.txt', ext: '.txt', size: 1, ok: true }]);
    await adding;

    expect(attachments.value).toEqual([]);
    store.currentSessionName = 'session-a';
    expect(attachments.value.map(a => a.path)).toEqual(['C:/tmp/slow.txt']);
  });

  it('文件上传异步完成时仍写回发起上传的会话', async () => {
    const store = useAppStore();
    store.currentSessionName = 'session-a';
    let release!: (path: string) => void;
    const upload = vi.spyOn(store, 'uploadFile').mockImplementation(() =>
      new Promise(resolve => { release = resolve; }));
    vi.spyOn(store, 'getFilesInfo').mockImplementation(async paths =>
      paths.map(path => ({ path, name: 'upload.png', ext: '.png', size: 1, ok: true })));
    const { uploadFiles, attachments } = useAttachments();

    const uploading = uploadFiles([makeFile('upload.png', 1)]);
    await vi.waitFor(() => expect(upload).toHaveBeenCalledTimes(1));
    store.currentSessionName = 'session-b';
    release('C:/tmp/upload.png');
    await uploading;

    expect(attachments.value).toEqual([]);
    store.currentSessionName = 'session-a';
    expect(attachments.value.map(a => a.path)).toEqual(['C:/tmp/upload.png']);
  });

  it('系统文件选择返回时仍写回发起选择的会话', async () => {
    const store = useAppStore();
    store.currentSessionName = 'session-a';
    let release!: (paths: string[]) => void;
    vi.spyOn(store, 'pickFiles').mockImplementation(() =>
      new Promise(resolve => { release = resolve; }));
    vi.spyOn(store, 'getFilesInfo').mockImplementation(async paths =>
      paths.map(path => ({ path, name: 'picked.txt', ext: '.txt', size: 1, ok: true })));
    const { pickAttachments, attachments } = useAttachments();

    const picking = pickAttachments();
    store.currentSessionName = 'session-b';
    release(['C:/tmp/picked.txt']);
    await picking;

    expect(attachments.value).toEqual([]);
    store.currentSessionName = 'session-a';
    expect(attachments.value.map(a => a.path)).toEqual(['C:/tmp/picked.txt']);
  });

  it('粘贴图片异步落盘完成时仍写回发起粘贴的会话', async () => {
    const store = useAppStore();
    store.currentSessionName = 'session-a';
    let release!: (path: string) => void;
    const save = vi.spyOn(store, 'saveClipboardImage').mockImplementation(() =>
      new Promise(resolve => { release = resolve; }));
    vi.spyOn(store, 'getFilesInfo').mockImplementation(async paths =>
      paths.map(path => ({ path, name: 'paste.png', ext: '.png', size: 1, ok: true })));
    const { handlePaste, attachments } = useAttachments();
    const image = makeFile('paste.png', 1);
    const event = {
      preventDefault: vi.fn(),
      clipboardData: {
        items: [{ kind: 'file', type: 'image/png', getAsFile: () => image }],
      },
    } as unknown as ClipboardEvent;

    handlePaste(event);
    await vi.waitFor(() => expect(save).toHaveBeenCalledTimes(1));
    store.currentSessionName = 'session-b';
    release('C:/tmp/paste.png');
    await flushPromises();

    expect(attachments.value).toEqual([]);
    store.currentSessionName = 'session-a';
    expect(attachments.value.map(a => a.path)).toEqual(['C:/tmp/paste.png']);
  });

  it('并发拖拽超时时旧桥接路径不会被另一个会话抢走', async () => {
    vi.useFakeTimers();
    const report = vi.fn(async () => ({ paths: ['C:/tmp/b.txt'] }));
    (window as any).pywebview = { api: { report_dropped_files: report } };
    const store = useAppStore();
    vi.spyOn(store, 'getFilesInfo').mockImplementation(async paths =>
      paths.map(path => ({ path, name: path.split('/').pop()!, ext: '.txt', size: 1, ok: true })));
    const boxA = document.createElement('div');
    const boxB = document.createElement('div');
    const draftA = useAttachments();
    const draftB = useAttachments();
    draftA.bindDrop(boxA);
    draftB.bindDrop(boxB);

    store.currentSessionName = 'session-a';
    dispatchDrop(boxA, makeFile('a.txt', 1));
    store.currentSessionName = 'session-b';
    dispatchDrop(boxB, makeFile('b.txt', 1));

    await vi.advanceTimersByTimeAsync(4000);
    await flushPromises();

    store.currentSessionName = 'session-a';
    expect(draftA.attachments.value).toEqual([]);
    store.currentSessionName = 'session-b';
    expect(draftB.attachments.value.map(a => a.path)).toEqual(['C:/tmp/b.txt']);
    expect(report).toHaveBeenCalledTimes(1);
    draftA.unbindDrop();
    draftB.unbindDrop();
  });
});

describe('useAttachments 图片附件缩略图', () => {
  beforeEach(() => {
    setActivePinia(createPinia());
  });

  it('addPaths 后图片路径被写入 thumb，并可通过 openAttachmentImage 打开灯箱', async () => {
    const store = useAppStore();
    vi.spyOn(store, 'getFilesInfo').mockImplementation(async paths =>
      paths.map(path => ({ path, name: path.split('/').pop()!, ext: '.png', size: 1, ok: true })));
    vi.spyOn(store, 'fetchAttachmentThumbnails').mockResolvedValue(
      new Map([['C:/tmp/a.png', 'http://x/u']]));
    const { addPaths, attachments, openAttachmentImage } = useAttachments();

    await addPaths(['C:/tmp/a.png', 'C:/tmp/b.txt']);
    expect(attachments.value[0].thumb).toBe('http://x/u');
    expect(attachments.value.find(a => a.path === 'C:/tmp/b.txt')?.thumb).toBeUndefined();

    openAttachmentImage('C:/tmp/a.png');
    expect(store.lightbox.show).toBe(true);
    expect(store.lightbox.images).toEqual(['http://x/u']);
    expect(store.lightbox.index).toBe(0);
  });

  it('fetchAttachmentThumbnails 抛错时附件仍正常加入（thumb 为 undefined，不抛出）', async () => {
    const store = useAppStore();
    vi.spyOn(store, 'getFilesInfo').mockImplementation(async paths =>
      paths.map(path => ({ path, name: path.split('/').pop()!, ext: '.png', size: 1, ok: true })));
    vi.spyOn(store, 'fetchAttachmentThumbnails').mockRejectedValue(new Error('boom'));
    vi.spyOn(console, 'error').mockImplementation(() => {});
    const { addPaths, attachments } = useAttachments();

    await expect(addPaths(['C:/tmp/a.png'])).resolves.not.toThrow();
    expect(attachments.value.map(a => a.path)).toEqual(['C:/tmp/a.png']);
    expect(attachments.value[0].thumb).toBeUndefined();
  });
});
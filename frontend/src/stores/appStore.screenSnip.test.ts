import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, expect, it, vi } from 'vitest';
import { useAppStore } from './appStore';

const SNIP_PATH = 'C:\\tmp\\ycode_paste\\snip_x.png';

beforeEach(() => {
  setActivePinia(createPinia());
});

it('snip_attachment_ready 加入当前会话输入框附件；无文件信息时 name 退化为 baseName', async () => {
  const store = useAppStore();
  store.currentSessionName = 'session_snip_base';
  const getInfo = vi.spyOn(store, 'getFilesInfo').mockResolvedValue([]);
  vi.spyOn(store, 'fetchAttachmentThumbnails').mockResolvedValue(new Map());
  const notify = vi.spyOn(store, 'notifyChat').mockImplementation(() => {});

  store.handleBridgeEvent({ type: 'snip_attachment_ready', data: { path: SNIP_PATH } } as any);

  await vi.waitFor(() => expect(store._getSessionRuntime().composerAttachments).toHaveLength(1));
  const item = store._getSessionRuntime().composerAttachments![0];
  expect(item.path).toBe(SNIP_PATH);
  expect(item.name).toBe('snip_x.png');
  expect(item.ext).toBe('png');
  expect(item.size).toBeNull();
  expect(getInfo).toHaveBeenCalledWith([SNIP_PATH]);
  expect(notify).toHaveBeenCalledWith('截图已加入输入框附件');
});

it('getFilesInfo 可用时附件 name/ext/size 采用桥返回值', async () => {
  const store = useAppStore();
  store.currentSessionName = 'session_snip_info';
  vi.spyOn(store, 'getFilesInfo').mockResolvedValue([
    { path: SNIP_PATH, name: '截个图.png', ext: 'png', size: 123, ok: true },
  ]);
  vi.spyOn(store, 'fetchAttachmentThumbnails').mockResolvedValue(new Map());
  vi.spyOn(store, 'notifyChat').mockImplementation(() => {});

  store.handleBridgeEvent({ type: 'snip_attachment_ready', data: { path: SNIP_PATH } } as any);

  await vi.waitFor(() => expect(store._getSessionRuntime().composerAttachments).toHaveLength(1));
  const item = store._getSessionRuntime().composerAttachments![0];
  expect(item.name).toBe('截个图.png');
  expect(item.ext).toBe('png');
  expect(item.size).toBe(123);
});

it('重复事件去重：同一路径不叠加 chip，且不重复查文件信息/提示', async () => {
  const store = useAppStore();
  store.currentSessionName = 'session_snip_dedup';
  const getInfo = vi.spyOn(store, 'getFilesInfo').mockResolvedValue([
    { path: SNIP_PATH, name: 'snip_x.png', ext: 'png', size: 1, ok: true },
  ]);
  vi.spyOn(store, 'fetchAttachmentThumbnails').mockResolvedValue(new Map());
  const notify = vi.spyOn(store, 'notifyChat').mockImplementation(() => {});

  store.handleBridgeEvent({ type: 'snip_attachment_ready', data: { path: SNIP_PATH } } as any);
  await vi.waitFor(() => expect(store._getSessionRuntime().composerAttachments).toHaveLength(1));

  store.handleBridgeEvent({ type: 'snip_attachment_ready', data: { path: SNIP_PATH } } as any);
  await Promise.resolve();
  await Promise.resolve();

  expect(store._getSessionRuntime().composerAttachments).toHaveLength(1);
  expect(getInfo).toHaveBeenCalledTimes(1);
  expect(notify).toHaveBeenCalledTimes(1);
});

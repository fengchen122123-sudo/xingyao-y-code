import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, expect, it, vi } from 'vitest';
import { useAppStore } from './appStore';

// 屏幕截图「发到输入框」：宿主（gui_snip_mixin）经 snip_attachment_ready 桥事件
// 回传图片路径，handleBridgeEvent → _attachSnipToComposer 把路径加入当前会话
// 输入框附件：去重 → getFilesInfo 补全 name/ext/size（失败退化为 baseName）→
// fetchAttachmentThumbnails 补缩略图（静默）→ notifyChat 提示。

// C:\tmp\ycode_paste\snip_x.png（JS 字符串里用 \\ 表示反斜杠）
const SNIP_PATH = 'C:\\tmp\\ycode_paste\\snip_x.png';

beforeEach(() => {
  setActivePinia(createPinia());
});

it('snip_attachment_ready 加入当前会话输入框附件；无文件信息时 name 退化为 baseName', async () => {
  const store = useAppStore();
  store.currentSessionName = 'session_snip_base';
  // 文件信息查询失败（空列表）：name 退化为 baseName，ext 走 fileExt
  const getInfo = vi.spyOn(store, 'getFilesInfo').mockResolvedValue([]);
  vi.spyOn(store, 'fetchAttachmentThumbnails').mockResolvedValue(new Map());
  const notify = vi.spyOn(store, 'notifyChat').mockImplementation(() => {});

  store.handleBridgeEvent({ type: 'snip_attachment_ready', data: { path: SNIP_PATH } } as any);

  // _attachSnipToComposer 是 void 异步调用：等其一内部微任务落库后再断言
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

  // 事件重放/重复截图同一文件：目标已存在该路径，直接短路
  store.handleBridgeEvent({ type: 'snip_attachment_ready', data: { path: SNIP_PATH } } as any);
  await Promise.resolve();
  await Promise.resolve();

  expect(store._getSessionRuntime().composerAttachments).toHaveLength(1);
  expect(getInfo).toHaveBeenCalledTimes(1);
  expect(notify).toHaveBeenCalledTimes(1);
});

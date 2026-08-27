import { describe, expect, it, vi, beforeEach } from 'vitest';
import { setActivePinia, createPinia } from 'pinia';
import { useAttachments } from './useAttachments';
import { useAppStore } from '../stores/appStore';

function makeFile(name: string, size: number): File {
  const f = new File(['x'], name);
  Object.defineProperty(f, 'size', { value: size });
  return f;
}

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
    const { uploadFiles, attachments } = useAttachments();
    await uploadFiles([makeFile('a.jpg', 3)]);
    expect(attachments.value.map(a => a.path)).toContain('C:/tmp/upload_a.jpg');
  });
});

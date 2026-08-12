import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { useAttachments } from './useAttachments';
import { useAppStore } from '../stores/appStore';

// 模拟 DragEvent：dataTransfer.files 只带 name/size（与 pywebview 环境一致，
// JS 侧 File 对象拿不到完整路径，路径由 Python 侧 __ycode_on_drop_paths 推送）
function fakeDropEvent(names: string[]): DragEvent {
  const files = names.map((name, i) => ({ name, size: 100 + i }));
  return {
    preventDefault: () => {},
    dataTransfer: {
      files,
      getData: () => '',
      types: ['Files'],
    },
  } as unknown as DragEvent;
}

function pushPaths(files: { name: string; path: string }[]) {
  (window as unknown as { __ycode_on_drop_paths: (p: unknown) => void })
    .__ycode_on_drop_paths({ files });
}

beforeEach(() => {
  setActivePinia(createPinia());
  vi.useFakeTimers();
});

describe('useAttachments 拖拽流程', () => {
  it('拖入后立即显示 loading 占位，路径推送后替换为真实附件', async () => {
    const store = useAppStore();
    store.getFilesInfo = vi.fn(async (paths: string[]) =>
      paths.map(p => ({ path: p, name: p.split(/[\\/]/).pop()!, ext: 'pdf', size: 123, ok: true })));

    const { attachments, bindDrop, unbindDrop } = useAttachments();
    const el = document.createElement('div');
    bindDrop(el);

    const fire = (names: string[]) => el.dispatchEvent(Object.assign(new Event('drop'), {
      dataTransfer: (fakeDropEvent(names) as { dataTransfer: unknown }).dataTransfer,
      preventDefault: () => {},
    }));
    fire(['报告.pdf']);

    // 立即上屏 loading 占位
    expect(attachments.value.length).toBe(1);
    expect(attachments.value[0].name).toBe('报告.pdf');
    expect(attachments.value[0].loading).toBe(true);

    // Python 侧推送完整路径
    pushPaths([{ name: '报告.pdf', path: 'D:/docs/报告.pdf' }]);
    await vi.advanceTimersByTimeAsync(0);

    expect(store.getFilesInfo).toHaveBeenCalledWith(['D:/docs/报告.pdf']);
    expect(attachments.value.length).toBe(1);
    expect(attachments.value[0].path).toBe('D:/docs/报告.pdf');
    expect(attachments.value[0].loading).not.toBe(true);

    unbindDrop();
  });

  it('连续拖拽两次按文件名匹配，不会串单', async () => {
    const store = useAppStore();
    store.getFilesInfo = vi.fn(async (paths: string[]) =>
      paths.map(p => ({ path: p, name: p.split(/[\\/]/).pop()!, ext: 'txt', size: 1, ok: true })));

    const { attachments, bindDrop, unbindDrop } = useAttachments();
    const el = document.createElement('div');
    bindDrop(el);

    const fire = (names: string[]) => el.dispatchEvent(Object.assign(new Event('drop'), {
      dataTransfer: (fakeDropEvent(names) as { dataTransfer: unknown }).dataTransfer,
      preventDefault: () => {},
    }));

    fire(['A.txt']);
    fire(['B.txt']);
    expect(attachments.value.filter(a => a.loading).length).toBe(2);

    // B 的路径先推送到：应按名字匹配到第二个等待者
    pushPaths([{ name: 'B.txt', path: 'D:/B.txt' }]);
    await vi.advanceTimersByTimeAsync(0);
    pushPaths([{ name: 'A.txt', path: 'D:/A.txt' }]);
    await vi.advanceTimersByTimeAsync(0);

    const paths = attachments.value.map(a => a.path);
    expect(paths).toContain('D:/A.txt');
    expect(paths).toContain('D:/B.txt');
    expect(attachments.value.every(a => !a.loading)).toBe(true);

    unbindDrop();
  });

  it('路径推送超时后清除占位并提示', async () => {
    const store = useAppStore();
    const toast = vi.fn();
    store.showToast = toast as unknown as typeof store.showToast;

    const { attachments, bindDrop, unbindDrop } = useAttachments();
    const el = document.createElement('div');
    bindDrop(el);

    el.dispatchEvent(Object.assign(new Event('drop'), {
      dataTransfer: (fakeDropEvent(['C.txt']) as { dataTransfer: unknown }).dataTransfer,
      preventDefault: () => {},
    }));
    expect(attachments.value.length).toBe(1);

    await vi.advanceTimersByTimeAsync(5000);

    expect(attachments.value.length).toBe(0);
    expect(toast).toHaveBeenCalled();

    unbindDrop();
  });
});

import { beforeEach, describe, expect, it } from 'vitest';
import { createPinia, setActivePinia } from 'pinia';

import { useAppStore } from './appStore';

// 拖放路径推送（GUI 线程死锁修复后走 _js_queue 事件管线）：
// store 收到 dropped_paths_push 必须转发给 useAttachments 挂在 window 上的
// __ycode_on_drop_paths，且不触碰会话 runtime 状态。
describe('dropped_paths_push bridge event', () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    delete (window as any).__ycode_on_drop_paths;
  });

  it('forwards the payload to the window drop-path handler', () => {
    const store = useAppStore();
    const received: unknown[] = [];
    (window as any).__ycode_on_drop_paths = (payload: unknown) => received.push(payload);

    store.handleBridgeEvent({
      type: 'dropped_paths_push',
      data: { files: [{ name: 'a.py', path: 'C:/tmp/a.py' }] },
    });

    expect(received).toHaveLength(1);
    expect(received[0]).toMatchObject({
      files: [{ name: 'a.py', path: 'C:/tmp/a.py' }],
    });
  });

  it('is a no-op when no drop handler is bound (input areas not mounted)', () => {
    const store = useAppStore();
    expect(() => store.handleBridgeEvent({
      type: 'dropped_paths_push',
      data: { files: [] },
    })).not.toThrow();
    // 不得误标连接或写入会话运行态
    expect(store.runningSessions).toEqual([]);
  });
});

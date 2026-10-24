import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, describe, expect, it } from 'vitest';

import { useAppStore } from './appStore';

// decision_recorded 桥事件：入口与徽章口径必须是「当前会话 + 真实业务决策」。
// 探针（scene="probe"）只证明链路通了，不算真实决策——与后端
// has_business_records 的口径一致；否则会出现「跑一次探针点亮入口，
// 下次启动探测又把它熄灭」的闪烁。

beforeEach(() => {
  setActivePinia(createPinia());
  (window as any).pywebview = undefined;
});

describe('decision_recorded 事件', () => {
  it('当前会话的业务决策：累加未读并点亮入口', () => {
    const store = useAppStore();
    store.currentSessionName = 'sess-A';

    store.handleBridgeEvent({
      type: 'decision_recorded',
      data: { scene: 'router', session_id: 'sess-A' },
    } as any);

    expect(store.decisionEventTick).toBe(1);
    expect(store.decisionUnread).toBe(1);
    expect(store.decisionHasRecords).toBe(true);
  });

  it('探针记录：只刷新面板，不累加未读、不点亮入口', () => {
    const store = useAppStore();
    store.currentSessionName = 'sess-A';

    store.handleBridgeEvent({
      type: 'decision_recorded',
      data: { scene: 'probe', session_id: 'sess-A' },
    } as any);

    expect(store.decisionEventTick).toBe(1);
    expect(store.decisionUnread).toBe(0);
    expect(store.decisionHasRecords).toBe(false);
  });

  it('其他会话的决策：只刷新面板，不影响当前会话的未读与入口', () => {
    const store = useAppStore();
    store.currentSessionName = 'sess-A';

    store.handleBridgeEvent({
      type: 'decision_recorded',
      data: { scene: 'router', session_id: 'sess-B' },
    } as any);

    expect(store.decisionEventTick).toBe(1);
    expect(store.decisionUnread).toBe(0);
    expect(store.decisionHasRecords).toBe(false);
  });

  it('旧后端缺省 scene：按业务决策处理（不能因为缺字段就不提示）', () => {
    const store = useAppStore();
    store.currentSessionName = 'sess-A';

    store.handleBridgeEvent({
      type: 'decision_recorded',
      data: { session_id: 'sess-A' },
    } as any);

    expect(store.decisionUnread).toBe(1);
    expect(store.decisionHasRecords).toBe(true);
  });
});

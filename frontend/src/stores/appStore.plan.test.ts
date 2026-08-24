import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, describe, expect, it } from 'vitest';
import { useAppStore } from './appStore';

beforeEach(() => setActivePinia(createPinia()));

describe('appStore plan panel', () => {
  it('空 items 快照清空计划面板（特性测试：改动前后都必须成立）', () => {
    const store = useAppStore();
    store.currentSessionName = 's1';
    store.planData = {
      title: '当前任务计划',
      items: [{ id: 'a', text: '任务A', status: 'done' }] as any,
      progress: 100,
    };

    store.handleBridgeEvent({
      type: 'plan_updated',
      data: { title: '当前任务计划', items: [], session_name: 's1' },
    } as any);

    expect(store.planData.items).toEqual([]);
    expect(store.planData.progress).toBe(0);
  });

  it('items 非数组时忽略事件且不抛错（现状会 .map 抛 TypeError）', () => {
    const store = useAppStore();
    store.currentSessionName = 's1';
    store.planData = {
      title: '当前任务计划',
      items: [{ id: 'a', text: '任务A', status: 'done' }] as any,
      progress: 100,
    };

    expect(() => store.handleBridgeEvent({
      type: 'plan_updated',
      data: { title: '当前任务计划', items: {}, session_name: 's1' },
    } as any)).not.toThrow();

    // 非数组载荷不进入分支，面板保持原状
    expect(store.planData.items).toHaveLength(1);
  });
});

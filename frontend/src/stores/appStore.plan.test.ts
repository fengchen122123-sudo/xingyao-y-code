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

describe('appStore plan_item_handoff', () => {
  it('更新本会话计划项 execution_mode/owner 且不污染并行会话', () => {
    const store = useAppStore();
    store.currentSessionName = 's1';
    store.planData = {
      title: '计划',
      items: [{ id: 'a', text: '任务A', status: 'in_progress' } as any],
      progress: 0,
    };
    store._captureRuntime('s1');

    store.handleBridgeEvent({
      type: 'plan_item_handoff',
      data: {
        item: {
          id: 'a', subject: '任务A', status: 'in_progress',
          execution_mode: 'main_takeover', owner: 'main:turn-1',
        },
        source_task_id: 'subagent-abcd1234',
        mode: 'main_takeover',
        queue_position: 0,
        session_name: 's1',
      },
    } as any);

    expect((store.planData.items[0] as any).execution_mode).toBe('main_takeover');
    expect((store.planData.items[0] as any).owner).toBe('main:turn-1');
    expect((store.planData.items[0] as any).handoff_note).toContain('主 Agent 正在接管');

    // 并行会话 s2 的同名计划项事件绑定到 s2 运行时，不覆盖 s1 的接管状态
    store.handleBridgeEvent({
      type: 'plan_item_handoff',
      data: {
        item: {
          id: 'a', subject: '任务A', status: 'in_progress',
          execution_mode: 'takeover_queued', owner: '',
        },
        source_task_id: 'subagent-ef012345',
        mode: 'takeover_queued',
        queue_position: 2,
        session_name: 's2',
      },
    } as any);

    // s2 事件只写入 s2 运行时（reducer 处理完非当前会话事件后恢复当前视图）
    const s2Plan = (store.sessionRuntimeByName['s2'] as any).planData;
    expect((s2Plan.items[0] as any).execution_mode).toBe('takeover_queued');
    expect((s2Plan.items[0] as any).handoff_note).toContain('队列第 2 项');

    // 当前视图（s1）未被 s2 事件污染
    expect((store.planData.items[0] as any).execution_mode).toBe('main_takeover');
    store._bindSessionRuntime('s1');
    expect((store.planData.items[0] as any).execution_mode).toBe('main_takeover');
  });

  it('plan_updated 保留完整接管事实并可跨会话恢复', () => {
    const store = useAppStore();
    store.currentSessionName = 's1';

    store.handleBridgeEvent({
      type: 'plan_item_handoff',
      data: {
        item: {
          id: 'a', subject: '任务A', status: 'in_progress',
          execution_mode: 'main_takeover', owner: 'main:turn-1',
          takeover_required: true, handoff_reason: 'timeout', takeover_sequence: 3,
        },
        mode: 'main_takeover', queue_position: 0, session_name: 's1',
      },
    } as any);
    store.handleBridgeEvent({
      type: 'plan_updated',
      data: {
        session_name: 's1',
        items: [{
          id: 'a', subject: '任务A', status: 'in_progress',
          execution_mode: 'main_takeover', owner: 'main:turn-1',
          takeover_required: true, handoff_reason: 'timeout', takeover_sequence: 3,
        }],
      },
    } as any);

    const item = store.planData.items[0] as any;
    expect(item.execution_mode).toBe('main_takeover');
    expect(item.takeover_required).toBe(true);
    expect(item.handoff_reason).toBe('timeout');
    expect(item.takeover_sequence).toBe(3);
    expect(item.handoff_note).toContain('主 Agent 正在接管');

    store._captureRuntime('s1');
    store._bindSessionRuntime('s2');
    store._bindSessionRuntime('s1');
    expect((store.planData.items[0] as any).handoff_note).toContain('主 Agent 正在接管');
  });

  it('plan_updated 可从 queued 快照派生队列文案并在晋升后更新', () => {
    const store = useAppStore();
    store.currentSessionName = 's1';
    const base = {
      id: 'a', subject: '任务A', status: 'pending', owner: '',
      takeover_required: true, handoff_reason: 'timeout', takeover_sequence: 7,
    };

    store.handleBridgeEvent({
      type: 'plan_updated',
      data: {
        session_name: 's1',
        items: [{ ...base, execution_mode: 'takeover_queued' }],
      },
    } as any);
    expect((store.planData.items[0] as any).handoff_note).toContain('队列第 1 项');

    store.handleBridgeEvent({
      type: 'plan_updated',
      data: {
        session_name: 's1',
        items: [{
          ...base, status: 'in_progress', execution_mode: 'main_takeover',
          owner: 'main:turn-2',
        }],
      },
    } as any);
    expect((store.planData.items[0] as any).handoff_note).toContain('主 Agent 正在接管');
    expect((store.planData.items[0] as any).handoff_note).not.toContain('队列');
  });

  it('终态快照清除活动接管文案但保留接管历史字段', () => {
    const store = useAppStore();
    store.currentSessionName = 's1';
    store.handleBridgeEvent({
      type: 'plan_updated',
      data: {
        session_name: 's1',
        items: [{
          id: 'a', subject: '任务A', status: 'failed',
          execution_mode: 'main_takeover', owner: '',
          takeover_required: true, handoff_reason: 'verification failed',
          takeover_sequence: 4,
        }],
      },
    } as any);

    const item = store.planData.items[0] as any;
    expect(item.handoff_note).toBeUndefined();
    expect(item.takeover_required).toBe(true);
    expect(item.handoff_reason).toBe('verification failed');
    expect(item.takeover_sequence).toBe(4);
  });

  it('GUI 文案固定：active 与 queued 各有固定提示', () => {
    const store = useAppStore();
    store.currentSessionName = 's1';
    store.planData = {
      title: '计划',
      items: [{ id: 'a', text: '任务A', status: 'in_progress' } as any],
      progress: 0,
    };

    store.handleBridgeEvent({
      type: 'plan_item_handoff',
      data: {
        item: { id: 'a', subject: '任务A', execution_mode: 'main_takeover', owner: 'main:t' },
        source_task_id: 'subagent-x',
        mode: 'main_takeover',
        queue_position: 0,
        session_name: 's1',
      },
    } as any);
    expect((store.planData.items[0] as any).handoff_note).toBe(
      '子 Agent 已超时，主 Agent 正在接管并核查现场'
    );
  });
});

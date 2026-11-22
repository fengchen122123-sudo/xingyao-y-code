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

    const s2Plan = (store.sessionRuntimeByName['s2'] as any).planData;
    expect((s2Plan.items[0] as any).execution_mode).toBe('takeover_queued');
    expect((s2Plan.items[0] as any).handoff_note).toContain('队列第 2 项');

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
    expect((store.planData.items[0] as any).handoff_task_id).toBe('subagent-x');
  });

  it('plan_updated 保留阶段、依赖、文件、验收、结果/报错与异常态映射', () => {
    const store = useAppStore();
    store.currentSessionName = 's1';
    store.handleBridgeEvent({
      type: 'plan_updated',
      data: {
        session_name: 's1',
        title: '端到端改造',
        items: [
          {
            id: 'P1',
            subject: '梳理接口',
            status: 'completed',
            stage: 'plan',
            files: ['a.ts', 'b.ts'],
            result: '已梳理完毕',
            attempts: 1,
          },
          {
            id: 'P2',
            subject: '编写测试',
            status: 'blocked',
            stage: 'review',
            depends_on: ['P1'],
            verification: 'npm test',
            verification_type: 'command',
            attempts: 2,
            error: '依赖未就绪',
            handoff_task_id: 'sub-22',
          },
          {
            id: 'P3',
            subject: '清理脚本',
            status: 'cancelled',
          },
        ],
      },
    } as any);

    const [p1, p2, p3] = store.planData.items;
    expect(p1.status).toBe('done');
    expect(p1.stage).toBe('plan');
    expect(p1.files).toEqual(['a.ts', 'b.ts']);
    expect(p1.result).toBe('已梳理完毕');

    expect(p2.status).toBe('blocked');
    expect(p2.stage).toBe('review');
    expect(p2.depends_on).toEqual(['P1']);
    expect(p2.verification).toBe('npm test');
    expect(p2.verification_type).toBe('command');
    expect(p2.attempts).toBe(2);
    expect(p2.error).toBe('依赖未就绪');
    expect(p2.handoff_task_id).toBe('sub-22');

    expect(p3.status).toBe('cancelled');
  });
});

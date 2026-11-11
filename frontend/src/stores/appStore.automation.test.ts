import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, expect, it, vi } from 'vitest';
import { useAppStore } from './appStore';

beforeEach(() => setActivePinia(createPinia()));

const _task = {
  id: 'task-1',
  name: '每日摘要',
  enabled: true,
  trigger_type: 'daily',
  trigger_config: { time: '09:30' },
  target_session: '星轨自动化任务',
  session_mode: 'fixed',
  prompt: '汇总',
  permission_mode: 'confirm',
  missed_run_policy: 'skip',
  notify_on_success: false,
  created_at: '2026-09-12 10:00:00',
  updated_at: '2026-09-12 10:00:00',
  last_run_at: null,
  next_run_at: null,
  trigger_summary: '每天 09:30',
} as any;

const _run = (id: string, status: string) => ({
  id,
  task_id: 'task-1',
  task_name: '每日摘要',
  trigger_kind: 'manual',
  status,
  session_name: '星轨自动化任务',
  turn_id: null,
  error: '',
  queued_at: '2026-09-12 10:00:00',
  started_at: null,
  finished_at: null,
});

it('automation_tasks_changed 事件幂等替换任务列表', () => {
  const store = useAppStore();

  store.handleBridgeEvent({ type: 'automation_tasks_changed', data: { tasks: [_task] } } as any);
  expect(store.automationTasks).toHaveLength(1);
  expect(store.automationTasks[0].name).toBe('每日摘要');

  // 后端全量快照覆盖本地（不增量合并）
  store.handleBridgeEvent({ type: 'automation_tasks_changed', data: { tasks: [] } } as any);
  expect(store.automationTasks).toHaveLength(0);
});

it('automation_run_updated 插入历史头部、按 id 幂等替换并封顶 50 条', () => {
  const store = useAppStore();

  store.handleBridgeEvent({ type: 'automation_run_updated', data: { run: _run('r1', 'queued') } } as any);
  store.handleBridgeEvent({ type: 'automation_run_updated', data: { run: _run('r2', 'running') } } as any);
  expect(store.automationRuns['task-1'].map(r => r.id)).toEqual(['r2', 'r1']);

  // 同 id 状态更新：原位替换，不重复插入
  store.handleBridgeEvent({ type: 'automation_run_updated', data: { run: _run('r1', 'completed') } } as any);
  expect(store.automationRuns['task-1'].map(r => r.id)).toEqual(['r2', 'r1']);
  expect(store.automationRuns['task-1'].find(r => r.id === 'r1')?.status).toBe('completed');

  // 上限 50 条
  for (let i = 0; i < 55; i++) {
    store.handleBridgeEvent({
      type: 'automation_run_updated',
      data: { run: _run(`bulk-${i}`, 'completed') },
    } as any);
  }
  expect(store.automationRuns['task-1'].length).toBe(50);
});

it('无 task_id 的运行行被忽略', () => {
  const store = useAppStore();
  store.handleBridgeEvent({
    type: 'automation_run_updated',
    data: { run: { ..._run('r9', 'error'), task_id: '' } },
  } as any);
  expect(store.automationRuns).toEqual({});
});

it('saveAutomationTask 校验错误透传、成功后刷新任务', async () => {
  const store = useAppStore();
  const api = {
    save_automation_task: vi.fn()
      .mockResolvedValueOnce({ ok: false, errors: ['任务名称不能为空'] })
      .mockResolvedValueOnce({ ok: true, id: 'task-9' }),
    get_automation_tasks: vi.fn().mockResolvedValue({ ok: true, tasks: [_task] }),
  };
  (window as any).pywebview = { api };

  const bad = await store.saveAutomationTask({ name: '' });
  expect(bad.ok).toBe(false);
  expect(bad.errors).toEqual(['任务名称不能为空']);
  expect(api.get_automation_tasks).not.toHaveBeenCalled();

  const good = await store.saveAutomationTask({ name: '每日摘要' });
  expect(good.ok).toBe(true);
  expect(api.get_automation_tasks).toHaveBeenCalledTimes(1);
  expect(store.automationTasks).toHaveLength(1);
});

it('runAutomationTaskNow 单飞拒绝时弹提示', async () => {
  const store = useAppStore();
  const notifySpy = vi.fn();
  store.notifyModal = notifySpy as any;
  const api = {
    run_automation_task_now: vi.fn().mockResolvedValue({ ok: false, msg: '上一次运行尚未结束，请稍后再试' }),
  };
  (window as any).pywebview = { api };

  const res = await store.runAutomationTaskNow('task-1');
  expect(res.ok).toBe(false);
  expect(notifySpy).toHaveBeenCalledWith('星轨自动化任务', '上一次运行尚未结束，请稍后再试');
});

it('loadAutomationRuns 拉取并写入对应任务历史', async () => {
  const store = useAppStore();
  const api = {
    get_automation_runs: vi.fn().mockResolvedValue({ ok: true, runs: [_run('r1', 'completed')] }),
  };
  (window as any).pywebview = { api };

  await store.loadAutomationRuns('task-1');
  expect(api.get_automation_runs).toHaveBeenCalledWith('task-1', 50);
  expect(store.automationRuns['task-1']).toHaveLength(1);
});

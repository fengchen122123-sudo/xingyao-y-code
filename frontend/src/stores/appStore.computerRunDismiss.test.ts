import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, expect, it } from 'vitest';
import { useAppStore } from './appStore';

beforeEach(() => {
  setActivePinia(createPinia());
});

it('clearComputerRun 清空运行态与轨迹并记录关闭黑名单', () => {
  const store = useAppStore();
  store.reduceComputerEvent({ type: 'computer_state', run_id: 'run-1', event_seq: 1, status: 'running' });
  store.reduceComputerEvent({
    type: 'computer_action_started', run_id: 'run-1', event_seq: 2, action_ordinal: 1, action_kind: 'click',
  });
  expect(store.computerTrace).toHaveLength(1);

  store.clearComputerRun();

  expect(store.computerRun.run_id).toBe('');
  expect(store.computerRun.status).toBe('idle');
  expect(store.computerTrace).toEqual([]);
  expect(store._computerClosedRunId).toBe('run-1');
});

it('已关闭 run 的迟到事件不再复活运行态', () => {
  const store = useAppStore();
  store.reduceComputerEvent({ type: 'computer_state', run_id: 'run-1', event_seq: 5, status: 'stopped' });
  store.clearComputerRun();

  store.reduceComputerEvent({ type: 'computer_observation', run_id: 'run-1', event_seq: 6, preview_url: '/late' });
  store.reduceComputerEvent({ type: 'computer_state', run_id: 'run-1', event_seq: 7, status: 'running' });

  expect(store.computerRun.run_id).toBe('');
  expect(store.computerRun.preview_url).toBe('');
  expect(store.computerRun.status).toBe('idle');
});

it('已关闭 run 的快照不复活运行态，新 run 快照正常生效', () => {
  const store = useAppStore();
  store.reduceComputerEvent({ type: 'computer_state', run_id: 'run-1', event_seq: 1, status: 'completed' });
  store.clearComputerRun();

  store._applyComputerSnapshot({ run_id: 'run-1', status: 'completed' });
  expect(store.computerRun.run_id).toBe('');

  store._applyComputerSnapshot({ run_id: 'run-2', status: 'running' });
  expect(store.computerRun.run_id).toBe('run-2');
  expect(store.computerRun.status).toBe('running');
});

it('关闭后新 run 事件正常建 run，轨迹从空开始', () => {
  const store = useAppStore();
  store.reduceComputerEvent({ type: 'computer_state', run_id: 'run-1', event_seq: 1, status: 'running' });
  store.reduceComputerEvent({
    type: 'computer_action_started', run_id: 'run-1', event_seq: 2, action_ordinal: 1, action_kind: 'click',
  });
  store.clearComputerRun();

  store.reduceComputerEvent({ type: 'computer_state', run_id: 'run-2', event_seq: 1, status: 'starting' });
  expect(store.computerRun.run_id).toBe('run-2');
  expect(store.computerTrace).toEqual([]);
});

it('computerRunTerminal 只对 completed / stopped / failed 返回 true', () => {
  const store = useAppStore();
  const statuses: Array<[string, boolean]> = [
    ['idle', false],
    ['starting', false],
    ['running', false],
    ['paused', false],
    ['waiting_for_user', false],
    ['provider_safety_required', false],
    ['stopping', false],
    ['completed', true],
    ['stopped', true],
    ['failed', true],
  ];
  for (const [status, expected] of statuses) {
    store.computerRun = { ...store.computerRun, run_id: 'run-1', status };
    expect(store.computerRunTerminal).toBe(expected);
  }
});

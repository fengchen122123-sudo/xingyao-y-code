import { beforeEach, expect, it, vi } from 'vitest';
import { createPinia, setActivePinia } from 'pinia';

import { useAppStore } from './appStore';
import { useIdeDebugStore } from './ideDebugStore';
import { onDapOutput } from '../utils/dapBus';
import { onPtyData, onPtyExit } from '../utils/ptyBus';

beforeEach(() => {
  setActivePinia(createPinia());
  (window as any).pywebview = { api: {} };
});

it('routes dap_output to the bus without touching reactive state', () => {
  const app = useAppStore();
  const debug = useIdeDebugStore();
  const seen: string[] = [];
  const off = onDapOutput((payload) => seen.push(payload.text));
  const before = JSON.stringify(debug.$state);

  app.handleBridgeEvent({ type: 'dap_output', data: { session_id: 'dap-1', category: 'stdout', text: 'hi\n' } });

  expect(seen).toEqual(['hi\n']);
  expect(debug.frames).toEqual([]);
  // 高频输出必须走旁路：状态仓一个字节都不该变（否则每行输出都触发组件更新）
  expect(JSON.stringify(debug.$state)).toBe(before);
  off();
});

it('routes dap_stopped into the debug store', () => {
  const app = useAppStore();
  const debug = useIdeDebugStore();

  app.handleBridgeEvent({
    type: 'dap_stopped',
    data: { session_id: 'dap-1', reason: 'breakpoint', thread_id: 1, frame_id: 1001, path: 'E:/p/app.py', line: 7 },
  });

  expect(debug.debugState).toBe('paused');
  expect(debug.stopLine).toBe(7);
  expect(debug.threadId).toBe(1);
});

it('routes the rest of the dap_* family into the debug store', () => {
  const app = useAppStore();
  const debug = useIdeDebugStore();

  app.handleBridgeEvent({ type: 'dap_status', data: { session_id: 'dap-1', state: 'running', msg: '' } });
  expect(debug.debugState).toBe('running');

  app.handleBridgeEvent({ type: 'dap_breakpoint_changed', data: { path: 'E:/p/app.py', line: 7, verified: true } });
  expect(debug.breakpointStates['E:/p/app.py:7']).toBe(true);

  app.handleBridgeEvent({ type: 'dap_continued', data: { session_id: 'dap-1', thread_id: 1 } });
  expect(debug.debugState).toBe('running');

  app.handleBridgeEvent({ type: 'dap_terminated', data: { session_id: 'dap-1' } });
  expect(debug.debugState).toBe('stopped');
  expect(debug.active).toBe(false);
});

it('treats repeated dap events as idempotent (no state jitter)', () => {
  const app = useAppStore();
  const debug = useIdeDebugStore();

  app.handleBridgeEvent({
    type: 'dap_stopped',
    data: { session_id: 'dap-1', thread_id: 1, frame_id: 1001, path: 'E:/p/app.py', line: 7 },
  });
  const frames = debug.frames;

  app.handleBridgeEvent({
    type: 'dap_stopped',
    data: { session_id: 'dap-1', thread_id: 1, frame_id: 1001, path: 'E:/p/app.py', line: 7 },
  });

  expect(debug.frames).toBe(frames); // 重复事件不换数组引用 → 不触发无谓更新
  expect(debug.debugState).toBe('paused');
  expect(debug.stopLine).toBe(7);
});

it('ignores unknown dap_* events instead of throwing', () => {
  const app = useAppStore();
  const debug = useIdeDebugStore();

  // 桥可能先于前端加新事件（后端要透传 thread/process 这类还未接的事件）
  expect(() => app.handleBridgeEvent({ type: 'dap_thread', data: { session_id: 'dap-1' } })).not.toThrow();
  expect(debug.debugState).toBe('idle');
});

it('keeps pty routing intact after adding dap branches', () => {
  const app = useAppStore();
  const seen: string[] = [];
  const exits: number[] = [];
  const offData = onPtyData((t) => seen.push(t));
  const offExit = onPtyExit((c) => exits.push(c));

  app.handleBridgeEvent({ type: 'pty_data', data: { text: '$ dir\r\n' } });
  app.handleBridgeEvent({ type: 'pty_exit', data: { code: 3 } });

  expect(seen).toEqual(['$ dir\r\n']);
  expect(exits).toEqual([3]);

  offData();
  offExit();
});

it('does not let a throwing output subscriber break bridge event handling', () => {
  const app = useAppStore();
  const debug = useIdeDebugStore();
  const errors = vi.spyOn(console, 'error').mockImplementation(() => {});
  const offBad = onDapOutput(() => {
    throw new Error('订阅者自己炸了');
  });

  expect(() =>
    app.handleBridgeEvent({ type: 'dap_output', data: { session_id: 'dap-1', category: 'stdout', text: 'x' } }),
  ).not.toThrow();

  errors.mockRestore();
  offBad();
  // 事件泵没被打断：下一条 dap_* 照常进状态仓
  app.handleBridgeEvent({ type: 'dap_terminated', data: { session_id: 'dap-1' } });
  expect(debug.debugState).toBe('stopped');
});

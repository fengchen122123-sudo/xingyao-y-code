import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, expect, it, vi } from 'vitest';
import { nextTick } from 'vue';
import { mount } from '@vue/test-utils';

import ComputerControlBar from './ComputerControlBar.vue';
import { useAppStore } from '../stores/appStore';

// 前端测试一律 mock window.pywebview.api（无真实 Bridge）
const api = {
  get_computer_control_status: vi.fn(),
  pause_computer_control: vi.fn(),
  resume_computer_control: vi.fn(),
  stop_computer_control: vi.fn(),
};

beforeEach(() => {
  vi.clearAllMocks();
  setActivePinia(createPinia());
  (window as any).pywebview = { api };
});

function piniaWithRunningComputer() {
  const pinia = createPinia();
  setActivePinia(pinia);
  const store = useAppStore();
  store.computerRun = {
    ...store.computerRun,
    run_id: 'run-1',
    status: 'running',
    target: 'browser',
    browser: 'edge',
    action_count: 3,
    current_surface: 'https://example.com/docs',
  };
  return { plugins: [pinia] };
}

function piniaWithRunningDesktop() {
  const pinia = createPinia();
  setActivePinia(pinia);
  const store = useAppStore();
  store.computerRun = {
    ...store.computerRun,
    run_id: 'run-1',
    status: 'running',
    target: 'desktop',
    action_count: 3,
    current_surface: '记事本 - 未命名',
  };
  return { plugins: [pinia] };
}

it('rejects late events from old runs and older event_seq', () => {
  const store = useAppStore();
  store.reduceComputerEvent({ type: 'computer_state', run_id: 'run-2', event_seq: 1, status: 'running' });
  store.reduceComputerEvent({ type: 'computer_observation', run_id: 'run-1', event_seq: 99, preview_url: '/old' });
  store.reduceComputerEvent({ type: 'computer_observation', run_id: 'run-2', event_seq: 0, preview_url: '/stale' });
  expect(store.computerRun.run_id).toBe('run-2');
  expect(store.computerRun.preview_url).not.toBe('/old');
  expect(store.computerRun.preview_url).not.toBe('/stale');
});


it('keeps only the newest 200 sanitized trace rows', () => {
  const store = useAppStore();
  for (let ordinal = 1; ordinal <= 205; ordinal++) {
    store.reduceComputerEvent({ type: 'computer_action_finished', run_id: 'run-1', event_seq: ordinal, action_ordinal: ordinal, summary: `输入 ${ordinal} 个字符` });
  }
  expect(store.computerTrace).toHaveLength(200);
  expect(JSON.stringify(store.computerTrace)).not.toContain('secret text');
});


it('recalibrates from bridge snapshot after queue drops events', async () => {
  const store = useAppStore();
  store.reduceComputerEvent({ type: 'computer_state', run_id: 'run-2', event_seq: 1, status: 'running' });
  api.get_computer_control_status.mockResolvedValue({ run_id: 'run-2', status: 'stopped', stop_reason: 'hotkey' });
  await store.refreshComputerRun();
  expect(store.computerRun.status).toBe('stopped');
});


it('always shows stop and pause controls on the compact bar', () => {
  const wrapper = mount(ComputerControlBar, { global: piniaWithRunningDesktop() });
  expect(wrapper.get('[data-test="computer-pause"]').isVisible()).toBe(true);
  expect(wrapper.get('[data-test="computer-stop"]').isVisible()).toBe(true);
  wrapper.unmount();
});

it('routes flat computer envelopes through the bridge event handler', () => {
  const store = useAppStore();
  store.handleBridgeEvent({ type: 'computer_state', run_id: 'run-1', event_seq: 1, status: 'starting', target: 'browser' } as any);
  expect(store.computerRun.run_id).toBe('run-1');
  expect(store.computerRun.status).toBe('starting');
});

it('a new run resets the trace and the previous run keeps it until then', () => {
  const store = useAppStore();
  store.reduceComputerEvent({ type: 'computer_state', run_id: 'run-1', event_seq: 1, status: 'starting' });
  store.reduceComputerEvent({ type: 'computer_action_started', run_id: 'run-1', event_seq: 2, action_ordinal: 1, action_kind: 'click', target: 'browser' });
  store.reduceComputerEvent({ type: 'computer_action_finished', run_id: 'run-1', event_seq: 3, action_ordinal: 1, action_kind: 'click', ok: true, error_code: '' });
  store.reduceComputerEvent({ type: 'computer_state', run_id: 'run-1', event_seq: 4, status: 'stopped', stop_reason: 'user_stop' });
  expect(store.computerTrace).toHaveLength(1);
  expect(store.computerRun.status).toBe('stopped');
  store.reduceComputerEvent({ type: 'computer_state', run_id: 'run-2', event_seq: 1, status: 'starting' });
  expect(store.computerTrace).toHaveLength(0);
  expect(store.computerRun.run_id).toBe('run-2');
});

it('sanitizes type_text input to length only and never stores raw text', () => {
  const store = useAppStore();
  store.reduceComputerEvent({ type: 'computer_state', run_id: 'run-1', event_seq: 1, status: 'starting' });
  store.reduceComputerEvent({ type: 'computer_action_started', run_id: 'run-1', event_seq: 2, action_ordinal: 1, action_kind: 'type_text', target: 'browser', text: 'secret text' });
  store.reduceComputerEvent({ type: 'computer_action_finished', run_id: 'run-1', event_seq: 3, action_ordinal: 1, action_kind: 'type_text', ok: true, error_code: '', duration_ms: 250, text: 'secret text', text_length: 11 });
  expect(JSON.stringify(store.computerTrace)).not.toContain('secret text');
  expect(store.computerTrace[0].summary).toBe('输入 11 个字符');
  // 轨迹行只含五个契约字段，绝不透传事件原始字段
  expect(store.computerTrace[0]).toEqual({
    action_ordinal: 1,
    summary: '输入 11 个字符',
    status: 'done',
    duration_ms: 250,
    error_code: '',
  });
});

it('recalibrating to a different run id resets the trace', async () => {
  const store = useAppStore();
  store.reduceComputerEvent({ type: 'computer_state', run_id: 'run-1', event_seq: 1, status: 'starting' });
  store.reduceComputerEvent({ type: 'computer_action_finished', run_id: 'run-1', event_seq: 2, action_ordinal: 1, action_kind: 'click', ok: true, error_code: '' });
  api.get_computer_control_status.mockResolvedValue({ run_id: 'run-2', status: 'running', target: 'desktop' });
  await store.refreshComputerRun();
  expect(store.computerRun.run_id).toBe('run-2');
  expect(store.computerRun.status).toBe('running');
  expect(store.computerTrace).toHaveLength(0);
});

it('pause resume and stop calibrate the store from the returned snapshot', async () => {
  const store = useAppStore();
  store.computerRun = { ...store.computerRun, run_id: 'run-1', status: 'running' };
  api.pause_computer_control.mockResolvedValue({ ok: true, run_id: 'run-1', status: 'paused', pause_reason: 'user_pause' });
  await store.pauseComputerControl();
  expect(api.pause_computer_control).toHaveBeenCalledWith('run-1');
  expect(store.computerRun.status).toBe('paused');
  api.resume_computer_control.mockResolvedValue({ ok: true, run_id: 'run-1', status: 'running' });
  await store.resumeComputerControl();
  expect(api.resume_computer_control).toHaveBeenCalledWith('run-1');
  expect(store.computerRun.status).toBe('running');
  api.stop_computer_control.mockResolvedValue({ ok: true, run_id: 'run-1', status: 'stopped', stop_reason: 'user_stop' });
  await store.stopComputerControl();
  expect(api.stop_computer_control).toHaveBeenCalledWith('run-1');
  expect(store.computerRun.status).toBe('stopped');
});

it('a failed control operation surfaces the error and keeps the snapshot', async () => {
  const store = useAppStore();
  store.computerRun = { ...store.computerRun, run_id: 'run-1', status: 'running' };
  api.stop_computer_control.mockResolvedValue({
    ok: false, run_id: 'run-1', status: 'running', error_code: 'computer_invalid_state', msg: '未知的电脑操控 run',
  });
  const ok = await store.stopComputerControl();
  expect(ok).toBe(false);
  expect(store.computerRun.error_code).toBe('computer_invalid_state');
  expect(store.confirmModal.message).toContain('未知的电脑操控 run');
});

it.each([
  ['starting'], ['running'], ['paused'], ['waiting_for_user'],
  ['provider_safety_required'], ['stopping'], ['failed'],
])('shows the bar for %s runs', (status) => {
  const pinia = createPinia();
  setActivePinia(pinia);
  const store = useAppStore();
  store.computerRun = { ...store.computerRun, run_id: 'run-1', status };
  const wrapper = mount(ComputerControlBar, { global: { plugins: [pinia] } });
  expect(wrapper.find('[data-test="computer-control-bar"]').exists()).toBe(true);
  wrapper.unmount();
});

it.each([['idle'], ['stopped'], ['completed'], ['completing']])(
  'hides the bar for %s runs', (status) => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const store = useAppStore();
    store.computerRun = { ...store.computerRun, run_id: 'run-1', status };
    const wrapper = mount(ComputerControlBar, { global: { plugins: [pinia] } });
    expect(wrapper.find('[data-test="computer-control-bar"]').exists()).toBe(false);
    wrapper.unmount();
  },
);

it('renders the target, action count and current surface on the bar', () => {
  const wrapper = mount(ComputerControlBar, { global: piniaWithRunningDesktop() });
  const text = wrapper.get('[data-test="computer-control-bar"]').text();
  expect(text).toContain('桌面');
  expect(text).toContain('3');
  expect(text).toContain('记事本');
  wrapper.unmount();
});

it('shows the bar for browser runs so Edge/Chrome and collapsed panel keep a stop entry', () => {
  const wrapper = mount(ComputerControlBar, { global: piniaWithRunningComputer() });
  const bar = wrapper.find('[data-test="computer-control-bar"]');
  expect(bar.exists()).toBe(true);
  expect(bar.text()).toContain('Edge');
  expect(wrapper.find('[data-test="computer-stop"]').exists()).toBe(true);
  wrapper.unmount();
});

it('shows resume instead of pause while paused', () => {
  const pinia = createPinia();
  setActivePinia(pinia);
  const store = useAppStore();
  store.computerRun = { ...store.computerRun, run_id: 'run-1', status: 'paused', target: 'desktop' };
  const wrapper = mount(ComputerControlBar, { global: { plugins: [pinia] } });
  const pause = wrapper.get('[data-test="computer-pause"]');
  expect(pause.text()).toContain('恢复');
  expect(pause.text()).not.toContain('暂停');
  wrapper.unmount();
});

it('hides pause while provider safety is required, keeping stop only', () => {
  const pinia = createPinia();
  setActivePinia(pinia);
  const store = useAppStore();
  store.computerRun = { ...store.computerRun, run_id: 'run-1', status: 'provider_safety_required' };
  const wrapper = mount(ComputerControlBar, { global: { plugins: [pinia] } });
  expect(wrapper.find('[data-test="computer-pause"]').exists()).toBe(false);
  expect(wrapper.find('[data-test="computer-stop"]').exists()).toBe(true);
  wrapper.unmount();
});

it('pause and stop buttons drive the bridge with the current run id', async () => {
  const pinia = createPinia();
  setActivePinia(pinia);
  const store = useAppStore();
  store.computerRun = { ...store.computerRun, run_id: 'run-1', status: 'running' };
  api.pause_computer_control.mockResolvedValue({ ok: true, run_id: 'run-1', status: 'paused', pause_reason: 'user_pause' });
  api.stop_computer_control.mockResolvedValue({ ok: true, run_id: 'run-1', status: 'stopped', stop_reason: 'user_stop' });
  const wrapper = mount(ComputerControlBar, { global: { plugins: [pinia] } });
  await wrapper.get('[data-test="computer-pause"]').trigger('click');
  expect(api.pause_computer_control).toHaveBeenCalledWith('run-1');
  expect(store.computerRun.status).toBe('paused');
  await wrapper.get('[data-test="computer-stop"]').trigger('click');
  expect(api.stop_computer_control).toHaveBeenCalledWith('run-1');
  expect(store.computerRun.status).toBe('stopped');
  wrapper.unmount();
});

function mountBarForSession(ownerSession: string, viewingSession: string) {
  const pinia = createPinia();
  setActivePinia(pinia);
  const store = useAppStore();
  store.currentSessionName = viewingSession;
  store.computerRun = {
    ...store.computerRun,
    run_id: 'run-1',
    status: 'running',
    target: 'desktop',
    owner_session_id: ownerSession,
  };
  const wrapper = mount(ComputerControlBar, { global: { plugins: [pinia] } });
  return { store, wrapper };
}

it('shows owner attribution and stop control while another session owns the running computer', () => {
  const { wrapper } = mountBarForSession('A', 'B');
  const bar = wrapper.find('[data-test="computer-control-bar"]');
  expect(bar.exists()).toBe(true);
  expect(bar.text()).toContain('A');
  expect(wrapper.find('[data-test="computer-stop"]').exists()).toBe(true);
  wrapper.unmount();
});

it('shows the bar when the running computer belongs to the current session', () => {
  const { wrapper } = mountBarForSession('A', 'A');
  expect(wrapper.find('[data-test="computer-control-bar"]').exists()).toBe(true);
  expect(wrapper.find('[data-test="computer-owner-banner"]').exists()).toBe(false);
  wrapper.unmount();
});

it('keeps the bar when the run owner is unknown (legacy snapshot)', () => {
  const { wrapper } = mountBarForSession('', 'B');
  expect(wrapper.find('[data-test="computer-control-bar"]').exists()).toBe(true);
  wrapper.unmount();
});

it('switches to cross-session attribution bar with stop button when user switches away from owner session', async () => {
  const { store, wrapper } = mountBarForSession('A', 'A');
  expect(wrapper.find('[data-test="computer-control-bar"]').exists()).toBe(true);
  expect(wrapper.find('[data-test="computer-owner-banner"]').exists()).toBe(false);
  store.currentSessionName = 'B';
  await nextTick();
  const bar = wrapper.find('[data-test="computer-control-bar"]');
  expect(bar.exists()).toBe(true);
  expect(wrapper.get('[data-test="computer-owner-banner"]').text()).toContain('A');
  expect(wrapper.find('[data-test="computer-stop"]').exists()).toBe(true);
  wrapper.unmount();
});

it('keeps the global bar getter session-agnostic for the right panel', () => {
  const pinia = createPinia();
  setActivePinia(pinia);
  const store = useAppStore();
  store.currentSessionName = 'B';
  store.computerRun = {
    ...store.computerRun, run_id: 'run-1', status: 'running', owner_session_id: 'A',
  };
  expect(store.computerControlBarVisible).toBe(true);
  expect(store.computerBarVisibleInCurrentSession).toBe(false);
});

it('merges state by (run_generation, event_seq) and clears current_action only on matching ordinal', async () => {
  const store = useAppStore();
  store.reduceComputerEvent({
    type: 'computer_state', run_id: 'run-1', run_generation: 2, event_seq: 1, status: 'running',
  } as any);
  store.reduceComputerEvent({
    type: 'computer_action_started', run_id: 'run-1', run_generation: 2, event_seq: 2,
    action_ordinal: 1, action_kind: 'click', target: 'desktop',
  } as any);
  expect(store.computerRun.current_action).toBe('click');

  // 旧代次迟到事件/快照不得覆盖当前 run
  store.reduceComputerEvent({
    type: 'computer_state', run_id: 'run-0', run_generation: 1, event_seq: 99, status: 'stopped',
  } as any);
  store._applyComputerSnapshot({
    run_id: 'run-0', run_generation: 1, event_seq: 99, status: 'stopped',
  });
  expect(store.computerRun.run_id).toBe('run-1');
  expect(store.computerRun.status).toBe('running');

  // 新 ordinal 开始时，旧 ordinal 迟到的 finish 不得清除新动作指示
  store.reduceComputerEvent({
    type: 'computer_action_started', run_id: 'run-1', run_generation: 2, event_seq: 3,
    action_ordinal: 2, action_kind: 'wait_for', target: 'desktop',
  } as any);
  store.reduceComputerEvent({
    type: 'computer_action_finished', run_id: 'run-1', run_generation: 2, event_seq: 4,
    action_ordinal: 1, action_kind: 'click', ok: true, error_code: '',
  } as any);
  expect(store.computerRun.current_action).toBe('wait_for');

  // 匹配 ordinal 2 的 finish 才清理 current_action
  store.reduceComputerEvent({
    type: 'computer_action_finished', run_id: 'run-1', run_generation: 2, event_seq: 5,
    action_ordinal: 2, action_kind: 'wait_for', ok: true, error_code: '',
  } as any);
  expect(store.computerRun.current_action).toBe('');
});

it('single-flights snapshot refresh on event_seq gap and replays buffered newer events', async () => {
  const store = useAppStore();
  let resolveSnap!: (v: unknown) => void;
  api.get_computer_control_status.mockImplementation(
    () => new Promise((resolve) => { resolveSnap = resolve; }),
  );
  store.reduceComputerEvent({
    type: 'computer_state', run_id: 'run-1', run_generation: 3, event_seq: 1, status: 'running', target: 'desktop',
  } as any);

  // seq 1 -> seq 4 (断档) 与 seq 5 均应在单飞快照校准前先暂存，且只发一次请求
  store.reduceComputerEvent({
    type: 'computer_action_started', run_id: 'run-1', run_generation: 3, event_seq: 4,
    action_ordinal: 2, action_kind: 'click', target: 'desktop',
  } as any);
  store.reduceComputerEvent({
    type: 'computer_observation', run_id: 'run-1', run_generation: 3, event_seq: 5,
    origin_or_window: '记事本', preview_url: '/p/5.png',
  } as any);
  expect(api.get_computer_control_status).toHaveBeenCalledTimes(1);
  // 快照覆盖到 seq=4（running，动作 2 进行中），之后回放 seq=5 更新 preview_url
  resolveSnap({
    ok: true, run_id: 'run-1', run_generation: 3, event_seq: 4, status: 'running',
    target: 'desktop', action_count: 2, current_action: 'click',
  });
  await vi.waitFor(() => expect(store.computerRun.preview_url).toBe('/p/5.png'));
  expect(store.computerRun.action_count).toBe(2);
});

it('prevents duplicate stop bridge calls while request is in flight', async () => {
  let resolveStop!: (v: unknown) => void;
  api.stop_computer_control.mockImplementation(
    () => new Promise((resolve) => { resolveStop = resolve; }),
  );
  const { wrapper } = mountBarForSession('A', 'A');
  const stopBtn = wrapper.get('[data-test="computer-stop"]');
  await stopBtn.trigger('click');
  await stopBtn.trigger('click');
  expect(api.stop_computer_control).toHaveBeenCalledTimes(1);
  resolveStop({ ok: true, run_id: 'run-1', run_generation: 1, event_seq: 2, status: 'stopping' });
  await vi.waitFor(() => expect(useAppStore().computerRun.status).toBe('stopping'));
  wrapper.unmount();
});

it('最后一条终态事件丢失时通过快照校准收敛到 stopped，且迟到非终态事件不得复活 running', async () => {
  const store = useAppStore();
  store.reduceComputerEvent({
    type: 'computer_state', run_id: 'run-9', run_generation: 4, event_seq: 1,
    status: 'running', target: 'browser', browser: 'edge', browser_mode: 'external',
  } as any);
  expect(store.computerRun.browser).toBe('edge');
  expect(store.computerRun.browser_mode).toBe('external');

  api.get_computer_control_status.mockResolvedValue({
    ok: true, run_id: 'run-9', run_generation: 4, event_seq: 8,
    status: 'stopped', stop_reason: 'user_stop', target: 'browser', browser: 'edge', browser_mode: 'external',
  });
  await store.refreshComputerRun();
  expect(store.computerRun.status).toBe('stopped');

  // 迟到的非终态事件（即使缺 event_seq 或带较旧序号）也不得推翻同代次终态
  store.reduceComputerEvent({
    type: 'computer_state', run_id: 'run-9', run_generation: 4, event_seq: 0, status: 'running',
  } as any);
  store.reduceComputerEvent({
    type: 'computer_state', run_id: 'run-9', run_generation: 4, event_seq: 9, status: 'running',
  } as any);
  expect(store.computerRun.status).toBe('stopped');
});

it('bar distinguishes embedded browser, external Edge/Chrome, and desktop', () => {
  const pinia = createPinia();
  setActivePinia(pinia);
  const store = useAppStore();
  store.computerRun = {
    ...store.computerRun,
    run_id: 'run-e', status: 'running', target: 'browser', browser: 'embedded', browser_mode: 'embedded',
  };
  const wrapper = mount(ComputerControlBar, { global: { plugins: [pinia] } });
  expect(wrapper.get('[data-test="computer-control-bar"]').text()).toContain('内置浏览器');
  wrapper.unmount();
});

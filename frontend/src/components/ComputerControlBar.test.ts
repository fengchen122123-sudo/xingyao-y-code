import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, expect, it, vi } from 'vitest';
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

// brief 样例 4 的挂载辅助：运行中的 run + 已挂载 pinia 的 global 选项
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

// ── Store reducer：顺序、容量与脱敏（brief Step 1 样例 1-3 逐字） ──

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
  await store.refreshComputerRun();  // 窗口重新聚焦/切到操控页签时也调用
  expect(store.computerRun.status).toBe('stopped');
});


it('always shows stop and pause controls on the compact bar', () => {
  const wrapper = mount(ComputerControlBar, { global: piniaWithRunningComputer() });
  expect(wrapper.get('[data-test="computer-pause"]').isVisible()).toBe(true);
  expect(wrapper.get('[data-test="computer-stop"]').isVisible()).toBe(true);
  wrapper.unmount();
});

// ── 追加项：平铺信封分发、run 生命周期与脱敏轨迹 ──

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
  // run 结束：轨迹保留到下一个 run（设计 16.2）
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

// ── 安全条可见性（brief Step 4 的显示状态表逐字） ──

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
  const wrapper = mount(ComputerControlBar, { global: piniaWithRunningComputer() });
  const text = wrapper.get('[data-test="computer-control-bar"]').text();
  expect(text).toContain('Edge');
  expect(text).toContain('3');
  expect(text).toContain('example.com');
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

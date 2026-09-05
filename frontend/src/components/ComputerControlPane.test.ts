import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, expect, it, vi } from 'vitest';
import { mount } from '@vue/test-utils';

import ComputerControlPane from './ComputerControlPane.vue';
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

function mountPane() {
  return mount(ComputerControlPane);
}

function seedRun(store: ReturnType<typeof useAppStore>, overrides: Record<string, unknown> = {}) {
  store.computerRun = {
    ...store.computerRun,
    run_id: 'run-1',
    status: 'running',
    target: 'browser',
    browser: 'edge',
    model_name: 'glm-5.3',
    action_count: 2,
    preview_url: '/computer-preview/tok-abc/shot-003.png',
    ...overrides,
  } as typeof store.computerRun;
}

it('renders the screenshot preview from the loopback token url only', () => {
  const store = useAppStore();
  seedRun(store);
  const wrapper = mountPane();

  const img = wrapper.get('[data-test="computer-preview-image"]');
  expect(img.attributes('src')).toBe('/computer-preview/tok-abc/shot-003.png');
  // 绝不经 evaluate_js 传 base64：preview 只认短 URL
  expect(img.attributes('src')).not.toContain('base64');
  wrapper.unmount();
});

it('renders trace rows newest first without raw input text', () => {
  const store = useAppStore();
  seedRun(store);
  store.reduceComputerEvent({ type: 'computer_state', run_id: 'run-1', event_seq: 1, status: 'running' });
  store.reduceComputerEvent({ type: 'computer_action_started', run_id: 'run-1', event_seq: 2, action_ordinal: 1, action_kind: 'navigate', target: 'browser' });
  store.reduceComputerEvent({ type: 'computer_action_finished', run_id: 'run-1', event_seq: 3, action_ordinal: 1, action_kind: 'navigate', ok: true, error_code: '', duration_ms: 820 });
  store.reduceComputerEvent({ type: 'computer_action_finished', run_id: 'run-1', event_seq: 5, action_ordinal: 2, action_kind: 'type_text', ok: true, error_code: '', duration_ms: 140, text: 'secret text', text_length: 7 });

  const wrapper = mountPane();
  const rows = wrapper.findAll('[data-test="computer-trace-row"]');
  expect(rows).toHaveLength(2);
  // 新到旧：第 2 个动作排在最前
  expect(rows[0].text()).toContain('输入 7 个字符');
  expect(rows[0].text()).toContain('140');
  expect(rows[1].text()).toContain('导航');
  expect(rows[1].text()).toContain('820');
  expect(wrapper.text()).not.toContain('secret text');
  wrapper.unmount();
});

it('shows run status target model and failure reasons', () => {
  const store = useAppStore();
  seedRun(store, { status: 'failed', error_code: 'computer_no_progress' });
  const wrapper = mountPane();
  const text = wrapper.text();
  expect(text).toContain('Edge');
  expect(text).toContain('操控失败');
  expect(text).toContain('glm-5.3');
  expect(text).toContain('computer_no_progress');
  wrapper.unmount();
});

it('shows stop only without any resume button for provider safety runs', () => {
  const store = useAppStore();
  seedRun(store, { status: 'provider_safety_required' });
  const wrapper = mountPane();
  expect(wrapper.find('[data-test="computer-stop"]').exists()).toBe(true);
  expect(wrapper.find('[data-test="computer-pause"]').exists()).toBe(false);
  // Provider safety 确认契约未验证：不提供任何“继续”入口
  expect(wrapper.text()).not.toContain('继续');
  expect(wrapper.text()).not.toContain('恢复');
  wrapper.unmount();
});

it('pause resume and stop controls drive the bridge with the current run id', async () => {
  const store = useAppStore();
  seedRun(store);
  api.pause_computer_control.mockResolvedValue({ ok: true, run_id: 'run-1', status: 'paused', pause_reason: 'user_pause' });
  api.resume_computer_control.mockResolvedValue({ ok: true, run_id: 'run-1', status: 'running' });
  api.stop_computer_control.mockResolvedValue({ ok: true, run_id: 'run-1', status: 'stopping' });

  const wrapper = mountPane();
  await wrapper.get('[data-test="computer-pause"]').trigger('click');
  expect(api.pause_computer_control).toHaveBeenCalledWith('run-1');
  expect(store.computerRun.status).toBe('paused');
  await wrapper.get('[data-test="computer-pause"]').trigger('click');
  expect(api.resume_computer_control).toHaveBeenCalledWith('run-1');
  expect(store.computerRun.status).toBe('running');
  await wrapper.get('[data-test="computer-stop"]').trigger('click');
  expect(api.stop_computer_control).toHaveBeenCalledWith('run-1');
  expect(store.computerRun.status).toBe('stopping');
  wrapper.unmount();
});

it('keeps showing the trace after the run ends', () => {
  const store = useAppStore();
  seedRun(store, { status: 'stopped', stop_reason: 'user_stop' });
  store.reduceComputerEvent({ type: 'computer_state', run_id: 'run-1', event_seq: 1, status: 'running' });
  store.reduceComputerEvent({ type: 'computer_action_finished', run_id: 'run-1', event_seq: 2, action_ordinal: 1, action_kind: 'click', ok: true, error_code: '' });
  store.reduceComputerEvent({ type: 'computer_state', run_id: 'run-1', event_seq: 3, status: 'stopped' });

  const wrapper = mountPane();
  expect(wrapper.findAll('[data-test="computer-trace-row"]')).toHaveLength(1);
  expect(wrapper.text()).toContain('user_stop');
  wrapper.unmount();
});

it('shows an empty state before any run has been seen', () => {
  const wrapper = mountPane();
  expect(wrapper.find('[data-test="computer-preview-image"]').exists()).toBe(false);
  expect(wrapper.text()).toContain('暂无');
  wrapper.unmount();
});

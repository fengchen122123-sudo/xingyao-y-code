import { beforeEach, expect, it, vi } from 'vitest';
import { createPinia, setActivePinia } from 'pinia';
import { mount } from '@vue/test-utils';

import DebugToolbar from './DebugToolbar.vue';
import { useIdeStore } from '../stores/ideStore';
import { useIdeDebugStore } from '../stores/ideDebugStore';

const api = {
  ide_run_file: vi.fn().mockResolvedValue({ ok: true, command: 'python -u app.py' }),
  ide_debug_start: vi.fn().mockResolvedValue({ ok: true, session_id: 'dap-1', adapter: 'debugpy' }),
  ide_debug_command: vi.fn().mockResolvedValue({ ok: true }),
  ide_debug_stop: vi.fn().mockResolvedValue({ ok: true }),
  ide_debug_threads: vi.fn().mockResolvedValue({ ok: true, threads: [{ id: 7, name: 'MainThread' }] }),
};

beforeEach(() => {
  vi.clearAllMocks();
  setActivePinia(createPinia());
  (window as any).pywebview = { api };
});

function mountBar() {
  return mount(DebugToolbar);
}

function openFile(ide = useIdeStore()) {
  // 计划原文的活跃标签夹具（...encoding/eol/dirty/viewState）与仓内 IdeTab 不符：
  // viewState 字段不存在，且缺 content/bom/hash/externalChange/conflict。
  // 这里按 ideStore.IdeTab 的真实形状补全（最小修正，不改变用例意图）
  ide.activeTab = {
    path: 'E:/p/app.py',
    content: 'print(1)\n',
    encoding: 'utf-8',
    bom: false,
    eol: 'lf',
    hash: '',
    dirty: false,
    externalChange: false,
    conflict: false,
  };
  return ide;
}

it('runs the active file after expanding the bottom terminal panel', async () => {
  const ide = openFile();
  const wrapper = mountBar();

  await wrapper.get('[data-test="debug-run"]').trigger('click');

  expect(api.ide_run_file).toHaveBeenCalledWith('E:/p/app.py', '');
  expect(ide.bottomPanelOpen).toBe(true);
  expect(ide.bottomTab).toBe('terminal');
  wrapper.unmount();
});

it('disables run without an active editor tab', () => {
  const wrapper = mountBar();

  expect(wrapper.get('[data-test="debug-run"]').attributes('disabled')).toBeDefined();
  wrapper.unmount();
});

it('shows step controls only while paused', async () => {
  const debug = useIdeDebugStore();
  const wrapper = mountBar();
  expect(wrapper.find('[data-test="debug-step-over"]').exists()).toBe(false);

  // 计划原文只改 debugState：store 的 active 还要求 sessionId 非空（S3-T8 交付口径），
  // 不补这一句按钮根本不会渲染——这是计划夹具的事实性错误，已按 active 的真实语义修正
  debug.sessionId = 'dap-1';
  debug.debugState = 'paused';
  await wrapper.vm.$nextTick();

  expect(wrapper.find('[data-test="debug-step-over"]').exists()).toBe(true);
  await wrapper.get('[data-test="debug-step-over"]').trigger('click');
  expect(api.ide_debug_command).toHaveBeenCalledWith('next');
  wrapper.unmount();
});

it('starts a debug session for the active file', async () => {
  openFile();
  const debug = useIdeDebugStore();
  const wrapper = mountBar();

  await wrapper.get('[data-test="debug-start"]').trigger('click');

  expect(api.ide_debug_start).toHaveBeenCalledWith('E:/p/app.py', [], '', []);
  expect(debug.sessionId).toBe('dap-1');
  wrapper.unmount();
});

it('surfaces adapter hints when starting fails', async () => {
  api.ide_debug_start.mockResolvedValue({ ok: false, msg: '未安装 debugpy（Python 调试适配器）' });
  openFile();
  const wrapper = mountBar();

  await wrapper.get('[data-test="debug-start"]').trigger('click');

  expect(wrapper.get('[data-test="debug-error"]').text()).toContain('debugpy');
  wrapper.unmount();
});

it('labels the current debug state in the toolbar', async () => {
  const debug = useIdeDebugStore();
  const wrapper = mountBar();
  expect(wrapper.get('[data-test="debug-state"]').text()).toBe('未启动');

  debug.sessionId = 'dap-1';
  debug.adapter = 'debugpy';
  debug.debugState = 'running';
  await wrapper.vm.$nextTick();
  expect(wrapper.get('[data-test="debug-state"]').text()).toContain('运行中');

  debug.debugState = 'paused';
  debug.stopLine = 12;
  await wrapper.vm.$nextTick();
  expect(wrapper.get('[data-test="debug-state"]').text()).toContain('12');
  wrapper.unmount();
});

it('renders no session controls while no session is active', () => {
  const wrapper = mountBar();

  for (const hook of ['debug-continue', 'debug-pause', 'debug-step-over', 'debug-restart', 'debug-stop']) {
    expect(wrapper.find(`[data-test="${hook}"]`).exists()).toBe(false);
  }
  // 停止的按钮虽然不渲染，但停止能力必须还在（未启动时点不动「继续/暂停/停止」）
  expect(api.ide_debug_command).not.toHaveBeenCalled();
  wrapper.unmount();
});

it('refuses to start a second session while the first is starting', async () => {
  openFile();
  const debug = useIdeDebugStore();
  debug.debugState = 'starting';
  const wrapper = mountBar();

  expect(wrapper.get('[data-test="debug-start"]').attributes('disabled')).toBeDefined();
  wrapper.unmount();
});

it('resolves a thread id before pausing while running', async () => {
  const debug = useIdeDebugStore();
  debug.sessionId = 'dap-1';
  debug.debugState = 'running';
  const wrapper = mountBar();

  await wrapper.get('[data-test="debug-pause"]').trigger('click');

  // 运行中还没有 dap_stopped，threadId 是 0：必须先取线程表拿到真实线程号，
  // 否则适配器会把 threadId:0 的 pause 拒成 thread not found
  expect(api.ide_debug_threads).toHaveBeenCalled();
  expect(api.ide_debug_command).toHaveBeenCalledWith('pause', 7);
  wrapper.unmount();
});

it('surfaces command failures in the toolbar instead of failing silently', async () => {
  api.ide_debug_command.mockResolvedValue({ ok: false, msg: '继续运行失败：thread not found' });
  const debug = useIdeDebugStore();
  debug.sessionId = 'dap-1';
  debug.debugState = 'paused';
  const wrapper = mountBar();

  await wrapper.get('[data-test="debug-continue"]').trigger('click');

  expect(wrapper.get('[data-test="debug-error"]').text()).toContain('thread not found');
  wrapper.unmount();
});

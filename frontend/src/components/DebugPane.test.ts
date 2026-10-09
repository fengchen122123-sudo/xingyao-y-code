import { beforeEach, expect, it, vi } from 'vitest';
import { createPinia, setActivePinia } from 'pinia';
import { flushPromises, mount } from '@vue/test-utils';

import DebugPane from './DebugPane.vue';
import { useIdeDebugStore } from '../stores/ideDebugStore';
import { emitDapOutput } from '../utils/dapBus';

const api = {
  ide_debug_stack_trace: vi.fn().mockResolvedValue({
    ok: true,
    frames: [
      { id: 1001, name: 'main', path: 'E:/p/app.py', line: 7, column: 1 },
      { id: 1002, name: 'helper', path: 'E:/p/app.py', line: 21, column: 3 },
    ],
  }),
  ide_debug_scopes: vi.fn().mockResolvedValue({
    ok: true,
    scopes: [{ name: 'Locals', variablesReference: 2001, expensive: false }],
  }),
  ide_debug_variables: vi.fn().mockResolvedValue({
    ok: true,
    variables: [{ name: 'x', value: '42', type: 'int', variablesReference: 0 }],
  }),
  // 桥的真实契约：`ide_debug_evaluate` 平铺返回 {result, type, variablesReference}，且
  // result 是被求值表达式的结果（后端 `_FakeSession.evaluate` 就是 `f"eval:{expression}"`）。
  // 计划里写成返回常量 'eval:1+1' 会让「监视 x+1 应显示 eval:x+1」这条断言与夹具自相矛盾。
  ide_debug_evaluate: vi.fn().mockImplementation(async (expression: string) => ({
    ok: true,
    result: `eval:${expression}`,
    type: 'str',
  })),
};

beforeEach(() => {
  vi.clearAllMocks();
  setActivePinia(createPinia());
  (window as any).pywebview = { api };
});

function mountPane() {
  return mount(DebugPane);
}

it('renders the stopped stack and selects a frame on click', async () => {
  const debug = useIdeDebugStore();
  debug.threadId = 1;
  debug.selectedFrameId = 1001;
  const wrapper = mountPane();
  await debug.refreshFrames();
  await wrapper.vm.$nextTick();

  const rows = wrapper.findAll('[data-test="debug-frame"]');
  expect(rows.length).toBe(2);
  expect(rows[0].text()).toContain('main');

  await rows[1].trigger('click');

  expect(api.ide_debug_scopes).toHaveBeenCalledWith(1002);
  expect(debug.selectedFrameId).toBe(1002);
  wrapper.unmount();
});

it('renders variables of the first scope', async () => {
  const debug = useIdeDebugStore();
  const wrapper = mountPane();

  await debug.loadScopes(1001);
  await wrapper.vm.$nextTick();

  expect(wrapper.get('[data-test="debug-scope"]').text()).toContain('Locals');
  expect(wrapper.get('[data-test="debug-variable"]').text()).toContain('42');
  wrapper.unmount();
});

it('appends adapter output into the console buffer', async () => {
  const wrapper = mountPane();

  emitDapOutput({ session_id: 'dap-1', category: 'stdout', text: 'breakpoint hit\n' });
  await wrapper.vm.$nextTick();

  expect(wrapper.get('[data-test="debug-console-output"]').text()).toContain('breakpoint hit');
  wrapper.unmount();
});

it('evaluates an expression typed into the console on the selected frame', async () => {
  const debug = useIdeDebugStore();
  debug.selectedFrameId = 1001;
  const wrapper = mountPane();
  await wrapper.get('[data-test="debug-console-input"]').setValue('1+1');

  await wrapper.get('[data-test="debug-console-run"]').trigger('click');
  // 求值要经桥调用（异步）：trigger() 只冲刷 DOM 更新，不等处理函数的 Promise 链
  await flushPromises();

  expect(api.ide_debug_evaluate).toHaveBeenCalledWith('1+1', 1001);
  expect(wrapper.get('[data-test="debug-console-output"]').text()).toContain('eval:1+1');
  wrapper.unmount();
});

it('adds and removes watch expressions', async () => {
  const debug = useIdeDebugStore();
  debug.selectedFrameId = 1001;
  const wrapper = mountPane();
  await wrapper.get('[data-test="debug-watch-input"]').setValue('x+1');

  await wrapper.get('[data-test="debug-watch-add"]').trigger('click');
  await flushPromises();

  expect(api.ide_debug_evaluate).toHaveBeenCalledWith('x+1', 1001);
  expect(wrapper.get('[data-test="debug-watch-row"]').text()).toContain('eval:x+1');

  await wrapper.get('[data-test="debug-watch-remove"]').trigger('click');
  expect(wrapper.find('[data-test="debug-watch-row"]').exists()).toBe(false);
  wrapper.unmount();
});

it('shows the bridge error message when a debug call fails', async () => {
  api.ide_debug_evaluate.mockResolvedValue({ ok: false, msg: '调试未启动' });
  const wrapper = mountPane();
  await wrapper.get('[data-test="debug-console-input"]').setValue('x');
  await wrapper.get('[data-test="debug-console-run"]').trigger('click');
  await flushPromises();

  expect(wrapper.get('[data-test="debug-pane-error"]').text()).toContain('调试未启动');
  wrapper.unmount();
});

it('refreshes both scopes and variables when another frame is selected', async () => {
  // 交付要求：切换栈帧要刷新变量与作用域（variablesReference 是帧内作用域的子节点 id）
  const debug = useIdeDebugStore();
  debug.threadId = 1;
  const wrapper = mountPane();
  await debug.refreshFrames();
  await wrapper.vm.$nextTick();

  await wrapper.findAll('[data-test="debug-frame"]')[1].trigger('click');
  await wrapper.vm.$nextTick();

  expect(api.ide_debug_scopes).toHaveBeenCalledWith(1002);
  expect(api.ide_debug_variables).toHaveBeenCalledWith(2001);
  expect(debug.variables.map((item) => `${item.name}=${item.value}`)).toEqual(['x=42']);
  expect(wrapper.get('[data-test="debug-variable"]').text()).toContain('42');
  wrapper.unmount();
});

it('does not expand a variable whose variablesReference is 0', async () => {
  // 交付要求：variablesReference 为 0 表示无子节点，不展示展开能力（点了也不发请求）
  const debug = useIdeDebugStore();
  const wrapper = mountPane();
  await debug.loadScopes(1001);
  await wrapper.vm.$nextTick();

  const row = wrapper.get('[data-test="debug-variable"]');
  expect(row.classes()).not.toContain('clickable');
  expect(row.find('.dp-expand').exists()).toBe(false);

  await row.trigger('click');

  // 只有作用域那一次展开；点无子节点的变量不该再发 variables 请求
  expect(api.ide_debug_variables).toHaveBeenCalledTimes(1);
  wrapper.unmount();
});

// ── S3-T10 评审 ────────────────────────────────────────────────────────────

it('每个作用域只渲染自己的变量，不把首个作用域的内容渲染两遍', async () => {
  api.ide_debug_scopes.mockResolvedValue({
    ok: true,
    scopes: [
      { name: 'Locals', variablesReference: 2001, expensive: false },
      { name: 'Globals', variablesReference: 3001, expensive: false },
    ],
  });
  api.ide_debug_variables.mockImplementation(async (reference: number) =>
    reference === 2001
      ? { ok: true, variables: [{ name: 'x', value: '42', type: 'int', variablesReference: 0 }] }
      : { ok: true, variables: [{ name: 'os', value: '<module os>', type: 'module', variablesReference: 0 }] },
  );
  const debug = useIdeDebugStore();
  const wrapper = mountPane();

  await debug.loadScopes(1001);
  await wrapper.vm.$nextTick();

  const tabs = wrapper.findAll('[data-test="debug-scope"]');
  expect(tabs.map((tab) => tab.text())).toEqual(['Locals', 'Globals']);
  expect(tabs[0].classes()).toContain('active');
  expect(wrapper.findAll('[data-test="debug-variable"]').map((row) => row.text())).toHaveLength(1);
  expect(wrapper.get('[data-test="debug-variable"]').text()).toContain('x');

  await tabs[1].trigger('click');
  await wrapper.vm.$nextTick();

  const rows = wrapper.findAll('[data-test="debug-variable"]');
  expect(rows).toHaveLength(1);
  expect(rows[0].text()).toContain('os');
  expect(rows[0].text()).not.toContain('42');
  wrapper.unmount();
});

it('展开变量后显示层级面包屑与「返回上一层」，可回到父层', async () => {
  api.ide_debug_variables.mockResolvedValue({
    ok: true,
    variables: [{ name: 'self', value: 'App', type: 'App', variablesReference: 4001 }],
  });
  const debug = useIdeDebugStore();
  const wrapper = mountPane();

  await debug.loadScopes(1001);
  await wrapper.vm.$nextTick();

  // 作用域根层级没有展开箭头 → 没有返回按钮
  expect(wrapper.find('[data-test="debug-var-back"]').exists()).toBe(false);
  expect(wrapper.get('[data-test="debug-var-crumb"]').text()).toBe('Locals');

  await wrapper.get('[data-test="debug-variable"]').trigger('click');
  await flushPromises();
  await wrapper.vm.$nextTick();

  expect(wrapper.findAll('[data-test="debug-var-crumb"]').map((node) => node.text())).toEqual(['Locals', 'self']);

  await wrapper.get('[data-test="debug-var-back"]').trigger('click');
  await flushPromises();
  await wrapper.vm.$nextTick();

  expect(wrapper.findAll('[data-test="debug-var-crumb"]').map((node) => node.text())).toEqual(['Locals']);
  expect(wrapper.find('[data-test="debug-var-back"]').exists()).toBe(false);
  wrapper.unmount();
});

it('调试输出之间不多插空行（DAP 输出自带尾换行）', async () => {
  const wrapper = mountPane();

  emitDapOutput({ session_id: 'dap-1', category: 'stdout', text: 'first\n' });
  emitDapOutput({ session_id: 'dap-1', category: 'stdout', text: 'second\n' });
  await wrapper.vm.$nextTick();

  // 旧写法 split(/\r?\n/) 对尾换行会多出空串元素 → 两组输出之间被插入一个空行
  expect(wrapper.get('[data-test="debug-console-output"]').element.textContent).toBe('first\nsecond');

  // 输出内部的空行是真内容，必须保留
  emitDapOutput({ session_id: 'dap-1', category: 'stdout', text: 'third\n\n' });
  await wrapper.vm.$nextTick();
  expect(wrapper.get('[data-test="debug-console-output"]').element.textContent).toBe('first\nsecond\nthird\n');
  wrapper.unmount();
});

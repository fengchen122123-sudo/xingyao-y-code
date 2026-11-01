import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, expect, it, vi } from 'vitest';
import { flushPromises, mount } from '@vue/test-utils';

import GoalBar from './GoalBar.vue';
import { useAppStore } from '../stores/appStore';
import type { GoalStateInfo } from '../types/pywebview';

// 前端测试一律 mock window.pywebview.api（无真实 Bridge）。
// 挂载时组件会先探测一次 get_goal_state，默认回"没有目标"，
// 想验证某种状态就由用例自己写 store.goalState。
const api = {
  get_goal_state: vi.fn().mockResolvedValue({ ok: true, state: null }),
  goal_stop: vi.fn().mockResolvedValue({ ok: true }),
  goal_resume: vi.fn().mockResolvedValue({ ok: true }),
};

beforeEach(() => {
  vi.clearAllMocks();
  setActivePinia(createPinia());
  (window as any).pywebview = { api };
});

function goal(patch: Partial<GoalStateInfo> = {}): GoalStateInfo {
  return {
    goal: '把登录页拆成独立组件',
    round: 2,
    max_rounds: 5,
    status: 'running',
    last_summary: '已抽出表单组件',
    check: '',
    ...patch,
  };
}

// 挂载辅助：先让挂载探测（get_goal_state -> null）落地，再写入目标状态，
// 否则用例刚设好的状态会被那次探测响应覆盖成 null
async function mountWith(state: GoalStateInfo | null) {
  const pinia = createPinia();
  setActivePinia(pinia);
  const store = useAppStore();
  const wrapper = mount(GoalBar, { global: { plugins: [pinia] } });
  await flushPromises();
  store.goalState = state;
  await flushPromises();
  return { wrapper, store };
}

it('goalState 为 null 时不渲染状态条', async () => {
  const { wrapper } = await mountWith(null);
  expect(wrapper.find('[data-test="goal-bar"]').exists()).toBe(false);
  wrapper.unmount();
});

it('running 状态渲染目标文本、轮次与「停止」按钮', async () => {
  const { wrapper } = await mountWith(goal());

  expect(wrapper.get('[data-test="goal-text"]').text()).toContain('把登录页拆成独立组件');
  expect(wrapper.get('[data-test="goal-round"]').text()).toBe('第 2/5 轮');
  expect(wrapper.get('[data-test="goal-status"]').text()).toBe('进行中');
  expect(wrapper.find('[data-test="goal-stop"]').exists()).toBe(true);
  // 运行中不给「继续」：后端此时会拒绝重复启动，按钮不该出现
  expect(wrapper.find('[data-test="goal-resume"]').exists()).toBe(false);

  wrapper.unmount();
});

it('blocked 状态渲染「继续」按钮而不是「停止」', async () => {
  const { wrapper } = await mountWith(goal({ status: 'blocked', round: 5, last_summary: '' }));

  expect(wrapper.get('[data-test="goal-status"]').text()).toBe('已达上限');
  expect(wrapper.find('[data-test="goal-resume"]').exists()).toBe(true);
  expect(wrapper.find('[data-test="goal-stop"]').exists()).toBe(false);

  wrapper.unmount();
});

it('点击「停止」调用 store.stopGoal', async () => {
  const { wrapper, store } = await mountWith(goal());
  const stopSpy = vi.spyOn(store, 'stopGoal').mockResolvedValue(undefined);

  await wrapper.get('[data-test="goal-stop"]').trigger('click');
  await flushPromises();

  expect(stopSpy).toHaveBeenCalledTimes(1);
  wrapper.unmount();
});

it('achieved 状态用成功样式并在文案上标出「已完成」', async () => {
  const { wrapper } = await mountWith(goal({ status: 'achieved', round: 3 }));

  const bar = wrapper.get('[data-test="goal-bar"]');
  // 成功样式由 .gb-achieved 承担（3 秒后自动隐藏，隐藏期间不改 store，故这里只校验样式）
  expect(bar.classes()).toContain('gb-achieved');
  expect(wrapper.get('[data-test="goal-status"]').text()).toBe('已完成');
  // 达成是终态：停止/继续都不该出现
  expect(wrapper.find('[data-test="goal-stop"]').exists()).toBe(false);
  expect(wrapper.find('[data-test="goal-resume"]').exists()).toBe(false);

  wrapper.unmount();
});

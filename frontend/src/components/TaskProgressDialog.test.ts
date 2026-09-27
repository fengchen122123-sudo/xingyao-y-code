import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, expect, it } from 'vitest';
import { mount } from '@vue/test-utils';

import TaskProgressDialog from './TaskProgressDialog.vue';
import { useAppStore } from '../stores/appStore';

beforeEach(() => {
  setActivePinia(createPinia());
  delete (window as any).pywebview;
});

it('渲染计划全景：标题、进度与各状态条目，handoff_note 优先展示', () => {
  const store = useAppStore();
  store.planData = {
    title: '重构登录模块',
    progress: 60,
    items: [
      { id: '1', text: '梳理现状', status: 'done' },
      { id: '2', text: '改核心代码', status: 'in_progress', handoff_note: '正在改 session.rs' },
      { id: '3', text: '回归验证', status: 'todo' },
    ],
  };
  const wrapper = mount(TaskProgressDialog);

  expect(wrapper.find('[data-test="task-dialog"]').exists()).toBe(true);
  expect(wrapper.find('[data-test="task-dialog-plan"]').text()).toContain('重构登录模块');
  expect(wrapper.find('[data-test="task-dialog-plan"]').text()).toContain('60%');
  expect(wrapper.find('[data-test="plan-item-done"]').text()).toContain('梳理现状');
  expect(wrapper.find('[data-test="plan-item-in_progress"]').text()).toContain('正在改 session.rs');
  expect(wrapper.find('[data-test="plan-item-todo"]').text()).toContain('回归验证');
  // 条目单行省略展示，完整文字通过 title 悬停查看
  expect(wrapper.find('[data-test="plan-item-in_progress"] .p-text').attributes('title')).toBe('正在改 session.rs');
  wrapper.unmount();
});

it('无计划标题时进度行回退为“当前任务计划”', () => {
  const store = useAppStore();
  store.planData = { title: '', progress: 0, items: [] };
  const wrapper = mount(TaskProgressDialog);
  expect(wrapper.find('[data-test="task-dialog-plan"]').text()).toContain('当前任务计划');
  expect(wrapper.find('[data-test="task-dialog-empty"]').exists()).toBe(true);
  wrapper.unmount();
});

it('计划进度更新实时反映到面板（plan_updated 事件驱动，无需手动刷新）', async () => {
  const store = useAppStore();
  store.planData = { title: '计划', progress: 0, items: [{ id: '1', text: '第一步', status: 'in_progress' }] };
  const wrapper = mount(TaskProgressDialog);
  expect(wrapper.find('[data-test="task-dialog-plan"]').text()).toContain('0%');

  store.planData = { title: '计划', progress: 100, items: [{ id: '1', text: '第一步', status: 'done' }] };
  await wrapper.vm.$nextTick();
  expect(wrapper.find('[data-test="task-dialog-plan"]').text()).toContain('100%');
  expect(wrapper.find('[data-test="plan-item-done"]').exists()).toBe(true);
  wrapper.unmount();
});

it('点击关闭按钮 emits close', async () => {
  const store = useAppStore();
  store.planData = { title: '计划', progress: 0, items: [] };
  const wrapper = mount(TaskProgressDialog);
  await wrapper.find('.chip-btn').trigger('click');
  expect(wrapper.emitted('close')).toHaveLength(1);
  wrapper.unmount();
});

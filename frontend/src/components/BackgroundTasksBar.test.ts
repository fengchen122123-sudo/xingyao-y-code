import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, expect, it, vi } from 'vitest';
import { mount } from '@vue/test-utils';

import BackgroundTasksBar from './BackgroundTasksBar.vue';
import { useAppStore } from '../stores/appStore';

beforeEach(() => {
  setActivePinia(createPinia());
  delete (window as any).pywebview;
});

it('快照字段归一化：duration 映射 running_time，status 恒为 running', async () => {
  (window as any).pywebview = {
    api: {
      get_background_tasks: vi.fn().mockResolvedValue([
        { task_id: 't1', command: 'sleep 30', pid: 123, duration: '35秒', turn_id: 'turn-1' },
      ]),
    },
  };
  const store = useAppStore();
  await store.refreshBackgroundTasks();
  expect(store.backgroundTasks).toEqual([
    {
      task_id: 't1',
      command: 'sleep 30',
      status: 'running',
      running_time: '35秒',
    },
  ]);
});

it('无后台任务时不渲染执行条', () => {
  const wrapper = mount(BackgroundTasksBar);
  expect(wrapper.find('[data-test="bg-bar"]').exists()).toBe(false);
  wrapper.unmount();
});

it('有后台任务时显示数量文案，默认收起卡片，点详情展开', async () => {
  const store = useAppStore();
  store.backgroundTasks = [
    { task_id: 't1', command: 'pytest -q', status: 'running', running_time: '35s' },
    { task_id: 't2', command: 'npm run build', status: 'running' },
  ];
  const wrapper = mount(BackgroundTasksBar);

  expect(wrapper.find('[data-test="bg-bar"]').exists()).toBe(true);
  expect(wrapper.find('[data-test="bg-bar-count"]').text()).toContain('2 个后台任务在执行');
  expect(wrapper.find('[data-test="bg-card"]').exists()).toBe(false);

  await wrapper.find('[data-test="bg-bar-details"]').trigger('click');
  expect(wrapper.find('[data-test="bg-card"]').exists()).toBe(true);
  const cardText = wrapper.find('[data-test="bg-card"]').text();
  expect(cardText).toContain('pytest -q');
  expect(cardText).toContain('35s');
  expect(cardText).toContain('npm run build');
  expect(wrapper.find('[data-test="bg-kill-t1"]').exists()).toBe(true);

  // 再点一次详情收起
  await wrapper.find('[data-test="bg-bar-details"]').trigger('click');
  expect(wrapper.find('[data-test="bg-card"]').exists()).toBe(false);
  wrapper.unmount();
});

it('终止任务调用 kill_task 并立即刷新快照', async () => {
  // 组件挂载不拉快照，只有终止后的刷新会调用一次 get_background_tasks
  const getStatus = vi.fn().mockResolvedValue([]);
  const kill = vi.fn().mockResolvedValue({ ok: true, msg: '已终止后台任务' });
  (window as any).pywebview = { api: { get_background_tasks: getStatus, kill_task: kill } };
  const store = useAppStore();
  store.backgroundTasks = [{ task_id: 't1', command: 'sleep 30', status: 'running' }];
  const wrapper = mount(BackgroundTasksBar);

  await wrapper.find('[data-test="bg-bar-details"]').trigger('click');
  await wrapper.find('[data-test="bg-kill-t1"]').trigger('click');

  await vi.waitFor(() => expect(store.backgroundTasks).toHaveLength(0));
  expect(kill).toHaveBeenCalledWith('t1');
  expect(getStatus).toHaveBeenCalledTimes(1);
  wrapper.unmount();
});

it('任务详情按钮保持卡片展开并打开右侧后台任务工作台', async () => {
  const store = useAppStore();
  store.backgroundTasks = [{ task_id: 't1', command: 'sleep 30', status: 'running' }];
  const wrapper = mount(BackgroundTasksBar);

  await wrapper.find('[data-test="bg-bar-details"]').trigger('click');
  await wrapper.find('[data-test="bg-detail-t1"]').trigger('click');

  // 详情不收起卡片：侧栏查看执行过程时卡片保持可见
  expect(wrapper.find('[data-test="bg-card"]').exists()).toBe(true);
  expect(store.bgTaskDetailId).toBe('t1');
  expect(store.bgTaskOpenNonce).toBe(1);
  expect(store.rightPanelCollapsed).toBe(false);
  expect(store.rightPanelPicker).toBe(false);
  wrapper.unmount();
});

it('点击文档其他区域收起卡片', async () => {
  const store = useAppStore();
  store.backgroundTasks = [{ task_id: 't1', command: 'sleep 30', status: 'running' }];
  const wrapper = mount(BackgroundTasksBar);

  await wrapper.find('[data-test="bg-bar-details"]').trigger('click');
  expect(wrapper.find('[data-test="bg-card"]').exists()).toBe(true);

  document.body.dispatchEvent(new MouseEvent('click', { bubbles: true }));
  await wrapper.vm.$nextTick();
  expect(wrapper.find('[data-test="bg-card"]').exists()).toBe(false);
  wrapper.unmount();
});

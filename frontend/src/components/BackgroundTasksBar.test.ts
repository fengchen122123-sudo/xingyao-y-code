import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, expect, it, vi } from 'vitest';
import { mount } from '@vue/test-utils';

import BackgroundTasksBar from './BackgroundTasksBar.vue';
import { useAppStore } from '../stores/appStore';

beforeEach(() => {
  setActivePinia(createPinia());
  delete (window as any).pywebview;
});

it('快照字段归一化：duration 映射 running_time，status 恒为 running，输出心跳字段齐备', async () => {
  (window as any).pywebview = {
    api: {
      get_background_tasks: vi.fn().mockResolvedValue([
        {
          task_id: 't1',
          command: 'sleep 30',
          pid: 123,
          duration: '35秒',
          turn_id: 'turn-1',
          session_id: 's1',
          output_size: 2048,
          last_output_age: 2,
          no_output_yet: false,
        },
        {
          task_id: 't2',
          command: 'pytest -q | Select-Object -Last 35',
          duration: '12秒',
          session_id: 's1',
          output_size: 30,
          last_output_age: null,
          no_output_yet: true,
        },
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
      session_id: 's1',
      pid: 123,
      turn_id: 'turn-1',
      output_size: 2048,
      last_output_age: 2,
      no_output_yet: false,
    },
    {
      task_id: 't2',
      command: 'pytest -q | Select-Object -Last 35',
      status: 'running',
      running_time: '12秒',
      session_id: 's1',
      pid: undefined,
      turn_id: '',
      output_size: 30,
      last_output_age: null,
      no_output_yet: true,
    },
  ]);
});

it('任务卡脚注带输出心跳：无实际输出提示等待，有输出显示字节规模', async () => {
  const store = useAppStore();
  store.currentSessionName = 's1';
  store.backgroundTasks = [
    {
      task_id: 't1',
      command: 'pytest -q | Select-Object -Last 35',
      status: 'running',
      running_time: '16分',
      session_id: 's1',
      output_size: 30,
      no_output_yet: true,
    },
    {
      task_id: 't2',
      command: 'npm run dev',
      status: 'running',
      running_time: '35秒',
      session_id: 's1',
      output_size: 2048,
      no_output_yet: false,
    },
  ];
  const wrapper = mount(BackgroundTasksBar);
  await wrapper.find('[data-test="bg-bar-details"]').trigger('click');
  const cardText = wrapper.find('[data-test="bg-card"]').text();
  expect(cardText).toContain('16分 · 等待首行输出');
  expect(cardText).toContain('35秒 · 输出 2.0 KB');
  wrapper.unmount();
});

it('无后台任务时不渲染执行条', () => {
  const wrapper = mount(BackgroundTasksBar);
  expect(wrapper.find('[data-test="bg-bar"]').exists()).toBe(false);
  wrapper.unmount();
});

it('执行条只显示当前会话发起的任务，其他会话/无归属任务不串显', () => {
  const store = useAppStore();
  store.currentSessionName = 's1';
  store.backgroundTasks = [
    { task_id: 't1', command: 'pytest -q', status: 'running', session_id: 's1' },
    { task_id: 't2', command: 'npm run dev', status: 'running', session_id: 's2' },
    { task_id: 't3', command: 'legacy job', status: 'running' },
  ];
  const wrapper = mount(BackgroundTasksBar);

  // 只计本会话的 t1；当前会话为空（草稿）时不显示任何任务
  expect(wrapper.find('[data-test="bg-bar"]').exists()).toBe(true);
  expect(wrapper.find('[data-test="bg-bar-count"]').text()).toContain('1 个后台任务在执行');

  store.currentSessionName = '';
  expect(store.currentSessionBackgroundTasks).toHaveLength(0);
  wrapper.unmount();
});

it('有后台任务时显示数量文案，默认收起卡片，点详情展开', async () => {
  const store = useAppStore();
  store.currentSessionName = 's1';
  store.backgroundTasks = [
    { task_id: 't1', command: 'pytest -q', status: 'running', running_time: '35s', session_id: 's1' },
    { task_id: 't2', command: 'npm run build', status: 'running', session_id: 's1' },
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
  store.currentSessionName = 's1';
  store.backgroundTasks = [{ task_id: 't1', command: 'sleep 30', status: 'running', session_id: 's1' }];
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
  store.currentSessionName = 's1';
  store.backgroundTasks = [{ task_id: 't1', command: 'sleep 30', status: 'running', session_id: 's1' }];
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
  store.currentSessionName = 's1';
  store.backgroundTasks = [{ task_id: 't1', command: 'sleep 30', status: 'running', session_id: 's1' }];
  const wrapper = mount(BackgroundTasksBar);

  await wrapper.find('[data-test="bg-bar-details"]').trigger('click');
  expect(wrapper.find('[data-test="bg-card"]').exists()).toBe(true);

  document.body.dispatchEvent(new MouseEvent('click', { bubbles: true }));
  await wrapper.vm.$nextTick();
  expect(wrapper.find('[data-test="bg-card"]').exists()).toBe(false);
  wrapper.unmount();
});

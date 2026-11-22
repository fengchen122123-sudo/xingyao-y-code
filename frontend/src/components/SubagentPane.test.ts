import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, expect, it, vi } from 'vitest';
import { mount } from '@vue/test-utils';

import SubagentPane from './SubagentPane.vue';
import { useAppStore } from '../stores/appStore';

const LONG_DESC = `任务：给星瑶 GUI 侧边栏 QQ2009 皮肤用户头像加「点击更换头像」功能。${'补充说明。'.repeat(20)}`;

function detailPayload(description: string) {
  return {
    ok: true,
    task: {
      id: 'sub-1',
      description,
      type: 'implement',
      status: 'running',
      stage: 'thinking',
      elapsed: 5,
      background: false,
      started_at: 1758000000,
      stages: [{ name: 'model', at: 1758000000, detail: '' }],
    },
    tasks: [],
  };
}

async function mountPane(description: string) {
  const pinia = createPinia();
  setActivePinia(pinia);
  const getDetail = vi.fn().mockResolvedValue(detailPayload(description));
  (window as any).pywebview = { api: { get_subagent_task_detail: getDetail } };
  const store = useAppStore();
  store.subagentDetailId = 'sub-1';
  const wrapper = mount(SubagentPane, {
    props: { active: true },
    global: { plugins: [pinia] },
  });
  await vi.waitFor(() => {
    expect(wrapper.find('[data-test="subagent-title"]').exists()).toBe(true);
  });
  return wrapper;
}

function stubOverflow(el: HTMLElement, scrollHeight: number, clientHeight: number) {
  Object.defineProperty(el, 'scrollHeight', { value: scrollHeight, configurable: true });
  Object.defineProperty(el, 'clientHeight', { value: clientHeight, configurable: true });
}

function tipElement() {
  return document.body.querySelector('[data-test="subagent-title-tip"]');
}

beforeEach(() => {
  setActivePinia(createPinia());
  delete (window as any).pywebview;
});

it('标题被 5 行截断时：hover 弹出气泡显示全文，移出后收起', async () => {
  const wrapper = await mountPane(LONG_DESC);
  const title = wrapper.get('[data-test="subagent-title"]');
  expect(title.text()).toBe(LONG_DESC);
  expect(tipElement()).toBeNull();

  stubOverflow(title.element as HTMLElement, 240, 100);
  await title.trigger('mouseenter');
  expect(tipElement()?.textContent).toBe(LONG_DESC);

  await title.trigger('mouseleave');
  await vi.waitFor(() => {
    expect(tipElement()).toBeNull();
  });
  wrapper.unmount();
});

it('标题未被截断（内容不足 5 行）时不弹气泡', async () => {
  const wrapper = await mountPane('整理测试报告');
  const title = wrapper.get('[data-test="subagent-title"]');
  stubOverflow(title.element as HTMLElement, 20, 20);

  await title.trigger('mouseenter');
  expect(tipElement()).toBeNull();
  wrapper.unmount();
});

it('气泡可悬停：鼠标进入气泡不收起，移出后收起', async () => {
  const wrapper = await mountPane(LONG_DESC);
  const title = wrapper.get('[data-test="subagent-title"]');
  stubOverflow(title.element as HTMLElement, 240, 100);

  await title.trigger('mouseenter');
  const tip = tipElement() as HTMLElement;
  expect(tip).not.toBeNull();

  await title.trigger('mouseleave');
  tip.dispatchEvent(new MouseEvent('mouseenter'));
  await new Promise((resolve) => setTimeout(resolve, 200));
  expect(tipElement()).not.toBeNull();

  tip.dispatchEvent(new MouseEvent('mouseleave'));
  await vi.waitFor(() => {
    expect(tipElement()).toBeNull();
  });
  wrapper.unmount();
});

it('结构化轮次视图：展示动作目标、展开工具参数与错误详情、仅看失败筛选与停止操作', async () => {
  const pinia = createPinia();
  setActivePinia(pinia);
  const cancelTask = vi.fn().mockResolvedValue({ ok: true, status: 'cancelled', msg: '已停止该子代理' });
  const getDetail = vi.fn().mockResolvedValue({
    ok: true,
    task: {
      id: 'sub-rich',
      description: '工作目录 E:\\repo，分支 dev。目标：发布校验并上传',
      type: 'implement',
      status: 'running',
      stage: 'waiting_approval',
      elapsed: 132,
      execution_elapsed: 90,
      approval_wait_elapsed: 42,
      timeout_seconds: 600,
      background: true,
      model_name: 'gpt-4.1-mini',
      approval_summary: '执行命令：git tag v1.4.2',
      rounds: 2,
      tool_ok: 1,
      tool_err: 1,
      started_at: 1758000000,
      stages: [],
      changed_files: ['dist/manifest.json'],
      tool_records: [
        {
          id: 'c1', seq: 1, round: 1, name: 'search_code', category: 'search',
          action: '搜索代码', target: 'release_parts', args_preview: 'query: release_parts',
          status: 'ok', started_at: 1758000002, duration_text: '31ms', result_preview: '命中 3 处',
        },
        {
          id: 'c2', seq: 2, round: 2, name: 'execute_command', category: 'command',
          action: '运行命令', target: 'pytest', args_preview: 'command: pytest',
          status: 'err', started_at: 1758000010, error_summary: 'KeyError: TOKEN',
          result_preview: 'Traceback: KeyError TOKEN',
        },
      ],
      result: '',
      error: '',
      partial: '已核对前 6 个分卷。',
    },
    tasks: [
      { id: 'sub-rich', description: '发布校验并上传', type: 'implement', status: 'running', stage: 'waiting_approval', elapsed: 132, background: true },
    ],
  });
  (window as any).pywebview = {
    api: { get_subagent_task_detail: getDetail, cancel_subagent_task: cancelTask },
  };
  const store = useAppStore();
  store.workdir = 'E:\\repo';
  store.subagentDetailId = 'sub-rich';

  const wrapper = mount(SubagentPane, {
    props: { active: true },
    global: { plugins: [pinia] },
  });

  await vi.waitFor(() => {
    expect(wrapper.find('[data-test="subagent-tool-c1"]').exists()).toBe(true);
  });

  expect(wrapper.find('[data-test="subagent-wait-banner"]').text()).toContain('git tag v1.4.2');
  expect(wrapper.text()).toContain('Round 1');
  expect(wrapper.text()).toContain('搜索代码');
  expect(wrapper.text()).toContain('release_parts');
  expect(wrapper.text()).toContain('KeyError: TOKEN');

  // 点击工具展开调用参数与错误详情
  await wrapper.get('[data-test="subagent-tool-c2"] .suba-tool-btn').trigger('click');
  expect(wrapper.text()).toContain('Traceback: KeyError TOKEN');

  // 仅看失败筛选隐藏 Round 1 的成功调用
  await wrapper.get('[data-test="subagent-filter-failed"]').trigger('click');
  expect(wrapper.find('[data-test="subagent-tool-c1"]').exists()).toBe(false);
  expect(wrapper.find('[data-test="subagent-tool-c2"]').exists()).toBe(true);

  // 切换到改动文件列表并预览文件
  await wrapper.get('[data-test="subagent-filter-files"]').trigger('click');
  expect(wrapper.find('[data-test="subagent-files"]').text()).toContain('dist/manifest.json');

  // 点击停止按钮触发协作取消
  await wrapper.get('[data-test="subagent-stop"]').trigger('click');
  expect(cancelTask).toHaveBeenCalledWith('sub-rich', store.currentSessionName);
  wrapper.unmount();
});

it('未指定任务打开总览清单，点选任务进入详情且终态停止轮询', async () => {
  vi.useFakeTimers();
  try {
    const pinia = createPinia();
    setActivePinia(pinia);
    const getDetail = vi.fn().mockImplementation(async (taskId: string) => {
      if (!taskId) {
        return {
          ok: true,
          task: null,
          tasks: [
            { id: 'sub-done', description: '构建产物', type: 'explore', status: 'completed', stage: 'completed', elapsed: 40, background: false, rounds: 2, tool_ok: 3, tool_err: 0 },
          ],
        };
      }
      return {
        ok: true,
        task: {
          id: 'sub-done',
          description: '构建产物',
          type: 'explore',
          status: 'completed',
          stage: 'completed',
          elapsed: 40,
          background: false,
          stages: [],
          tool_records: [],
          result: '### 完成\n- 产出 **v1.4.2**',
          error: '',
          partial: '',
        },
        tasks: [
          { id: 'sub-done', description: '构建产物', type: 'explore', status: 'completed', stage: 'completed', elapsed: 40, background: false },
        ],
      };
    });
    (window as any).pywebview = { api: { get_subagent_task_detail: getDetail } };
    const store = useAppStore();
    store.subagentDetailId = '';

    const wrapper = mount(SubagentPane, {
      props: { active: true },
      global: { plugins: [pinia] },
    });

    await vi.waitFor(() => {
      expect(wrapper.find('[data-test="subagent-hub-item-sub-done"]').exists()).toBe(true);
    });

    await wrapper.get('[data-test="subagent-hub-item-sub-done"]').trigger('click');
    await vi.waitFor(() => {
      expect(wrapper.find('[data-test="subagent-result"]').exists()).toBe(true);
    });
    expect(wrapper.find('[data-test="subagent-result"] h4').text()).toBe('完成');
    expect(wrapper.find('[data-test="subagent-result"] strong').text()).toBe('v1.4.2');

    const callsAtTerminal = getDetail.mock.calls.length;
    await vi.advanceTimersByTimeAsync(3500);
    expect(getDetail.mock.calls.length).toBe(callsAtTerminal);

    await wrapper.get('[data-test="subagent-full-result"]').trigger('click');
    expect(document.body.querySelector('[data-test="subagent-full-modal"]')).not.toBeNull();
    wrapper.unmount();
  } finally {
    vi.useRealTimers();
  }
});

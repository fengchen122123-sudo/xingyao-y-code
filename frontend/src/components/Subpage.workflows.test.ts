import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushPromises, mount } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';

import Subpage from './Subpage.vue';
import { useAppStore } from '../stores/appStore';

function mountSubpage(api: Record<string, unknown> = {}) {
  (window as any).pywebview = { api };
  return mount(Subpage, {
    props: {
      visible: true,
      subType: 'workflows' as const,
    },
  });
}

// 星流面板如今是星轨子页里的一个标签：从星轨入口进入后点标签切换过来。
// openSubpage 改的是 store.subpage，而 subType prop 由 App.vue 绑定 ——
// 测试里用 setProps 模拟 App 层对 store 变化的响应。
async function mountViaTab(api: Record<string, unknown> = {}) {
  (window as any).pywebview = { api };
  const wrapper = mount(Subpage, {
    props: { visible: true, subType: 'automations' as const },
  });
  await wrapper.find('[data-test="auto-tab-workflows"]').trigger('click');
  await wrapper.setProps({ subType: 'workflows' as const });
  await flushPromises();
  return wrapper;
}

const _workflow = {
  name: 'pr-review',
  scope: 'user',
  path: 'C:/home/.ycode/workflows/pr-review.dwf.ts',
  description: 'Review changes against a base ref.',
  when_to_use: 'Use when the user asks to review changes.',
  args: {
    base: { type: 'string', description: 'Git ref.', required: true },
    maxRounds: { type: 'number', default: 3 },
  },
} as any;

const _invalid = { name: 'broken', scope: 'user', error: 'invalid_metadata: description 必填且不能为空' } as any;

describe('Subpage - 星流工作流', () => {
  beforeEach(() => {
    setActivePinia(createPinia());
  });

  afterEach(() => {
    delete (window as any).pywebview;
  });

  it('从星轨标签页切入星流：分组空态与对话创建入口', async () => {
    const wrapper = await mountViaTab();
    // tab 切换走真实 openSubpage（幂等置值），星流面板渲染
    expect(wrapper.find('[data-test="workflow-import-btn"]').exists()).toBe(true);
    expect(wrapper.find('[data-test="workflow-group-user"]').exists()).toBe(true);
    expect(wrapper.text()).toContain('还没有全局工作流');
  });

  it('渲染分组标题、空态与通过对话创建按钮', () => {
    const wrapper = mountSubpage();
    expect(wrapper.text()).toContain('已保存的工作流');
    expect(wrapper.text()).toContain('全局');
    expect(wrapper.text()).toContain('对所有项目可见');
    expect(wrapper.text()).toContain('还没有全局工作流');
    // 自由对话（无项目目录）下不显示项目组
    expect(wrapper.find('[data-test="workflow-group-project"]').exists()).toBe(false);
    expect(wrapper.find('[data-test="workflow-create-user"]').exists()).toBe(true);
  });

  it('通过对话创建：预填输入框草稿并关闭子页（不直接发送）', async () => {
    const store = useAppStore();
    store.workflows = [];
    store.workflowInvalid = [];
    store.workflowsLoading = false;
    const prefillSpy = vi.fn();
    const closeSpy = vi.fn();
    const sendSpy = vi.fn();
    store.prefillComposer = prefillSpy as any;
    store.closeSubpage = closeSpy as any;
    store.sendMessage = sendSpy as any;

    const wrapper = mountSubpage();
    await wrapper.find('[data-test="workflow-create-user"]').trigger('click');

    expect(prefillSpy).toHaveBeenCalledWith('帮我创建一个工作流：');
    expect(closeSpy).toHaveBeenCalled();
    expect(sendSpy).not.toHaveBeenCalled(); // 只写草稿，发送交给用户
  });

  it('修改入口：带工作流名预填输入框并关闭子页', async () => {
    const store = useAppStore();
    store.workflows = [_workflow];
    store.workflowInvalid = [];
    store.workflowsLoading = false;
    const prefillSpy = vi.fn();
    const closeSpy = vi.fn();
    store.prefillComposer = prefillSpy as any;
    store.closeSubpage = closeSpy as any;

    const wrapper = mountSubpage();
    await wrapper.find('[data-test="workflow-edit-pr-review"]').trigger('click');

    const draft = prefillSpy.mock.calls[0][0] as string;
    expect(draft).toContain('「pr-review」');
    expect(draft).toContain('workflow_save');
    expect(closeSpy).toHaveBeenCalled();
  });

  it('项目组：有项目目录时显示，含项目空态卡片与对话创建按钮', () => {
    const store = useAppStore();
    store.workflows = [];
    store.workflowInvalid = [];
    store.workflowsLoading = false;
    store.workdir = 'E:/work/demo';
    const wrapper = mountSubpage();
    expect(wrapper.find('[data-test="workflow-group-project"]').exists()).toBe(true);
    expect(wrapper.text()).toContain('demo'); // 项目名作为组标题
    expect(wrapper.text()).toContain('当前项目里还没有保存的工作流');
    expect(wrapper.find('[data-test="workflow-create-project-empty"]').exists()).toBe(true);
  });

  it('打开页面时拉取工作流清单并渲染卡片/参数表/坏文件', async () => {
    const store = useAppStore();
    const refreshSpy = vi.fn().mockResolvedValue(true);
    store.refreshWorkflows = refreshSpy as any;
    store.workflows = [_workflow];
    store.workflowInvalid = [_invalid];

    (window as any).pywebview = { api: {} };
    const wrapper = mount(Subpage, { props: { visible: false, subType: 'workflows' as const } });
    await wrapper.setProps({ visible: true });
    await flushPromises();

    expect(refreshSpy).toHaveBeenCalled();
    expect(wrapper.find('[data-test="workflow-card-pr-review"]').exists()).toBe(true);
    expect(wrapper.text()).toContain('Review changes against a base ref.');
    expect(wrapper.text()).toContain('base');
    expect(wrapper.text()).toContain('string *');
    expect(wrapper.text()).toContain('默认 3');
    expect(wrapper.text()).toContain('解析失败');
    expect(wrapper.text()).toContain('invalid_metadata');
  });

  it('运行表单：必填校验拦截 + 发送参数化 prompt 到当前会话', async () => {
    const store = useAppStore();
    store.workflows = [_workflow];
    store.workflowInvalid = [];
    store.workflowsLoading = false;
    const sendSpy = vi.fn().mockResolvedValue(undefined);
    const closeSpy = vi.fn();
    store.sendMessage = sendSpy as any;
    store.closeSubpage = closeSpy as any;

    const wrapper = mountSubpage();
    await wrapper.find('[data-test="workflow-run-pr-review"]').trigger('click');

    // 必填未填：拦截且不发消息
    await wrapper.find('[data-test="workflow-run-send"]').trigger('click');
    expect(sendSpy).not.toHaveBeenCalled();
    expect(wrapper.text()).toContain('参数 base 必填');

    // 填必填后发送：prompt 携带工作流名与参数；默认值留空不注入
    await wrapper.find('[data-test="workflow-arg-base"]').setValue('origin/main');
    await wrapper.find('[data-test="workflow-run-send"]').trigger('click');
    expect(sendSpy).toHaveBeenCalledTimes(1);
    const prompt = sendSpy.mock.calls[0][0] as string;
    expect(prompt).toContain('「pr-review」');
    expect(prompt).toContain('base=origin/main');
    expect(prompt).not.toContain('maxRounds');
    expect(closeSpy).toHaveBeenCalled();
  });

  it('导入按钮：选文件 → 调导入 → 展示结果摘要', async () => {
    const store = useAppStore();
    store.workflows = [];
    store.workflowInvalid = [];
    store.workflowsLoading = false;
    const pickSpy = vi.fn().mockResolvedValue(['C:/pick/pr-review.dwf.ts']);
    const importSpy = vi.fn().mockResolvedValue({
      imported: [{ name: 'pr-review', path: 'p' }],
      failed: [],
    });
    const notifySpy = vi.fn();
    store.pickWorkflowFiles = pickSpy as any;
    store.importWorkflows = importSpy as any;
    store.notifyModal = notifySpy as any;

    const wrapper = mountSubpage();
    await wrapper.find('[data-test="workflow-import-btn"]').trigger('click');
    await flushPromises();

    expect(pickSpy).toHaveBeenCalled();
    expect(importSpy).toHaveBeenCalledWith(['C:/pick/pr-review.dwf.ts'], 'user');
    const summary = notifySpy.mock.calls[0][1] as string;
    expect(summary).toContain('pr-review');
  });

  it('运行历史：展开时拉取台账并渲染状态行', async () => {
    const store = useAppStore();
    store.workflows = [_workflow];
    store.workflowInvalid = [];
    store.workflowsLoading = false;
    const loadSpy = vi.fn().mockImplementation(async (name: string) => {
      store.workflowRuns[name] = [
        {
          id: 'wfr-abc',
          workflow_name: name,
          scope: 'user',
          session_name: '主会话',
          args: { base: 'origin/main' },
          status: 'completed',
          summary: '2 处发现',
          error: '',
          artifact_dir: 'E:/p/.ycode/workflow-artifacts/wfr-abc',
          started_at: '2026-10-25 12:00:00',
          finished_at: '2026-10-25 12:03:30',
        },
      ];
      return true;
    });
    store.loadWorkflowRuns = loadSpy as any;

    const wrapper = mountSubpage();
    await wrapper.find('[data-test="workflow-history-pr-review"]').trigger('click');
    await flushPromises();

    expect(loadSpy).toHaveBeenCalledWith('pr-review');
    const box = wrapper.find('[data-test="workflow-history-box-pr-review"]');
    expect(box.exists()).toBe(true);
    expect(box.text()).toContain('已完成');
    expect(box.text()).toContain('2 处发现');
    expect(box.text()).toContain('base=origin/main');
    expect(box.text()).toContain('3m30s');

    // 再点收起
    await wrapper.find('[data-test="workflow-history-pr-review"]').trigger('click');
    expect(wrapper.find('[data-test="workflow-history-box-pr-review"]').exists()).toBe(false);
  });

  it('删除按钮：确认后调用 store.deleteWorkflow', async () => {
    const store = useAppStore();
    store.workflows = [_workflow];
    store.workflowInvalid = [];
    store.workflowsLoading = false;
    const deleteSpy = vi.fn().mockResolvedValue(true);
    store.askConfirm = vi.fn().mockResolvedValue(true) as any;
    store.deleteWorkflow = deleteSpy as any;

    const wrapper = mountSubpage();
    await wrapper.find('[data-test="workflow-delete-pr-review"]').trigger('click');
    await flushPromises();

    expect(deleteSpy).toHaveBeenCalledWith('pr-review', 'user');
  });
});

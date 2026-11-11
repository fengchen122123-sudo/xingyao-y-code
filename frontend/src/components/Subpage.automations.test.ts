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
      subType: 'automations' as const,
    },
  });
}

const _task = {
  id: 'task-1',
  name: '每日摘要',
  enabled: true,
  trigger_type: 'daily',
  trigger_config: { time: '09:30' },
  target_session: '星轨自动化任务',
  prompt: '汇总昨日进展',
  permission_mode: 'confirm',
  missed_run_policy: 'skip',
  notify_on_success: false,
  created_at: '2026-09-12 10:00:00',
  updated_at: '2026-09-12 10:00:00',
  last_run_at: null,
  next_run_at: 1789224000,
  trigger_summary: '每天 09:30',
} as any;

describe('Subpage - 星轨自动化任务', () => {
  beforeEach(() => {
    setActivePinia(createPinia());
  });

  afterEach(() => {
    delete (window as any).pywebview;
  });

  it('渲染标题、说明条与空态', () => {
    const wrapper = mountSubpage();
    expect(wrapper.text()).toContain('星轨自动化任务');
    expect(wrapper.text()).toContain('还没有星轨自动化任务');
  });

  it('页内提供星轨/星流双标签，当前标签高亮', () => {
    const wrapper = mountSubpage();
    const tabs = wrapper.find('[data-test="auto-tabs"]');
    expect(tabs.exists()).toBe(true);
    expect(wrapper.find('[data-test="auto-tab-automations"]').classes()).toContain('active');
    expect(wrapper.find('[data-test="auto-tab-workflows"]').classes()).not.toContain('active');
  });

  it('点星流标签切到工作流面板（openSubpage 幂等切 subType 不关子页）', async () => {
    const store = useAppStore();
    const openSpy = vi.fn();
    store.openSubpage = openSpy as any;

    const wrapper = mountSubpage();
    await wrapper.find('[data-test="auto-tab-workflows"]').trigger('click');
    expect(openSpy).toHaveBeenCalledWith('workflows');
  });

  it('打开页面时拉取任务列表并渲染任务卡', async () => {
    const store = useAppStore();
    const refreshSpy = vi.fn().mockResolvedValue(true);
    store.refreshAutomations = refreshSpy as any;
    store.automationTasks = [_task];

    // watch 在 visible 从 false 翻转为 true 时触发（与 MCP/插件页一致）
    (window as any).pywebview = { api: {} };
    const wrapper = mount(Subpage, { props: { visible: false, subType: 'automations' as const } });
    await wrapper.setProps({ visible: true });
    await flushPromises();

    expect(refreshSpy).toHaveBeenCalled();
    expect(wrapper.find('[data-test="automation-card-每日摘要"]').exists()).toBe(true);
    expect(wrapper.text()).toContain('每天 09:30');
    expect(wrapper.find('[data-test="automation-toggle-每日摘要"]').exists()).toBe(true);
  });

  it('立即执行调用 store 并展开历史', async () => {
    const store = useAppStore();
    store.automationTasks = [_task];
    const runSpy = vi.fn().mockResolvedValue({ ok: true });
    store.runAutomationTaskNow = runSpy as any;

    const wrapper = mountSubpage();
    await wrapper.find('[data-test="automation-run-每日摘要"]').trigger('click');

    expect(runSpy).toHaveBeenCalledWith('task-1');
  });

  it('表单校验：空提示词拦截，不调用保存', async () => {
    const store = useAppStore();
    const saveSpy = vi.fn();
    store.saveAutomationTask = saveSpy as any;

    const wrapper = mountSubpage();
    await wrapper.find('[data-test="automation-new-btn"]').trigger('click');
    await wrapper.find('[data-test="automation-name"]').setValue('测试任务');
    await wrapper.find('[data-test="automation-save"]').trigger('click');

    expect(saveSpy).not.toHaveBeenCalled();
    expect(wrapper.find('[data-test="automation-form-errors"]').exists()).toBe(true);
  });

  it('表单提交调用 store.saveAutomationTask 并携带触发器配置', async () => {
    const store = useAppStore();
    const saveSpy = vi.fn().mockResolvedValue({ ok: true });
    store.saveAutomationTask = saveSpy as any;
    store.sessionGroups = [];

    const wrapper = mountSubpage();
    await wrapper.find('[data-test="automation-new-btn"]').trigger('click');
    await wrapper.find('[data-test="automation-name"]').setValue('测试任务');
    (wrapper.find('[data-test="automation-new-session"]').element as HTMLInputElement).value = '';
    await wrapper.find('[data-test="automation-new-session"]').setValue('自动会话');
    await wrapper.find('[data-test="automation-prompt"]').setValue('干点活');
    await wrapper.find('[data-test="automation-save"]').trigger('click');

    expect(saveSpy).toHaveBeenCalledTimes(1);
    const payload = saveSpy.mock.calls[0][0];
    expect(payload.name).toBe('测试任务');
    expect(payload.target_session).toBe('自动会话');
    expect(payload.prompt).toBe('干点活');
    expect(payload.trigger_type).toBe('daily');
    expect(payload.trigger_config).toEqual({ time: '09:30' });
    expect(payload.permission_mode).toBe('confirm');
    // 新建会话默认自由对话归属，不再落到应用启动目录
    expect(payload.workdir).toBe('GLOBAL');
    // 未选择模型时跟随会话绑定/全局默认
    expect(payload.model_id).toBe('');
  });

  it('选择执行模型后保存携带模型 id', async () => {
    const store = useAppStore();
    const saveSpy = vi.fn().mockResolvedValue({ ok: true });
    store.saveAutomationTask = saveSpy as any;
    store.sessionGroups = [];
    store.models = [{
      id: 'm1', name: 'GLM-5.3', provider_id: 'p1', provider_name: '智谱',
      context_window: 128000, supports_vision: false,
      supports_function_calling: false, computer_protocol: 'none',
    }] as any;

    const wrapper = mountSubpage();
    await wrapper.find('[data-test="automation-new-btn"]').trigger('click');
    await wrapper.find('[data-test="automation-name"]').setValue('测试任务');
    await wrapper.find('[data-test="automation-new-session"]').setValue('自动会话');
    await wrapper.find('[data-test="automation-prompt"]').setValue('干点活');
    await wrapper.find('[data-test="automation-model"]').setValue('m1');
    await wrapper.find('[data-test="automation-save"]').trigger('click');

    expect(saveSpy).toHaveBeenCalledTimes(1);
    expect(saveSpy.mock.calls[0][0].model_id).toBe('m1');
  });

  it('选择 accept_edits 档位时展示风险说明', async () => {
    const store = useAppStore();
    store.sessionGroups = [];

    const wrapper = mountSubpage();
    await wrapper.find('[data-test="automation-new-btn"]').trigger('click');
    await wrapper.find('[data-test="automation-permission"]').setValue('accept_edits');

    expect(wrapper.find('[data-test="automation-risk-note"]').exists()).toBe(true);
    expect(wrapper.find('[data-test="automation-risk-note"]').text()).toContain('修改文件');
  });

  it('权限档位与对话页同文案，可选计划模式且不展示风险提示', async () => {
    const store = useAppStore();
    const saveSpy = vi.fn().mockResolvedValue({ ok: true });
    store.saveAutomationTask = saveSpy as any;
    store.sessionGroups = [];

    const wrapper = mountSubpage();
    await wrapper.find('[data-test="automation-new-btn"]').trigger('click');

    const sel = wrapper.find('[data-test="automation-permission"]');
    expect(sel.text()).toContain('变更前确认');
    expect(sel.text()).toContain('自动编辑');
    expect(sel.text()).toContain('计划模式');
    expect(sel.text()).toContain('完全访问');

    // 计划模式只读，不属于无人值守改文件风险档位
    await sel.setValue('plan');
    expect(wrapper.find('[data-test="automation-risk-note"]').exists()).toBe(false);

    await wrapper.find('[data-test="automation-name"]').setValue('测试任务');
    await wrapper.find('[data-test="automation-new-session"]').setValue('自动会话');
    await wrapper.find('[data-test="automation-prompt"]').setValue('干点活');
    await wrapper.find('[data-test="automation-save"]').trigger('click');
    expect(saveSpy).toHaveBeenCalledTimes(1);
    expect(saveSpy.mock.calls[0][0].permission_mode).toBe('plan');
  });

  it('新建会话展示工作目录行；指定目录但未选择时拦截保存', async () => {
    const store = useAppStore();
    const saveSpy = vi.fn();
    store.saveAutomationTask = saveSpy as any;
    store.sessionGroups = [];

    const wrapper = mountSubpage();
    await wrapper.find('[data-test="automation-new-btn"]').trigger('click');
    await wrapper.find('[data-test="automation-name"]').setValue('测试任务');
    await wrapper.find('[data-test="automation-new-session"]').setValue('自动会话');
    await wrapper.find('[data-test="automation-prompt"]').setValue('干点活');

    expect(wrapper.find('[data-test="automation-workdir-row"]').exists()).toBe(true);

    await wrapper.find('[data-test="automation-workdir-mode"]').setValue('folder');
    await wrapper.find('[data-test="automation-save"]').trigger('click');

    expect(saveSpy).not.toHaveBeenCalled();
    expect(wrapper.find('[data-test="automation-form-errors"]').text()).toContain('项目文件夹');
  });

  it('指定项目文件夹后保存携带该目录', async () => {
    const store = useAppStore();
    const saveSpy = vi.fn().mockResolvedValue({ ok: true });
    store.saveAutomationTask = saveSpy as any;
    store.sessionGroups = [];

    const wrapper = mountSubpage();
    await wrapper.find('[data-test="automation-new-btn"]').trigger('click');
    await wrapper.find('[data-test="automation-name"]').setValue('测试任务');
    await wrapper.find('[data-test="automation-new-session"]').setValue('自动会话');
    await wrapper.find('[data-test="automation-prompt"]').setValue('干点活');
    await wrapper.find('[data-test="automation-workdir-mode"]').setValue('folder');
    await wrapper.find('[data-test="automation-workdir-input"]').setValue('E:\\proj');
    await wrapper.find('[data-test="automation-save"]').trigger('click');

    expect(saveSpy).toHaveBeenCalledTimes(1);
    expect(saveSpy.mock.calls[0][0].workdir).toBe('E:\\proj');
  });

  it('浏览按钮走 store.pickFolderPlain 并回填所选目录', async () => {
    const store = useAppStore();
    store.sessionGroups = [];
    store.pickFolderPlain = ((cb: (p: string) => void) => { cb('D:\\work'); }) as any;

    const wrapper = mountSubpage();
    await wrapper.find('[data-test="automation-new-btn"]').trigger('click');
    await wrapper.find('[data-test="automation-workdir-mode"]').setValue('folder');
    await wrapper.find('[data-test="automation-workdir-pick"]').trigger('click');

    expect((wrapper.find('[data-test="automation-workdir-input"]').element as HTMLInputElement).value).toBe('D:\\work');
  });

  it('已有会话目标不携带 workdir 并展示归属目录提示', async () => {
    const store = useAppStore();
    const saveSpy = vi.fn().mockResolvedValue({ ok: true });
    store.saveAutomationTask = saveSpy as any;
    store.sessionGroups = [{
      folder_path: 'E:\\proj',
      folder_name: 'proj',
      sessions: [{ name: '既有会话', display_name: '既有会话', count: 3, workdir: 'E:\\proj' }],
    }] as any;

    const wrapper = mountSubpage();
    await wrapper.find('[data-test="automation-new-btn"]').trigger('click');
    // 默认选中第一个既有会话
    await wrapper.find('[data-test="automation-name"]').setValue('测试任务');
    await wrapper.find('[data-test="automation-prompt"]').setValue('干点活');
    expect(wrapper.text()).toContain('该会话归属项目目录：E:\\proj');
    await wrapper.find('[data-test="automation-save"]').trigger('click');

    expect(saveSpy).toHaveBeenCalledTimes(1);
    expect(saveSpy.mock.calls[0][0].target_session).toBe('既有会话');
    expect(saveSpy.mock.calls[0][0].workdir).toBe('');
  });

  it('任务卡按 workdir 展示自由对话或目录标识', async () => {
    const store = useAppStore();
    store.models = [{
      id: 'm1', name: 'GLM-5.3', provider_id: 'p1', provider_name: '智谱',
      context_window: 128000, supports_vision: false,
      supports_function_calling: false, computer_protocol: 'none',
    }] as any;
    store.automationTasks = [
      { ..._task, id: 't-g', name: '对话任务', workdir: 'GLOBAL', model_id: 'm1' },
      { ..._task, id: 't-p', name: '项目任务', workdir: 'E:\\proj' },
    ] as any;

    const wrapper = mountSubpage();
    await flushPromises();

    expect(wrapper.text()).toContain('自由对话');
    expect(wrapper.text()).toContain('📁 proj');
    // 指定模型的任务卡展示模型名（供应商）
    expect(wrapper.text()).toContain('🧠 GLM-5.3（智谱）');
  });

  it('选择一次性触发后出现「N 分钟后」输入，保存只传 delay_minutes', async () => {
    const store = useAppStore();
    const saveSpy = vi.fn().mockResolvedValue({ ok: true });
    store.saveAutomationTask = saveSpy as any;
    store.sessionGroups = [];

    const wrapper = mountSubpage();
    await wrapper.find('[data-test="automation-new-btn"]').trigger('click');
    await wrapper.find('[data-test="automation-trigger-type"]').setValue('once');

    // 默认形态是「N 分钟后」；绝对时刻输入此时不渲染（二选一）
    expect(wrapper.find('[data-test="automation-delay-minutes"]').exists()).toBe(true);
    expect(wrapper.find('[data-test="automation-run-at"]').exists()).toBe(false);

    await wrapper.find('[data-test="automation-name"]').setValue('一次性任务');
    await wrapper.find('[data-test="automation-new-session"]').setValue('自动会话');
    await wrapper.find('[data-test="automation-prompt"]').setValue('干点活');
    await wrapper.find('[data-test="automation-delay-minutes"]').setValue('15');
    await wrapper.find('[data-test="automation-save"]').trigger('click');

    expect(saveSpy).toHaveBeenCalledTimes(1);
    const payload = saveSpy.mock.calls[0][0];
    expect(payload.trigger_type).toBe('once');
    // 只传其中一个键：延迟形态不带 run_at
    expect(payload.trigger_config).toEqual({ delay_minutes: 15 });
  });

  it('一次性触发切到绝对时刻时保存传 run_at（空格分隔的本地时刻）', async () => {
    const store = useAppStore();
    const saveSpy = vi.fn().mockResolvedValue({ ok: true });
    store.saveAutomationTask = saveSpy as any;
    store.sessionGroups = [];

    const wrapper = mountSubpage();
    await wrapper.find('[data-test="automation-new-btn"]').trigger('click');
    await wrapper.find('[data-test="automation-trigger-type"]').setValue('once');
    await wrapper.find('[data-test="automation-once-mode"]').setValue('absolute');

    expect(wrapper.find('[data-test="automation-run-at"]').exists()).toBe(true);
    expect(wrapper.find('[data-test="automation-delay-minutes"]').exists()).toBe(false);

    await wrapper.find('[data-test="automation-name"]').setValue('一次性任务');
    await wrapper.find('[data-test="automation-new-session"]').setValue('自动会话');
    await wrapper.find('[data-test="automation-prompt"]').setValue('干点活');
    await wrapper.find('[data-test="automation-run-at"]').setValue('2030-01-02T08:15');
    await wrapper.find('[data-test="automation-save"]').trigger('click');

    expect(saveSpy).toHaveBeenCalledTimes(1);
    const payload = saveSpy.mock.calls[0][0];
    expect(payload.trigger_type).toBe('once');
    // 绝对时刻形态不带 delay_minutes，且 datetime-local 的 T 换成后端认的空格
    expect(payload.trigger_config).toEqual({ run_at: '2030-01-02 08:15' });
  });

  it('最多执行次数留空保存为 null，填写后按整数下发', async () => {
    const store = useAppStore();
    const saveSpy = vi.fn().mockResolvedValue({ ok: true });
    store.saveAutomationTask = saveSpy as any;
    store.sessionGroups = [];

    const wrapper = mountSubpage();

    const fillAndSave = async (maxRuns?: string) => {
      await wrapper.find('[data-test="automation-new-btn"]').trigger('click');
      await wrapper.find('[data-test="automation-name"]').setValue('限次任务');
      await wrapper.find('[data-test="automation-new-session"]').setValue('自动会话');
      await wrapper.find('[data-test="automation-prompt"]').setValue('干点活');
      const input = wrapper.find('[data-test="automation-max-runs"]');
      expect(input.exists()).toBe(true);
      expect(wrapper.text()).toContain('手动「立即执行」不计入次数');
      if (maxRuns !== undefined) await input.setValue(maxRuns);
      await wrapper.find('[data-test="automation-save"]').trigger('click');
      return saveSpy.mock.calls[saveSpy.mock.calls.length - 1][0];
    };

    // 留空 = 不限次
    expect((await fillAndSave()).max_runs).toBeNull();
    expect((await fillAndSave('3')).max_runs).toBe(3);
  });

  it('已完成任务卡展示「已完成」徽标并禁用立即执行与启停', async () => {
    const store = useAppStore();
    store.automationTasks = [{
      ..._task,
      id: 'task-done',
      name: '跑完的任务',
      max_runs: 3,
      scheduled_run_count: 3,
      status: 'completed',
    }] as any;
    const runSpy = vi.fn().mockResolvedValue({ ok: true });
    store.runAutomationTaskNow = runSpy as any;

    const wrapper = mountSubpage();
    const card = wrapper.find('[data-test="automation-card-跑完的任务"]');
    expect(card.exists()).toBe(true);

    const badge = card.find('[data-test="automation-task-status"]');
    expect(badge.exists()).toBe(true);
    expect(badge.text()).toContain('已完成');

    const runBtn = card.find('[data-test="automation-run-跑完的任务"]');
    expect(runBtn.attributes('disabled')).toBeDefined();
    const toggle = card.find('[data-test="automation-toggle-跑完的任务"]').element as HTMLInputElement;
    expect(toggle.disabled).toBe(true);

    // 置灰后点击不会真正派发
    await runBtn.trigger('click');
    expect(runSpy).not.toHaveBeenCalled();
  });

  it('有限次任务卡展示「已执行 1/3 次」进度', async () => {
    const store = useAppStore();
    store.automationTasks = [{
      ..._task,
      id: 'task-limit',
      name: '限次任务',
      max_runs: 3,
      scheduled_run_count: 1,
      status: 'active',
    }] as any;

    const wrapper = mountSubpage();
    const card = wrapper.find('[data-test="automation-card-限次任务"]');
    expect(card.exists()).toBe(true);
    expect(card.text()).toContain('已执行 1/3 次');
    // 未达上限不展示生命周期徽标
    expect(card.find('[data-test="automation-task-status"]').exists()).toBe(false);
  });

  it('一次性触发时刻已过时，把后端返回的 msg 展示在表单错误位', async () => {
    const store = useAppStore();
    const saveSpy = vi.fn().mockResolvedValue({
      ok: false,
      msg: '一次性触发时刻已过（2020-01-01 00:00），请改用 delay_minutes 指定「距现在多少分钟」后重试',
    });
    store.saveAutomationTask = saveSpy as any;
    store.sessionGroups = [];

    const wrapper = mountSubpage();
    await wrapper.find('[data-test="automation-new-btn"]').trigger('click');
    await wrapper.find('[data-test="automation-name"]').setValue('一次性任务');
    await wrapper.find('[data-test="automation-new-session"]').setValue('自动会话');
    await wrapper.find('[data-test="automation-prompt"]').setValue('干点活');
    await wrapper.find('[data-test="automation-trigger-type"]').setValue('once');
    await wrapper.find('[data-test="automation-once-mode"]').setValue('absolute');
    await wrapper.find('[data-test="automation-run-at"]').setValue('2020-01-01T00:00');
    await wrapper.find('[data-test="automation-save"]').trigger('click');

    expect(saveSpy).toHaveBeenCalledTimes(1);
    expect(saveSpy.mock.calls[0][0].trigger_config).toEqual({ run_at: '2020-01-01 00:00' });
    const err = wrapper.find('[data-test="automation-form-errors"]');
    expect(err.exists()).toBe(true);
    expect(err.text()).toContain('一次性触发时刻已过');
    // 失败后表单保持打开，便于用户改时刻重试
    expect(wrapper.find('[data-test="automation-form"]').exists()).toBe(true);
  });
});

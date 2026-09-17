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
    expect(wrapper.text()).toContain('仅星瑶运行期间生效');
    expect(wrapper.text()).toContain('还没有星轨自动化任务');
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
});

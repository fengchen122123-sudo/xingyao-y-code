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
  target_session: '自动化任务',
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

describe('Subpage - 自动化任务', () => {
  beforeEach(() => {
    setActivePinia(createPinia());
  });

  afterEach(() => {
    delete (window as any).pywebview;
  });

  it('渲染标题、说明条与空态', () => {
    const wrapper = mountSubpage();
    expect(wrapper.text()).toContain('自动化任务');
    expect(wrapper.text()).toContain('仅星瑶运行期间生效');
    expect(wrapper.text()).toContain('还没有自动化任务');
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
});

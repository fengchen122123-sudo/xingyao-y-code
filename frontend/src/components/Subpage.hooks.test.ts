import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushPromises, mount } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';

import Subpage from './Subpage.vue';
import { useAppStore } from '../stores/appStore';
import type { HookInfo } from '../types/pywebview';

// C12/C13：Hook 管理页测试数据工厂（默认项目级待确认，按需覆盖）
function makeHook(overrides: Partial<HookInfo> = {}): HookInfo {
  return {
    event: 'PreToolUse',
    matcher: 'Bash',
    command: 'echo hi',
    timeout_ms: 5000,
    status_message: '',
    source_path: 'E:\\proj\\.ycode\\hooks.json',
    scope: 'project',
    digest: 'd-1',
    status: 'pending',
    disabled: false,
    ...overrides,
  };
}

function mountSubpage(visible = true, api: Record<string, unknown> = {}) {
  (window as any).pywebview = { api };
  return mount(Subpage, {
    props: {
      visible,
      subType: 'hooks' as const,
    },
  });
}

describe('Subpage - 钩子 Hooks 管理页', () => {
  beforeEach(() => {
    setActivePinia(createPinia());
  });

  afterEach(() => {
    delete (window as any).pywebview;
  });

  it('无 Hook 时展示空态文案与新建入口', () => {
    const wrapper = mountSubpage();
    expect(wrapper.find('[data-test="hook-row"]').exists()).toBe(false);
    expect(wrapper.text()).toContain('尚未配置钩子');
    expect(wrapper.text()).toContain('.ycode/hooks.json');
    expect(wrapper.find('[data-test="hook-empty-new-btn"]').exists()).toBe(true);
  });

  it('渲染全部 Hook：状态文案、来源徽标与命令文本', () => {
    const store = useAppStore();
    store.hooks = [
      makeHook({
        digest: 'd-user-trusted',
        event: 'UserPromptSubmit',
        matcher: '',
        command: '~/.ycode/bin/notify.sh',
        source_path: 'C:\\Users\\me\\.ycode\\hooks.json',
        scope: 'user',
        status: 'trusted',
      }),
      makeHook({
        digest: 'd-proj-pending',
        event: 'PreToolUse',
        command: 'python ./scripts/guard.py',
        status: 'pending',
      }),
      makeHook({
        digest: 'd-proj-revoked',
        event: 'PostToolUse',
        command: 'npm run fmt',
        status: 'revoked',
      }),
    ];
    const wrapper = mountSubpage();

    const rows = wrapper.findAll('[data-test="hook-row"]');
    expect(rows).toHaveLength(3);
    // 状态文案按 status 派生
    expect(wrapper.text()).toContain('已信任');
    expect(wrapper.text()).toContain('待确认');
    expect(wrapper.text()).toContain('已撤销');
    // 命令文本与来源徽标
    expect(wrapper.text()).toContain('python ./scripts/guard.py');
    expect(wrapper.text()).toContain('npm run fmt');
    expect(rows[0].text()).toContain('用户');
    expect(rows[1].text()).toContain('项目');
    expect(rows[0].text()).toContain('matcher:  · 来源: C:\\Users\\me\\.ycode\\hooks.json');
  });

  it('项目级待确认行可信任 / 拒绝，动作携带该行 digest', async () => {
    const store = useAppStore();
    store.hooks = [
      makeHook({ digest: 'd-proj-pending', status: 'pending' }),
      makeHook({ digest: 'd-proj-stale', event: 'Stop', status: 'stale' }),
    ];
    const grantSpy = vi.fn().mockResolvedValue(undefined);
    const denySpy = vi.fn().mockResolvedValue(undefined);
    store.grantHookTrust = grantSpy as any;
    store.denyHookTrust = denySpy as any;

    const wrapper = mountSubpage();
    const rows = wrapper.findAll('[data-test="hook-row"]');
    expect(rows).toHaveLength(2);

    await rows[0].get('[data-test="hook-action-grant"]').trigger('click');
    expect(grantSpy).toHaveBeenCalledWith('d-proj-pending');

    await rows[0].get('[data-test="hook-action-deny"]').trigger('click');
    expect(denySpy).toHaveBeenCalledWith('d-proj-pending');

    // stale（配置已变更）同样需要重新决策
    expect(rows[1].find('[data-test="hook-action-grant"]').exists()).toBe(true);
    await rows[1].get('[data-test="hook-action-deny"]').trigger('click');
    expect(denySpy).toHaveBeenCalledWith('d-proj-stale');
  });

  it('已信任行可撤销、已拒绝行可重置', async () => {
    const store = useAppStore();
    store.hooks = [
      makeHook({ digest: 'd-proj-trusted', status: 'trusted' }),
      makeHook({ digest: 'd-proj-blocked', event: 'Stop', status: 'blocked' }),
    ];
    const revokeSpy = vi.fn().mockResolvedValue(undefined);
    const resetSpy = vi.fn().mockResolvedValue(undefined);
    store.revokeHookTrust = revokeSpy as any;
    store.resetHookTrust = resetSpy as any;

    const wrapper = mountSubpage();
    const rows = wrapper.findAll('[data-test="hook-row"]');

    // 已信任行不再展示信任/拒绝，只提供撤销入口
    expect(rows[0].find('[data-test="hook-action-grant"]').exists()).toBe(false);
    await rows[0].get('[data-test="hook-action-revoke"]').trigger('click');
    expect(revokeSpy).toHaveBeenCalledWith('d-proj-trusted');

    await rows[1].get('[data-test="hook-action-reset"]').trigger('click');
    expect(resetSpy).toHaveBeenCalledWith('d-proj-blocked');
  });

  it('打开页面（visible 翻转为 true）时刷新 Hook 列表', async () => {
    const store = useAppStore();
    const refreshSpy = vi.fn().mockResolvedValue(undefined);
    store.refreshHooks = refreshSpy as any;

    const wrapper = mountSubpage(false);
    expect(refreshSpy).not.toHaveBeenCalled();

    await wrapper.setProps({ visible: true });
    await flushPromises();
    expect(refreshSpy).toHaveBeenCalledTimes(1);
  });

  it('点击刷新按钮调用 store.refreshHooks', async () => {
    const store = useAppStore();
    const refreshSpy = vi.fn().mockResolvedValue(undefined);
    store.refreshHooks = refreshSpy as any;

    const wrapper = mountSubpage();
    await wrapper.get('[data-test="refresh-hooks-btn"]').trigger('click');
    expect(refreshSpy).toHaveBeenCalledTimes(1);
  });

  // ---------------- C13：表单视图（新建/编辑/删除/启停）与列表筛选 ----------------

  it('新建钩子：填表保存携带 scope 且不含定位字段，成功后回到列表', async () => {
    const store = useAppStore();
    store.hooks = [];
    const saveSpy = vi.fn().mockResolvedValue({ ok: true });
    store.saveHook = saveSpy as any;

    const wrapper = mountSubpage();
    expect(wrapper.find('[data-test="hook-form-save"]').exists()).toBe(false);

    await wrapper.get('[data-test="hook-new-btn"]').trigger('click');
    expect(wrapper.find('[data-test="hook-form-save"]').exists()).toBe(true);

    await wrapper.get('[data-test="hook-form-command"]').setValue('python ./scripts/guard.py');
    await wrapper.get('[data-test="hook-form-save"]').trigger('click');
    await flushPromises();

    expect(saveSpy).toHaveBeenCalledTimes(1);
    const payload = saveSpy.mock.calls[0][0] as Record<string, any>;
    expect(payload).toEqual(
      expect.objectContaining({
        event: 'PreToolUse',
        matcher: '*',
        command: 'python ./scripts/guard.py',
        timeout_ms: 60000,
        scope: 'user',
      })
    );
    // 新建不带编辑定位字段（由后端按 scope 决定写入用户级/项目级文件）
    expect(payload.source_path).toBeUndefined();
    expect(payload.digest).toBeUndefined();
    // 保存成功后表单关闭
    expect(wrapper.find('[data-test="hook-form-save"]').exists()).toBe(false);
  });

  it('点击行进入编辑：表单预填且保存携带 source_path/digest', async () => {
    const store = useAppStore();
    const hook = makeHook({
      digest: 'd-proj-edit',
      command: 'python ./scripts/guard.py',
      status: 'trusted',
    });
    store.hooks = [hook];
    const saveSpy = vi.fn().mockResolvedValue({ ok: true });
    store.saveHook = saveSpy as any;

    const wrapper = mountSubpage();
    await wrapper.get('[data-test="hook-row"]').trigger('click');

    const cmdInput = wrapper.get('[data-test="hook-form-command"]');
    expect((cmdInput.element as HTMLInputElement).value).toBe('python ./scripts/guard.py');
    // 编辑态作用域不迁移：只读展示，不提供下拉选择
    expect(wrapper.find('select[data-test="hook-form-scope"]').exists()).toBe(false);
    expect(wrapper.get('[data-test="hook-form-scope"]').text()).toContain('项目级');

    await cmdInput.setValue('python ./scripts/guard2.py');
    await wrapper.get('[data-test="hook-form-save"]').trigger('click');
    await flushPromises();

    const payload = saveSpy.mock.calls[0][0] as Record<string, any>;
    expect(payload.command).toBe('python ./scripts/guard2.py');
    expect(payload.source_path).toBe(hook.source_path);
    expect(payload.digest).toBe('d-proj-edit');
    expect(payload.scope).toBeUndefined();
  });

  it('编辑态删除：二次确认通过后按 (source_path, digest) 调用 store.deleteHook', async () => {
    const store = useAppStore();
    const hook = makeHook({ digest: 'd-proj-del', command: 'npm run fmt', status: 'trusted' });
    store.hooks = [hook];
    const confirmSpy = vi.fn().mockResolvedValue(true);
    const deleteSpy = vi.fn().mockResolvedValue({ ok: true });
    store.askConfirm = confirmSpy as any;
    store.deleteHook = deleteSpy as any;

    const wrapper = mountSubpage();
    await wrapper.get('[data-test="hook-row"]').trigger('click');
    await wrapper.get('[data-test="hook-form-delete"]').trigger('click');
    await flushPromises();

    expect(confirmSpy).toHaveBeenCalledTimes(1);
    expect(confirmSpy.mock.calls[0][0]).toContain('删除钩子');
    expect(deleteSpy).toHaveBeenCalledWith(hook.source_path, 'd-proj-del');
    // 删除成功后回到列表
    expect(wrapper.find('[data-test="hook-form-save"]').exists()).toBe(false);
  });

  it('二次确认被取消时不调用 store.deleteHook，表单保持编辑态', async () => {
    const store = useAppStore();
    const hook = makeHook({ digest: 'd-proj-cancel', status: 'trusted' });
    store.hooks = [hook];
    store.askConfirm = vi.fn().mockResolvedValue(false) as any;
    const deleteSpy = vi.fn().mockResolvedValue({ ok: true });
    store.deleteHook = deleteSpy as any;

    const wrapper = mountSubpage();
    await wrapper.get('[data-test="hook-row"]').trigger('click');
    await wrapper.get('[data-test="hook-form-delete"]').trigger('click');
    await flushPromises();

    expect(deleteSpy).not.toHaveBeenCalled();
    expect(wrapper.find('[data-test="hook-form-save"]').exists()).toBe(true);
  });

  it('已信任行点击启停开关调用 store.setHookDisabled(source_path, digest, true)', async () => {
    const store = useAppStore();
    const hook = makeHook({
      digest: 'd-user-trusted',
      command: '~/.ycode/bin/notify.sh',
      source_path: 'C:\\Users\\me\\.ycode\\hooks.json',
      scope: 'user',
      status: 'trusted',
      disabled: false,
    });
    store.hooks = [hook];
    const toggleSpy = vi.fn().mockResolvedValue({ ok: true });
    store.setHookDisabled = toggleSpy as any;

    const wrapper = mountSubpage();
    const sw = wrapper.get('[data-test="hook-toggle-switch"]');
    expect((sw.element as HTMLInputElement).checked).toBe(true);
    expect((sw.element as HTMLInputElement).disabled).toBe(false);

    // 取消勾选 = 禁用：checked 取反得到 disabled 语义
    await sw.setValue(false);

    expect(toggleSpy).toHaveBeenCalledTimes(1);
    expect(toggleSpy).toHaveBeenCalledWith(hook.source_path, 'd-user-trusted', true);
  });

  it('未信任行的启停开关为禁用态', () => {
    const store = useAppStore();
    store.hooks = [makeHook({ digest: 'd-proj-pending', status: 'pending', disabled: false })];

    const wrapper = mountSubpage();
    const sw = wrapper.get('[data-test="hook-toggle-switch"]');
    expect((sw.element as HTMLInputElement).disabled).toBe(true);
  });

  it('搜索框按关键词过滤行，无命中时展示筛选空态', async () => {
    const store = useAppStore();
    store.hooks = [
      makeHook({ digest: 'd-search-guard', command: 'python ./scripts/guard.py', status: 'trusted' }),
      makeHook({ digest: 'd-search-fmt', event: 'Stop', command: 'npm run fmt', status: 'trusted' }),
    ];
    const wrapper = mountSubpage();
    expect(wrapper.findAll('[data-test="hook-row"]')).toHaveLength(2);

    await wrapper.get('[data-test="hook-search-input"]').setValue('guard');
    const rows = wrapper.findAll('[data-test="hook-row"]');
    expect(rows).toHaveLength(1);
    expect(rows[0].text()).toContain('python ./scripts/guard.py');

    await wrapper.get('[data-test="hook-search-input"]').setValue('no-such-hook');
    expect(wrapper.findAll('[data-test="hook-row"]')).toHaveLength(0);
    expect(wrapper.text()).toContain('没有符合筛选条件的钩子');
  });

  it('作用域筛选：点击「用户」只显示用户级钩子', async () => {
    const store = useAppStore();
    store.hooks = [
      makeHook({
        digest: 'd-scope-user',
        scope: 'user',
        command: '~/.ycode/bin/notify.sh',
        source_path: 'C:\\Users\\me\\.ycode\\hooks.json',
        status: 'trusted',
      }),
      makeHook({ digest: 'd-scope-proj', scope: 'project', command: 'npm run fmt', status: 'trusted' }),
    ];
    const wrapper = mountSubpage();
    expect(wrapper.findAll('[data-test="hook-row"]')).toHaveLength(2);

    await wrapper.get('[data-test="hook-scope-filter-user"]').trigger('click');
    let rows = wrapper.findAll('[data-test="hook-row"]');
    expect(rows).toHaveLength(1);
    expect(rows[0].text()).toContain('~/.ycode/bin/notify.sh');

    await wrapper.get('[data-test="hook-scope-filter-project"]').trigger('click');
    rows = wrapper.findAll('[data-test="hook-row"]');
    expect(rows).toHaveLength(1);
    expect(rows[0].text()).toContain('npm run fmt');

    await wrapper.get('[data-test="hook-scope-filter-all"]').trigger('click');
    expect(wrapper.findAll('[data-test="hook-row"]')).toHaveLength(2);
  });

  it('命令为空时保存被拦截：提示校验且不调用 store.saveHook', async () => {
    const store = useAppStore();
    store.hooks = [];
    const saveSpy = vi.fn().mockResolvedValue({ ok: true });
    const notifySpy = vi.fn();
    store.saveHook = saveSpy as any;
    store.notifyModal = notifySpy as any;

    const wrapper = mountSubpage();
    await wrapper.get('[data-test="hook-new-btn"]').trigger('click');
    await wrapper.get('[data-test="hook-form-save"]').trigger('click');
    await flushPromises();

    expect(saveSpy).not.toHaveBeenCalled();
    expect(notifySpy).toHaveBeenCalledTimes(1);
    expect(notifySpy.mock.calls[0][0]).toContain('表单校验');
    // 表单保持在编辑态，便于用户补齐命令
    expect(wrapper.find('[data-test="hook-form-save"]').exists()).toBe(true);
  });
});
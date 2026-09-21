import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushPromises, mount } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';

import Subpage from './Subpage.vue';
import { useAppStore } from '../stores/appStore';
import type { HookInfo } from '../types/pywebview';

// C12：Hook 管理页测试数据工厂（默认项目级待确认，按需覆盖）
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

  it('无 Hook 时展示空态文案', () => {
    const wrapper = mountSubpage();
    expect(wrapper.find('[data-test="hook-row"]').exists()).toBe(false);
    expect(wrapper.text()).toContain('未配置任何 Hook');
    expect(wrapper.text()).toContain('.ycode/hooks.json');
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
});
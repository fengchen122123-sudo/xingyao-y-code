import { beforeEach, describe, expect, it, vi } from 'vitest';
import { flushPromises, mount } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';

import HookTrustCard from './HookTrustCard.vue';
import { useAppStore } from '../stores/appStore';
import type { HookInfo } from '../types/pywebview';

function makeHook(overrides: Partial<HookInfo> = {}): HookInfo {
  return {
    event: 'PreToolUse',
    matcher: '',
    command: 'npm run lint',
    timeout_ms: 60000,
    status_message: '',
    source_path: 'E:\\proj\\.ycode\\hooks.json',
    scope: 'project',
    digest: 'd-1',
    status: 'pending',
    ...overrides,
  };
}

describe('HookTrustCard', () => {
  beforeEach(() => setActivePinia(createPinia()));

  it('无待信任项目级 Hook 时不渲染卡片', () => {
    const wrapper = mount(HookTrustCard, { global: { stubs: { AppIcon: true } } });
    expect(wrapper.find('[data-test="hook-trust-card"]').exists()).toBe(false);
  });

  it('渲染待信任 Hook 的数量、命令与来源', () => {
    const store = useAppStore();
    store.hooks = [
      makeHook({ digest: 'd-1', command: 'npm run lint' }),
      makeHook({ digest: 'd-2', command: 'pytest -q', event: 'PostToolUse' }),
    ];
    const wrapper = mount(HookTrustCard, { global: { stubs: { AppIcon: true } } });

    expect(wrapper.find('[data-test="hook-trust-card"]').exists()).toBe(true);
    const items = wrapper.findAll('[data-test="hook-trust-item"]');
    expect(items).toHaveLength(2);
    expect(items[0].text()).toContain('npm run lint');
    expect(items[1].text()).toContain('pytest -q');
    expect(wrapper.text()).toContain('来源: E:\\proj\\.ycode\\hooks.json');
  });

  it('点击"信任并启用"触发 store.grantAllPendingHookTrusts', async () => {
    const store = useAppStore();
    store.hooks = [makeHook()];
    const grant = vi.spyOn(store, 'grantAllPendingHookTrusts').mockResolvedValue(undefined);
    const wrapper = mount(HookTrustCard, { global: { stubs: { AppIcon: true } } });

    await wrapper.get('[data-test="hook-trust-grant-all"]').trigger('click');
    await flushPromises();

    expect(grant).toHaveBeenCalledTimes(1);
  });

  it('点击"拒绝"触发 store.denyAllPendingHookTrusts', async () => {
    const store = useAppStore();
    store.hooks = [makeHook()];
    const deny = vi.spyOn(store, 'denyAllPendingHookTrusts').mockResolvedValue(undefined);
    const wrapper = mount(HookTrustCard, { global: { stubs: { AppIcon: true } } });

    await wrapper.get('[data-test="hook-trust-deny-all"]').trigger('click');
    await flushPromises();

    expect(deny).toHaveBeenCalledTimes(1);
  });
});
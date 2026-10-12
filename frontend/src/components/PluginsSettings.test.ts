import { beforeEach, describe, expect, it, vi } from 'vitest';
import { flushPromises, mount } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';

import PluginsSettings from './PluginsSettings.vue';
import { useAppStore } from '../stores/appStore';

const PAGE = {
  title: 'WorkBuddy',
  sections: [
    { type: 'info', items: [{ label: '当前账号', value: '木偶', badge: '使用中', badge_tone: 'ok' }] },
    {
      type: 'list',
      items: [{ title: '木偶', subtitle: 'uid u1', actions: [{ label: '切换', action: 'switch_account', params: { uid: 'u1' } }] }],
    },
    { type: 'actions', items: [{ label: '添加账号', action: 'add_account', style: 'primary' }] },
  ],
};

function mountPage() {
  return mount(PluginsSettings, { global: { stubs: { AppIcon: true } } });
}

describe('PluginsSettings', () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    const store = useAppStore();
    store.$patch({
      plugins: [
        { name: 'workbuddy_connect', status: 'loaded', enabled: true },
        { name: 'demo', status: 'disabled', enabled: false },
      ],
    });
  });

  it('lists plugins and renders the settings page on click', async () => {
    const store = useAppStore();
    vi.spyOn(store, 'loadPluginSettings').mockResolvedValue({ ok: true, page: PAGE });
    const wrapper = mountPage();

    await wrapper.get('[data-test="plugin-item-workbuddy_connect"]').trigger('click');
    await flushPromises();

    expect(wrapper.text()).toContain('当前账号');
    expect(wrapper.text()).toContain('木偶');
    expect(wrapper.find('[data-test="plugin-action-add_account"]').exists()).toBe(true);
  });

  it('runs a row action and renders the refreshed page', async () => {
    const store = useAppStore();
    vi.spyOn(store, 'loadPluginSettings').mockResolvedValue({ ok: true, page: PAGE });
    const runSpy = vi.spyOn(store, 'runPluginSettingsAction').mockResolvedValue({
      ok: true, msg: '已切换', page: { ...PAGE, title: 'WorkBuddy2' }, done: true,
    });
    const wrapper = mountPage();

    await wrapper.get('[data-test="plugin-item-workbuddy_connect"]').trigger('click');
    await flushPromises();
    await wrapper.get('[data-test="row-action-switch_account"]').trigger('click');
    await flushPromises();

    expect(runSpy).toHaveBeenCalledWith('workbuddy_connect', 'switch_account', { uid: 'u1' });
    expect(wrapper.text()).toContain('WorkBuddy2');
  });

  it('shows the enable guide for a disabled plugin', async () => {
    const store = useAppStore();
    vi.spyOn(store, 'loadPluginSettings').mockResolvedValue({ ok: false, msg: '插件未启用', needs: 'enable' });
    const wrapper = mountPage();

    await wrapper.get('[data-test="plugin-item-demo"]').trigger('click');
    await flushPromises();

    expect(wrapper.text()).toContain('插件未启用');
    expect(wrapper.text()).toContain('启用该插件');
  });
});

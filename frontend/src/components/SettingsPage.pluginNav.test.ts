import { beforeEach, describe, expect, it, vi } from 'vitest';
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';

import SettingsPage from './SettingsPage.vue';
import { useAppStore } from '../stores/appStore';

// 插件设置页从区里的「插件」列表提升为左侧导航一级入口：
// 数据源是 store.plugins 里 has_settings === true 的项（后端 get_plugins 已返回该字段），
// 页面主体复用 PluginSettingsPanel（props name / reloadKey），故沿用 PluginsSettings.test.ts 的
// PAGE 常量与 loadPluginSettings mock 写法，store 数据用 $patch 铺。
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

function setup() {
  const pinia = createPinia();
  setActivePinia(pinia);
  const store = useAppStore();
  // onMounted 的 store.refreshPlugins() 在无 pywebview.api 时直接 return，不会覆盖这里的快照
  store.$patch({
    plugins: [
      { name: 'workbuddy_connect', status: 'loaded', enabled: true, has_settings: true },
      { name: 'smart_router', status: 'loaded', enabled: true, has_settings: false },
      { name: 'hunyuan_image', status: 'loaded', enabled: true },
    ],
  });
  const wrapper = mount(SettingsPage, { global: { plugins: [pinia] } });
  return { wrapper, store };
}

/** 点左侧导航里的插件项，等设置页快照落地 */
async function openPluginNav(wrapper: VueWrapper, name: string) {
  await wrapper.get(`[data-test="plugin-nav-${name}"]`).trigger('click');
  await flushPromises();
}

describe('SettingsPage 插件设置页导航入口', () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    delete (window as any).pywebview;
  });

  it('只有声明了设置页的插件才进导航', () => {
    const { wrapper } = setup();

    const nav = wrapper.get('[data-test="plugin-nav-workbuddy_connect"]');
    expect(nav.text()).toContain('workbuddy_connect');
    // has_settings 为 false 或缺省（含禁用插件）都不进导航
    expect(wrapper.find('[data-test="plugin-nav-smart_router"]').exists()).toBe(false);
    expect(wrapper.find('[data-test="plugin-nav-hunyuan_image"]').exists()).toBe(false);
    // 未点击前不渲染任何插件设置页
    expect(wrapper.find('[data-test="plugin-nav-page-workbuddy_connect"]').exists()).toBe(false);
  });

  it('点击导航项切到插件设置页', async () => {
    const { wrapper, store } = setup();
    const loadSpy = vi.spyOn(store, 'loadPluginSettings').mockResolvedValue({ ok: true, page: PAGE });

    await openPluginNav(wrapper, 'workbuddy_connect');

    expect(loadSpy).toHaveBeenCalledWith('workbuddy_connect');
    const page = wrapper.get('[data-test="plugin-nav-page-workbuddy_connect"]');
    expect(page.isVisible()).toBe(true);
    expect(page.text()).toContain('当前账号');
    expect(page.text()).toContain('木偶');
    expect(wrapper.get('[data-test="plugin-nav-workbuddy_connect"]').classes()).toContain('active');
    // 切到插件设置分区后不再渲染插件列表页
    expect(wrapper.find('[data-test="plugins-refresh"]').exists()).toBe(false);
  });

  it('未启用的插件在导航分区里给出启用引导', async () => {
    const { wrapper, store } = setup();
    vi.spyOn(store, 'loadPluginSettings')
      .mockResolvedValue({ ok: false, msg: '插件未启用，启用后即可配置', needs: 'enable' });

    await openPluginNav(wrapper, 'workbuddy_connect');

    const page = wrapper.get('[data-test="plugin-nav-page-workbuddy_connect"]');
    expect(page.text()).toContain('插件未启用');
    expect(page.text()).toContain('启用该插件');
  });

  it('页头刷新按钮自增 reloadKey，再次拉取设置页快照', async () => {
    const { wrapper, store } = setup();
    const loadSpy = vi.spyOn(store, 'loadPluginSettings').mockResolvedValue({ ok: true, page: PAGE });

    await openPluginNav(wrapper, 'workbuddy_connect');
    expect(loadSpy).toHaveBeenCalledTimes(1);

    await wrapper.get('[data-test="plugin-nav-refresh-workbuddy_connect"]').trigger('click');
    await flushPromises();

    expect(loadSpy).toHaveBeenCalledTimes(2);
    expect(loadSpy).toHaveBeenLastCalledWith('workbuddy_connect');
  });
});

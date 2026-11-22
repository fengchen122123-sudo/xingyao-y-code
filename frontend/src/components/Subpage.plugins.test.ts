import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushPromises, mount } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';

import Subpage from './Subpage.vue';
import { useAppStore } from '../stores/appStore';

function mountPluginsPage() {
  return mount(Subpage, { props: { visible: true, subType: 'plugins' } });
}

function rowNames(wrapper: ReturnType<typeof mount>): string[] {
  return wrapper.findAll('.sub-row .row-name').map(n => n.text());
}

const loadedUser = {
  name: 'tokenrhythm-image',
  version: '1.0',
  description: '生图插件',
  source: 'user',
  status: 'loaded',
  enabled: true,
  tools: ['tokenrhythm_generate_image'],
};

const loadedBuiltin = {
  name: 'workbuddy_connect',
  version: '1.0',
  description: 'WorkBuddy 接入',
  source: 'builtin',
  status: 'loaded',
  enabled: true,
  tools: ['workbuddy_status'],
};

const removedBuiltin = {
  name: 'old_builtin',
  version: '1.0',
  description: '已被移除的内置插件',
  source: 'builtin',
  status: 'removed',
  enabled: false,
  tools: [] as string[],
};

describe('Subpage - 插件移除与恢复', () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    (window as any).pywebview = { api: {} };
  });

  afterEach(() => {
    delete (window as any).pywebview;
  });

  it('内置插件行也渲染垃圾桶按钮', () => {
    const store = useAppStore();
    store.plugins = [loadedUser, loadedBuiltin] as any;
    const wrapper = mountPluginsPage();

    expect(wrapper.findAll('.row-trash')).toHaveLength(2);
    const builtinRow = wrapper.findAll('.sub-row')[1];
    expect(builtinRow.find('.row-name').text()).toBe('workbuddy_connect');
    // 内置插件的垃圾桶要说明"不删随包文件、可恢复"，避免用户以为会彻底删除
    expect(builtinRow.get('.row-trash').attributes('title')).toContain('可恢复');
  });

  it('已移除插件默认隐藏，切换后可看到恢复按钮且没有启用开关', async () => {
    const store = useAppStore();
    store.plugins = [loadedUser, removedBuiltin] as any;
    const wrapper = mountPluginsPage();

    expect(rowNames(wrapper)).toEqual(['tokenrhythm-image']);
    expect(wrapper.text()).not.toContain('old_builtin');
    const toggle = wrapper.get('[data-test="plugins-show-removed"]');
    expect(toggle.text()).toContain('已移除 1');

    await toggle.trigger('click');

    expect(rowNames(wrapper)).toEqual(['old_builtin']);
    const row = wrapper.findAll('.sub-row')[0];
    expect(row.find('[data-test="plugin-restore-btn"]').exists()).toBe(true);
    expect(row.find('[data-test="plugin-restore-btn"]').text()).toBe('恢复');
    expect(row.find('.switch').exists()).toBe(false);
    expect(row.find('.row-trash').exists()).toBe(false);
  });

  it('点击恢复按钮调用 restore_plugin 并用返回的 plugins 覆盖列表', async () => {
    const restored = {
      ...removedBuiltin,
      status: 'loaded',
      enabled: true,
      tools: ['old_tool'],
    };
    const restoredList = [loadedUser, restored];
    const restorePlugin = vi.fn(async () => ({
      ok: true,
      msg: '已恢复内置插件 old_builtin',
      plugins: restoredList,
    }));
    (window as any).pywebview = { api: { restore_plugin: restorePlugin } };

    const store = useAppStore();
    store.plugins = [loadedUser, removedBuiltin] as any;
    const wrapper = mountPluginsPage();

    await wrapper.get('[data-test="plugins-show-removed"]').trigger('click');
    await wrapper.get('[data-test="plugin-restore-btn"]').trigger('click');
    await flushPromises();

    expect(restorePlugin).toHaveBeenCalledTimes(1);
    expect(restorePlugin).toHaveBeenCalledWith('old_builtin');
    expect(store.plugins).toEqual(restoredList);
    expect(store.plugins.find(p => p.name === 'old_builtin')!.status).toBe('loaded');
    await wrapper.vm.$nextTick();
    expect(wrapper.find('[data-test="plugins-show-removed"]').exists()).toBe(false);
  });
});
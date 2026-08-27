import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { mount } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';

import SettingsPage from './SettingsPage.vue';
import { useAppStore } from '../stores/appStore';

type Listener = (e: { matches: boolean }) => void;

// 在挂载组件前注入 matchMedia；jsdom 无此 API，必须 defineProperty（原型只读）
function installMatchMedia(matches: boolean) {
  const listeners = new Set<Listener>();
  Object.defineProperty(window, 'matchMedia', {
    configurable: true,
    value: (query: string) => {
      if (query !== '(max-width: 768px)') throw new Error('unexpected query: ' + query);
      return {
        matches,
        addEventListener: (_t: string, cb: Listener) => listeners.add(cb),
        removeEventListener: (_t: string, cb: Listener) => { listeners.delete(cb); },
      };
    },
  });
  return { fireMobileChange: (m: boolean) => { for (const cb of listeners) cb({ matches: m }); } };
}

describe('SettingsPage 手机端钻取布局', () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    const store = useAppStore();
    store.$patch({
      providers: [
        {
          id: 'p1',
          name: '月之暗面 Kimi-for-Coding',
          base_url: 'https://api.example.com',
          protocol: 'openai',
          has_api_key: true,
          models: [
            { id: 'm1', name: 'kimi-k2', provider_id: 'p1', provider_name: 'p1', context_window: 256000, supports_vision: true },
          ],
        },
      ],
    });
  });

  afterEach(() => {
    // 还原为"无 matchMedia"，避免影响其他测试文件的全局状态
    Object.defineProperty(window, 'matchMedia', { configurable: true, value: undefined });
  });

  it('手机端默认显示全宽厂商列表，详情隐藏、无返回按钮', () => {
    installMatchMedia(true);
    const wrapper = mount(SettingsPage);

    expect(wrapper.find('.settings-page--mobile').exists()).toBe(true);
    // v-if 硬互斥：未选中时详情不挂载
    expect(wrapper.find('.provider-list').exists()).toBe(true);
    expect(wrapper.find('.provider-detail').exists()).toBe(false);
  });

  it('点选厂商进入详情视图，返回按钮可回到列表', async () => {
    installMatchMedia(true);
    const wrapper = mount(SettingsPage);

    await wrapper.get('[data-test="provider-p1"]').trigger('click');
    await wrapper.vm.$nextTick();

    expect(wrapper.find('.provider-detail').exists()).toBe(true);
    expect(wrapper.find('.provider-list').exists()).toBe(false);
    expect(wrapper.get('.mobile-detail-back').isVisible()).toBe(true);
    expect(wrapper.text()).toContain('月之暗面 Kimi-for-Coding');

    await wrapper.get('.mobile-detail-back').trigger('click');
    // closeMobileDetail 改的是 ref，DOM 同步在下一帧
    await wrapper.vm.$nextTick();

    expect(wrapper.find('.provider-list').exists()).toBe(true);
    expect(wrapper.find('.provider-detail').exists()).toBe(false);
  });

  it('窗口从手机宽拉大到桌面宽时自动退出钻取态，恢复并排布局', async () => {
    const mql = installMatchMedia(true);
    const wrapper = mount(SettingsPage);

    await wrapper.get('[data-test="provider-p1"]').trigger('click');
    await wrapper.vm.$nextTick();
    expect(wrapper.find('.provider-detail').exists()).toBe(true);

    // watch 默认 pre-flush，DOM 更新异步，先等一帧再断言
    mql.fireMobileChange(false);
    await wrapper.vm.$nextTick();

    expect(wrapper.find('.settings-page--mobile').exists()).toBe(false);
    expect(wrapper.find('.provider-detail').exists()).toBe(true);
  });

  it('桌面环境（无 matchMedia）行为不变：详情常驻、无返回按钮', () => {
    Object.defineProperty(window, 'matchMedia', { configurable: true, value: undefined });
    const wrapper = mount(SettingsPage);

    expect(wrapper.find('.settings-page--mobile').exists()).toBe(false);
    expect(wrapper.find('.provider-list').exists()).toBe(true);
    expect(wrapper.find('.provider-detail').exists()).toBe(true);
    expect(wrapper.find('.mobile-detail-back').exists()).toBe(false);
  });
});

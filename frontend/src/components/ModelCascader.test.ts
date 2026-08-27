import { beforeEach, describe, expect, it } from 'vitest';
import { mount } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';

import ModelCascader from './ModelCascader.vue';
import { useAppStore } from '../stores/appStore';


describe('ModelCascader', () => {
  beforeEach(() => {
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: 1024 });
    Object.defineProperty(window, 'innerHeight', { configurable: true, value: 768 });
    setActivePinia(createPinia());
    useAppStore().$patch({
      models: [
        {
          id: 'm1', name: 'model-one', provider_id: 'p1', provider_name: '厂商一',
          context_window: 128000, supports_vision: false,
        },
        {
          id: 'm2', name: 'vision-two', provider_id: 'p1', provider_name: '厂商一',
          context_window: 200000, supports_vision: true,
        },
      ],
      activeModelId: 'm1',
    });
  });

  it('reveals provider models on hover and opens the unified settings page', async () => {
    const anchor = document.createElement('button');
    anchor.getBoundingClientRect = () => ({
      x: 200, y: 600, top: 600, right: 420, bottom: 640, left: 200,
      width: 220, height: 40, toJSON: () => ({}),
    });
    document.body.appendChild(anchor);
    const wrapper = mount(ModelCascader, { props: { visible: true, anchorEl: anchor }, global: { stubs: { teleport: true } } });

    const provider = wrapper.findAll('.cascader-item').find(item => item.text().includes('厂商一'))!;
    await provider.trigger('mouseenter');
    expect(wrapper.find('.cascader-sub-panel').text()).toContain('model-one');
    expect(wrapper.find('.cascader-sub-panel').text()).toContain('vision-two');

    const manage = wrapper.findAll('.cascader-item').find(item => item.text().includes('管理模型'))!;
    await manage.trigger('click');
    expect(useAppStore().settingsOpen).toBe(true);
  });

  it('aligns the first model row with the hovered provider row', async () => {
    const anchor = document.createElement('button');
    const wrapper = mount(ModelCascader, { props: { visible: true, anchorEl: anchor }, global: { stubs: { teleport: true } } });
    const provider = wrapper.find('.cascader-main-panel .cascader-item');
    provider.element.getBoundingClientRect = () => ({
      x: 100, y: 200, top: 200, right: 340, bottom: 236, left: 100,
      width: 240, height: 36, toJSON: () => ({}),
    });

    await provider.trigger('mouseenter');

    const subPanelStyle = wrapper.get('.cascader-sub-panel').attributes('style');
    expect(subPanelStyle).toContain('top: 193px');
    expect(subPanelStyle).toContain('max-height: 563px');
  });

  it('bottom-aligns both panels when the menu opens upward', async () => {
    const anchor = document.createElement('button');
    anchor.getBoundingClientRect = () => ({
      x: 200, y: 700, top: 700, right: 420, bottom: 740, left: 200,
      width: 220, height: 40, toJSON: () => ({}),
    });

    const wrapper = mount(ModelCascader, { props: { visible: false, anchorEl: anchor }, global: { stubs: { teleport: true } } });
    await wrapper.setProps({ visible: true });

    // teleport 桩后组件根是 stub，样式断言查真实根元素
    const root = wrapper.get('#model-cascader');
    expect(root.classes()).toContain('opens-up');
    expect(root.attributes('style')).toContain('--cascader-max-height: 680px');
  });

  it('模型菜单不再显示通用推理强度行', () => {
    const wrapper = mount(ModelCascader, {
      props: { visible: true, anchorEl: document.createElement('button') },
      global: { stubs: { teleport: true } },
    });

    expect(wrapper.find('.effort-row').exists()).toBe(false);
  });

  it('窄屏下模型方盒在主面板左侧，当前供应商模型内联', async () => {
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: 390 });
    (window as unknown as { matchMedia: (q: string) => { matches: boolean } }).matchMedia =
      (query: string) => ({ matches: query.includes('max-width'), media: query });

    try {
      const store = useAppStore();
      store.$patch({
        models: [
          ...store.models,
          {
            id: 'm3', name: 'very-long-model-name-from-vendor-two', provider_id: 'p2',
            provider_name: '厂商二', context_window: 64000, supports_vision: false,
          },
        ],
      });

      const anchor = document.createElement('button');
      const wrapper = mount(ModelCascader, {
        props: { visible: false, anchorEl: anchor },
        global: { stubs: { teleport: true } },
      });
      await wrapper.setProps({ visible: true });

      // teleport 桩在逐帧重渲染时会重建子元素，断言一律重新实时查询
      const mainPanel = () => wrapper.get('.cascader-main-panel');
      // 390 视口：subW = min(200, 175) = 175，mainW = min(220, 189) = 189，主面板靠右
      expect(mainPanel().attributes('style')).toContain('width: 189px');

      // 当前供应商（厂商一）模型内联在主面板顶部，带供应商标题
      expect(mainPanel().get('.cascader-provider-header').text()).toBe('厂商一');
      expect(mainPanel().text()).toContain('model-one');
      expect(mainPanel().text()).toContain('vision-two');

      // 点其他供应商 → 方盒出现在主面板左侧（left 10 / 宽 175 / 随主面板顶对齐）
      mainPanel().element.getBoundingClientRect = () => ({
        x: 191, y: 80, top: 80, right: 380, bottom: 480, left: 191,
        width: 189, height: 400, toJSON: () => ({}),
      });
      const provider = wrapper.findAll('.cascader-main-panel .cascader-item')
        .find(item => item.text().includes('厂商二'))!;
      await provider.trigger('mouseenter');

      const subPanel = () => wrapper.get('.cascader-sub-panel');
      expect(subPanel().classes()).toContain('narrow-side');
      expect(subPanel().classes()).toContain('show');
      const style = subPanel().attributes('style');
      expect(style).toContain('left: 10px');
      expect(style).toContain('width: 175px');
      expect(style).toContain('top: 80px');
      expect(style).toContain('max-height: 400px');
      // 方盒内是该供应商的模型，且不再有返回行（供应商列表始终可见）
      expect(subPanel().text()).toContain('very-long-model-name-from-vendor-two');
      expect(subPanel().findAll('.cascader-back').length).toBe(0);
    } finally {
      delete (window as unknown as { matchMedia?: unknown }).matchMedia;
    }
  });
});

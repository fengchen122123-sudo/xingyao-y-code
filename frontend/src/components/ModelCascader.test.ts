import { beforeEach, describe, expect, it } from 'vitest';
import { mount } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';

import ModelCascader from './ModelCascader.vue';
import { useAppStore } from '../stores/appStore';


describe('ModelCascader', () => {
  beforeEach(() => {
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
    const wrapper = mount(ModelCascader, { props: { visible: true, anchorEl: anchor } });

    const provider = wrapper.findAll('.cascader-item').find(item => item.text().includes('厂商一'))!;
    await provider.trigger('mouseenter');
    expect(wrapper.find('.cascader-sub-panel').text()).toContain('model-one');
    expect(wrapper.find('.cascader-sub-panel').text()).toContain('vision-two');

    const manage = wrapper.findAll('.cascader-item').find(item => item.text().includes('管理模型'))!;
    await manage.trigger('click');
    expect(useAppStore().settingsOpen).toBe(true);
  });

  it('bottom-aligns both panels when the menu opens upward', async () => {
    const anchor = document.createElement('button');
    anchor.getBoundingClientRect = () => ({
      x: 200, y: 700, top: 700, right: 420, bottom: 740, left: 200,
      width: 220, height: 40, toJSON: () => ({}),
    });

    const wrapper = mount(ModelCascader, { props: { visible: false, anchorEl: anchor } });
    await wrapper.setProps({ visible: true });

    expect(wrapper.classes()).toContain('opens-up');
    expect(wrapper.attributes('style')).toContain('--cascader-max-height: 680px');
  });
});

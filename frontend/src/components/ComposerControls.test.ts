import { beforeEach, describe, expect, it } from 'vitest';
import { mount } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';

import InputArea from './InputArea.vue';
import KimiEmptyState from './KimiEmptyState.vue';
import PermDropdown from './PermDropdown.vue';
import { useAppStore } from '../stores/appStore';


describe('composer controls', () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    useAppStore().permissionMode = 'confirm';
  });

  it.each([
    ['empty composer', KimiEmptyState],
    ['active composer', InputArea],
  ])('renders simplified and colored controls in the %s', async (_name, component) => {
    const store = useAppStore();
    store.reasoning = {
      kind: 'effort', visible: true, disabled: false, disabled_reason: '', choice: 'high',
      options: [{ value: 'high', label: '高' }],
    };
    const wrapper = mount(component);

    // 附件按钮与权限下拉箭头已统一为 AppIcon SVG 图标：断言按钮内渲染了内联 svg
    expect(wrapper.get('[data-test="attachment-button"]').find('svg').exists()).toBe(true);
    expect(wrapper.get('[data-test="permission-button"]').text()).not.toContain('☑');
    expect(wrapper.get('[data-test="permission-button"]').classes()).toContain('perm-confirm');

    store.permissionMode = 'plan';
    await wrapper.vm.$nextTick();

    expect(wrapper.get('[data-test="permission-button"]').classes()).toContain('perm-plan');

    store.permissionMode = 'full';
    await wrapper.vm.$nextTick();

    expect(wrapper.get('[data-test="permission-button"]').text().replace(/\s+/g, '')).toBe('完全访问');

    const model = wrapper.get('[data-test="model-button"]').element;
    const reasoning = wrapper.get('[data-test="reasoning-button"]').element;
    const send = wrapper.get('[data-test="send-button"]').element;
    expect(model.compareDocumentPosition(reasoning) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(reasoning.compareDocumentPosition(send) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    wrapper.unmount();
  });

  it('uses green for confirm mode in the dropdown', () => {
    const wrapper = mount(PermDropdown, {
      props: { visible: true, anchorEl: document.createElement('button') },
      global: { stubs: { teleport: true } },
    });

    expect(wrapper.findAll('.dd-dot')[0].attributes('style')).toContain('var(--green)');
    wrapper.unmount();
  });
});

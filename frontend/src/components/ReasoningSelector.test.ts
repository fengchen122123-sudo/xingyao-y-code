import { mount } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import ReasoningSelector from './ReasoningSelector.vue';
import { useAppStore } from '../stores/appStore';


describe('ReasoningSelector', () => {
  beforeEach(() => {
    setActivePinia(createPinia());
  });

  it('固定思考模型显示“开启”并禁用下拉', () => {
    const store = useAppStore();
    store.reasoning = {
      kind: 'fixed', visible: true, disabled: true,
      disabled_reason: '该模型始终开启思考', choice: 'on', options: [],
    };

    const wrapper = mount(ReasoningSelector);
    const button = wrapper.get('[data-test="reasoning-button"]');

    expect(button.text()).toContain('开启');
    expect(button.attributes('disabled')).toBeDefined();
    expect(button.attributes('title')).toBe('该模型始终开启思考');
  });

  it('只显示当前模型支持的选项并可选择', async () => {
    const store = useAppStore();
    store.reasoning = {
      kind: 'effort', visible: true, disabled: false, disabled_reason: '', choice: 'high',
      options: [
        { value: 'default', label: '默认' },
        { value: 'off', label: '关闭' },
        { value: 'high', label: '高' },
        { value: 'max', label: '最高' },
      ],
    };
    const setChoice = vi.spyOn(store, 'setReasoningChoice').mockResolvedValue(undefined);
    const wrapper = mount(ReasoningSelector, { global: { stubs: { teleport: true } } });

    await wrapper.get('[data-test="reasoning-button"]').trigger('click');
    const options = wrapper.findAll('[data-test="reasoning-option"]');

    expect(options.map(node => node.text())).toEqual(['默认', '关闭', '高', '最高']);
    await options[3].trigger('click');
    expect(setChoice).toHaveBeenCalledWith('max');
  });

  it('未知或未确认能力的模型不显示选择器', () => {
    const store = useAppStore();
    store.reasoning = {
      kind: 'hidden', visible: false, disabled: false,
      disabled_reason: '', choice: 'default', options: [],
    };

    expect(mount(ReasoningSelector).find('[data-test="reasoning-button"]').exists()).toBe(false);
  });
});

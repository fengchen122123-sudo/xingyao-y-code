import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, expect, it, vi } from 'vitest';
import { mount, type VueWrapper } from '@vue/test-utils';

import SettingsPage from './SettingsPage.vue';
import { useAppStore } from '../stores/appStore';
import type { ProviderInfo } from '../types/pywebview';

const stubs = {
  AppIcon: true,
  TokenUsageSettings: true,
  TokenUsageSettingsLegacy: true,
  GuiThemeSettings: true,
  SystemSettings: true,
  PersonaSettings: true,
  FeishuSettings: true,
  CloudRelaySettings: true,
  LanPeersSettings: true,
  PluginsSettings: true,
  PluginSettingsPanel: true,
  HooksSettings: true,
};

beforeEach(() => {
  localStorage.clear();
  setActivePinia(createPinia());
  delete (window as any).pywebview;
});

async function openCustomForm(wrapper: VueWrapper) {
  await wrapper.find('[data-test="add-provider-toggle"]').trigger('click');
  await wrapper.find('[data-test="add-custom-provider"]').trigger('click');
}

function fieldValue(wrapper: VueWrapper, testId: string) {
  return (wrapper.find(`[data-test="${testId}"]`).element as HTMLInputElement | HTMLTextAreaElement).value;
}

function submitCustomForm(wrapper: VueWrapper) {
  const submit = wrapper.find('button.primary-button');
  expect(submit.exists(), '「添加厂商」提交按钮应存在').toBeTruthy();
  return submit.trigger('click');
}

it('保存自定义厂商后再次打开表单，不残留上一次填写的名称 / Base URL / Key', async () => {
  const store = useAppStore();
  store.providers = [{
    id: 'p1', name: '书生·端砚', base_url: 'https://discovery-api.internal-ai.org.cn/v1',
    protocol: 'openai', preset_id: '', has_api_key: true,
  } as ProviderInfo];
  vi.spyOn(store, 'addProvider').mockResolvedValue({ ok: true, provider_id: 'p1' } as any);

  const wrapper = mount(SettingsPage, { global: { stubs } });
  await openCustomForm(wrapper);
  await wrapper.find('[data-test="custom-name"]').setValue('书生·端砚');
  await wrapper.find('[data-test="custom-base-url"]').setValue('https://discovery-api.internal-ai.org.cn/v1');
  await wrapper.find('[data-test="custom-api-key"]').setValue('sk-history-leak');
  await submitCustomForm(wrapper);
  await wrapper.vm.$nextTick();

  await openCustomForm(wrapper);

  expect(fieldValue(wrapper, 'custom-name')).toBe('');
  expect(fieldValue(wrapper, 'custom-base-url')).toBe('');
  expect(fieldValue(wrapper, 'custom-api-key')).toBe('');
  wrapper.unmount();
});

it('填写后切到其它视图再回来，表单同样为空', async () => {
  const store = useAppStore();
  store.providers = [{ id: 'p1', name: '已有厂商', protocol: 'openai' } as ProviderInfo];

  const wrapper = mount(SettingsPage, { global: { stubs } });
  await openCustomForm(wrapper);
  await wrapper.find('[data-test="custom-name"]').setValue('半途填写的网关');
  await wrapper.find('[data-test="custom-api-key"]').setValue('sk-halfway');

  await wrapper.find('[data-test="secondary-entry"]').trigger('click');
  await openCustomForm(wrapper);

  expect(fieldValue(wrapper, 'custom-name')).toBe('');
  expect(fieldValue(wrapper, 'custom-api-key')).toBe('');
  wrapper.unmount();
});

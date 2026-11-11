import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { mount } from '@vue/test-utils';
import { nextTick } from 'vue';
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

// ── 屏幕截图按钮：点击调 window.pywebview.api.screen_snip_start（宿主全屏框选），
//    桥缺失/失败时经 store.notifyChat 提示；截图结果由 snip_attachment_ready 回传 ──

describe('screen snip button', () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    useAppStore().permissionMode = 'confirm';
    // 每个用例前清掉宿主桥残留，避免注入的 mock 污染其他用例
    delete (window as any).pywebview;
  });

  afterEach(() => {
    delete (window as any).pywebview;
  });

  it.each([
    ['empty composer', KimiEmptyState],
    ['active composer', InputArea],
  ])('renders the screen-snip button with a crop icon in the %s', (_name, component) => {
    const wrapper = mount(component);
    // 与附件/权限按钮同口径：按钮内渲染 AppIcon 内联 svg
    const btn = wrapper.get('[data-test="screen-snip-button"]');
    expect(btn.find('svg').exists()).toBe(true);
    wrapper.unmount();
  });

  it.each([
    ['empty composer', KimiEmptyState],
    ['active composer', InputArea],
  ])('clicking triggers pywebview.api.screen_snip_start once in the %s', async (_name, component) => {
    const screenSnipStart = vi.fn(async () => ({ ok: true }));
    (window as any).pywebview = { api: { screen_snip_start: screenSnipStart } };
    const wrapper = mount(component);

    await wrapper.get('[data-test="screen-snip-button"]').trigger('click');
    await nextTick();

    expect(screenSnipStart).toHaveBeenCalledTimes(1);
    wrapper.unmount();
  });

  it.each([
    ['empty composer', KimiEmptyState],
    ['active composer', InputArea],
  ])('notifies the bridge msg when screen_snip_start returns ok:false in the %s', async (_name, component) => {
    const store = useAppStore();
    const notify = vi.spyOn(store, 'notifyChat').mockImplementation(() => {});
    (window as any).pywebview = {
      api: { screen_snip_start: vi.fn(async () => ({ ok: false, msg: '截图启动失败' })) },
    };
    const wrapper = mount(component);

    await wrapper.get('[data-test="screen-snip-button"]').trigger('click');
    await vi.waitFor(() => expect(notify).toHaveBeenCalledWith('截图启动失败'));
    wrapper.unmount();
  });

  it.each([
    ['empty composer', KimiEmptyState],
    ['active composer', InputArea],
  ])('reports missing desktop bridge without throwing in the %s', async (_name, component) => {
    delete (window as any).pywebview;
    const store = useAppStore();
    const notify = vi.spyOn(store, 'notifyChat').mockImplementation(() => {});
    const wrapper = mount(component);

    // 桥缺失时点击不得抛错（trigger 会同步执行点击处理器），只提示不可用
    await wrapper.get('[data-test="screen-snip-button"]').trigger('click');
    await nextTick();

    expect(notify).toHaveBeenCalledTimes(1);
    expect(notify).toHaveBeenCalledWith('桌面桥未就绪，无法使用截图');
    wrapper.unmount();
  });
});

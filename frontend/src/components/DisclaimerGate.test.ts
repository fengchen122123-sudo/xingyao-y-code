import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushPromises, mount } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';

import DisclaimerGate from './DisclaimerGate.vue';
import { useAppStore } from '../stores/appStore';
import { DISCLAIMER_CLAUSES, DISCLAIMER_VERSION } from '../data/disclaimer';

function mountGate(api: Record<string, unknown> = {}) {
  (window as any).pywebview = { api };
  return mount(DisclaimerGate);
}

describe('DisclaimerGate', () => {
  beforeEach(() => {
    setActivePinia(createPinia());
  });

  afterEach(() => {
    delete (window as any).pywebview;
  });

  it('renders every clause and highlights the key liability clauses', () => {
    const wrapper = mountGate();

    expect(wrapper.findAll('.gate-clause').length).toBe(DISCLAIMER_CLAUSES.length);

    // 《民法典》第 496 条要求对「免除或减轻自身责任」等重大利害条款作显著提示：
    // 「二、AI 生成内容」与「七、责任限制」必须带标记并通过样式强调
    expect(wrapper.findAll('[data-test="disclaimer-key-tag"]').length).toBe(2);
    const keyText = wrapper.findAll('.gate-clause--key').map((n) => n.text()).join('');
    expect(keyText).toContain('责任限制');
    expect(keyText).toContain('法律允许的最大范围内');
  });

  it('does not record consent without an explicit click', () => {
    const accept = vi.fn().mockResolvedValue({ ok: true });
    mountGate({ accept_disclaimer: accept });

    expect(accept).not.toHaveBeenCalled();
  });

  it('records consent through the backend and flips the gate state', async () => {
    const store = useAppStore();
    store.disclaimerRequiredVersion = DISCLAIMER_VERSION;
    store.disclaimerAcceptedVersion = '';
    expect(store.disclaimerNeedsAccept).toBe(true);

    const accept = vi.fn().mockResolvedValue({
      ok: true,
      disclaimer_version: DISCLAIMER_VERSION,
      accepted_version: DISCLAIMER_VERSION,
    });
    const wrapper = mountGate({ accept_disclaimer: accept });

    await wrapper.get('[data-test="disclaimer-accept"]').trigger('click');
    await flushPromises();

    expect(accept).toHaveBeenCalledTimes(1);
    // 同意后状态就地更新，父级 v-if 失效，弹窗随之卸载
    expect(store.disclaimerAcceptedVersion).toBe(DISCLAIMER_VERSION);
    expect(store.disclaimerNeedsAccept).toBe(false);
  });

  it('keeps the gate open and surfaces the reason when saving fails', async () => {
    const accept = vi.fn().mockResolvedValue({ ok: false, msg: '配置目录只读' });
    const wrapper = mountGate({ accept_disclaimer: accept });

    await wrapper.get('[data-test="disclaimer-accept"]').trigger('click');
    await flushPromises();

    expect(wrapper.get('[data-test="disclaimer-error"]').text()).toContain('配置目录只读');
  });

  it('exits the software when the user declines', async () => {
    const confirmClose = vi.fn().mockResolvedValue({ ok: true });
    const wrapper = mountGate({ window_confirm_close: confirmClose });

    await wrapper.get('[data-test="disclaimer-decline"]').trigger('click');
    await flushPromises();

    expect(confirmClose).toHaveBeenCalledWith('exit');
  });
});

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { mount } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';

import TokenUsagePopover from './TokenUsagePopover.vue';
import { normalizeTokenUsage, useAppStore } from '../stores/appStore';

function setUsage(usage: Record<string, unknown>) {
  const store = useAppStore();
  store.tokenUsage = { ...normalizeTokenUsage({}), ...usage };
}

function makeAnchor(): HTMLElement {
  const anchor = document.createElement('span');
  anchor.getBoundingClientRect = () => ({
    x: 24, y: 700, top: 700, right: 320, bottom: 718, left: 24,
    width: 296, height: 18, toJSON: () => ({}),
  } as DOMRect);
  return anchor;
}

function mountPopover(options: { visible?: boolean } = {}) {
  const anchorEl = makeAnchor();
  const wrapper = mount(TokenUsagePopover, {
    props: { visible: options.visible ?? true, anchorEl },
    global: { stubs: { teleport: true } },
  });
  return { wrapper, anchorEl };
}

describe('TokenUsagePopover', () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    // 打开面板会拉取概览；单测环境无 pywebview API，stub 掉避免弹连接失败框
    const store = useAppStore();
    vi.spyOn(store, 'loadTokenUsageOverview').mockResolvedValue(null);
  });

  it('展示本会话指标与命中率（分母为 cache_prompt）', () => {
    setUsage({
      session_totals: normalizeTokenUsage({
        session_totals: {
          session_id: '你来对我1.3.3版本新',
          prompt: 12480, completion: 3206, cached: 9876,
          cache_prompt: 12000, total: 15686, requests: 23,
          last_ts: Date.now() / 1000 - 120,
          keepalive_prompt: 0, keepalive_cached: 0, keepalive_requests: 0,
        },
      }).session_totals,
    });
    const { wrapper } = mountPopover();

    expect(wrapper.get('[data-test="tup-session-metrics"]').text()).toContain('12,480');
    expect(wrapper.get('[data-test="tup-session-metrics"]').text()).toContain('9,876');
    // 命中率 = 9876 / 12000 = 82%（不是 9876/12480=79%）
    expect(wrapper.get('[data-test="tup-session-metrics"]').text()).toContain('82%');
    wrapper.unmount();
  });

  it('无缓存样本时省略命中率，不显示 NaN', () => {
    setUsage({});
    const { wrapper } = mountPopover();

    const metrics = wrapper.get('[data-test="tup-session-metrics"]').text();
    // jsdom 会折叠模板空白：标签与数值间由 CSS margin 撑开视觉间距
    expect(metrics).toContain('命中0');
    expect(metrics).not.toContain('%');
    wrapper.unmount();
  });

  it('有保活轮次时单列保活烧损，无保活时不显示', () => {
    setUsage({
      session_totals: normalizeTokenUsage({
        session_totals: {
          session_id: 's', prompt: 10, completion: 1, cached: 0,
          cache_prompt: 10, total: 11, requests: 1, last_ts: null,
          keepalive_prompt: 8120, keepalive_cached: 0, keepalive_requests: 4,
        },
      }).session_totals,
    });
    const { wrapper } = mountPopover();
    expect(wrapper.get('[data-test="tup-keepalive"]').text()).toContain('8,120');
    wrapper.unmount();

    setUsage({});
    const { wrapper: wrapper2 } = mountPopover();
    expect(wrapper2.find('[data-test="tup-keepalive"]').exists()).toBe(false);
    wrapper2.unmount();
  });

  it('会话列表按接口返回渲染并高亮当前会话', async () => {
    const store = useAppStore();
    store.tokenUsageOverview = {
      ok: true,
      current: '会话A',
      totals: { prompt: 1000, completion: 100, cached: 800, cache_prompt: 1000, total: 1100, requests: 9 },
      sessions: [
        { session_id: '会话A', prompt: 600, completion: 60, cached: 500, cache_prompt: 600, total: 660, requests: 5, last_ts: null },
        { session_id: '会话B', prompt: 400, completion: 40, cached: 0, cache_prompt: 0, total: 440, requests: 4, last_ts: null },
      ],
    };
    const { wrapper } = mountPopover();

    const rows = wrapper.findAll('[data-test="tup-session-list"] .tup-row');
    expect(rows).toHaveLength(2);
    expect(rows[0].classes()).toContain('current');
    expect(rows[0].text()).toContain('会话A');
    // 会话B 无缓存样本 → 命中率显示 --，不冒充 0%
    expect(rows[1].text()).toContain('--');
    wrapper.unmount();
  });

  it('visible=false 时不渲染面板，打开时触发概览拉取', async () => {
    const store = useAppStore();
    const { wrapper } = mountPopover({ visible: false });
    expect(wrapper.find('[data-test="token-usage-popover"]').exists()).toBe(false);

    await wrapper.setProps({ visible: true });
    expect(wrapper.find('[data-test="token-usage-popover"]').exists()).toBe(true);
    expect(store.loadTokenUsageOverview).toHaveBeenCalled();
    wrapper.unmount();
  });
});

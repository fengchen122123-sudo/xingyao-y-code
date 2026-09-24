import { beforeEach, describe, expect, it } from 'vitest';
import { mount } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';

import ContextUsagePanel from './ContextUsagePanel.vue';
import { normalizeTokenUsage, useAppStore } from '../stores/appStore';
import type { ContextBreakdownItem, TokenUsage } from '../types/pywebview';

// 直接覆写 store.tokenUsage（绕过逐字段归一化），以覆盖面板自身的容错路径
function setUsage(usage: Partial<TokenUsage>) {
  useAppStore().tokenUsage = { ...normalizeTokenUsage({}), ...usage };
}

function makeAnchor(rect: Partial<DOMRect> = {}): HTMLElement {
  const anchor = document.createElement('button');
  anchor.getBoundingClientRect = () => ({
    x: 200, y: 700, top: 700, right: 220, bottom: 718, left: 200,
    width: 20, height: 18, toJSON: () => ({}), ...rect,
  } as DOMRect);
  return anchor;
}

function mountPanel(options: { visible?: boolean; anchorEl?: HTMLElement | null } = {}) {
  return mount(ContextUsagePanel, {
    props: {
      visible: options.visible !== false,
      anchorEl: options.anchorEl === undefined ? makeAnchor() : options.anchorEl,
    },
    global: { stubs: { teleport: true } },
  });
}

describe('ContextUsagePanel', () => {
  beforeEach(() => {
    setActivePinia(createPinia());
  });

  it('仅 visible 为 true 时展示面板', async () => {
    const wrapper = mountPanel({ visible: false });
    expect(wrapper.get('.ctx-usage-panel').classes()).not.toContain('show');

    await wrapper.setProps({ visible: true });
    expect(wrapper.get('.ctx-usage-panel').classes()).toContain('show');
    wrapper.unmount();
  });

  it('标题行显示中文标题与等宽摘要（千分位 + 1 位小数百分比）', () => {
    setUsage({ used: 12345, window: 128000 });
    const wrapper = mountPanel();

    expect(wrapper.text()).toContain('上下文容量');
    expect(wrapper.get('[data-test="ctx-usage-summary"]').text())
      .toBe('12,345 / 128,000（9.6%）');
    wrapper.unmount();
  });

  it('窗口为 0 时摘要不产生 NaN，进度条宽度按 0 处理', () => {
    setUsage({ used: 500, window: 0 });
    const wrapper = mountPanel();

    const summary = wrapper.get('[data-test="ctx-usage-summary"]').text();
    expect(summary).toBe('500 / 0（0.0%）');
    expect(summary).not.toContain('NaN');
    // CSS 数值归一化：0.00% 会被样式表规范为 0%
    expect(wrapper.get('[data-test="ctx-usage-bar-fill"]').attributes('style'))
      .toContain('width: 0%');
    wrapper.unmount();
  });

  it('按 chars 降序渲染来源占比行，显示中文名与 1 位小数百分比', () => {
    setUsage({
      used: 920,
      window: 128000,
      context_breakdown: [
        { source: 'skills', chars: 100 },
        { source: 'messages', chars: 620 },
        { source: 'system_prompt', chars: 200 },
      ],
    });
    const wrapper = mountPanel();

    const rows = wrapper.findAll('[data-test="ctx-usage-row"]');
    expect(rows).toHaveLength(3);

    expect(rows[0].find('.cxp-row-name').text()).toBe('消息');
    expect(rows[0].find('.cxp-row-pct').text()).toBe('67.4%');
    expect(rows[1].find('.cxp-row-name').text()).toBe('系统提示词');
    expect(rows[1].find('.cxp-row-pct').text()).toBe('21.7%');
    expect(rows[2].find('.cxp-row-name').text()).toBe('技能');
    expect(rows[2].find('.cxp-row-pct').text()).toBe('10.9%');

    // 有 breakdown：进度条按来源分段着色（色块与分段的颜色派生自主题变量）
    const segments = wrapper.findAll('.cxp-bar-seg');
    expect(segments).toHaveLength(3);
    expect(segments[0].attributes('style')).toContain('color-mix');
    expect(rows[0].find('.cxp-swatch').attributes('style')).toContain('color-mix');
    wrapper.unmount();
  });

  it('渲染系统工具与 MCP 工具两类来源行', () => {
    setUsage({
      used: 1000,
      window: 128000,
      context_breakdown: [
        { source: 'system_tools', chars: 800 },
        { source: 'mcp_tools', chars: 100 },
      ],
    });
    const wrapper = mountPanel();
    const rows = wrapper.findAll('[data-test="ctx-usage-row"]');
    expect(rows).toHaveLength(2);
    expect(rows[0].find('.cxp-row-name').text()).toBe('系统工具');
    expect(rows[1].find('.cxp-row-name').text()).toBe('MCP 工具');
    wrapper.unmount();
  });

  it('过滤非法来源与非正 chars；Σchars 为 0 时整段列表隐藏', () => {
    setUsage({
      used: 100,
      window: 128000,
      context_breakdown: [
        { source: 'bogus' as ContextBreakdownItem['source'], chars: 50 },
        { source: 'messages', chars: 0 },
        { source: 'system_tools', chars: -3 },
        // 旧来源 tools 已从白名单移除，须被静默过滤
        { source: 'tools' as ContextBreakdownItem['source'], chars: 42 },
      ],
    });
    const wrapper = mountPanel();
    expect(wrapper.find('[data-test="ctx-usage-breakdown"]').exists()).toBe(false);
    // 无有效来源：进度条为单色（无分段），且不出现 NaN
    expect(wrapper.findAll('.cxp-bar-seg')).toHaveLength(0);
    expect(wrapper.text()).not.toContain('NaN');
    wrapper.unmount();

    setUsage({ used: 100, window: 128000, context_breakdown: [] });
    const empty = mountPanel();
    expect(empty.find('[data-test="ctx-usage-breakdown"]').exists()).toBe(false);
    empty.unmount();
  });

  it('缓存命中率仅在 cache_available 且有值时显示，1 位小数', async () => {
    setUsage({ cache_available: true, cache_session_pct: 91.25 });
    const wrapper = mountPanel();

    const cacheRow = wrapper.get('[data-test="ctx-usage-cache"]');
    expect(cacheRow.text()).toContain('缓存命中率');
    expect(cacheRow.text()).toContain('91.3%');

    // cache_available=false 时隐藏（即使有数值）
    setUsage({ cache_available: false, cache_session_pct: 91.25 });
    await wrapper.vm.$nextTick();
    expect(wrapper.find('[data-test="ctx-usage-cache"]').exists()).toBe(false);

    // 有 available 但无值（null）时同样隐藏
    setUsage({ cache_available: true, cache_session_pct: null });
    await wrapper.vm.$nextTick();
    expect(wrapper.find('[data-test="ctx-usage-cache"]').exists()).toBe(false);
    wrapper.unmount();
  });

  it('向上弹出、水平对齐锚点并在视口内夹取', () => {
    // jsdom 视口 1024x768：锚点靠右 → left 夹到 1024-12-320=692；bottom=768-700+8=76
    const wrapper = mountPanel({ anchorEl: makeAnchor({ left: 900, right: 930 }) });

    const style = wrapper.get('.ctx-usage-panel').attributes('style');
    expect(style).toContain('left: 692px');
    expect(style).toContain('bottom: 76px');
    expect(style).toContain('width: 320px');
    wrapper.unmount();
  });

  it('缓存与保活命中率各占一行，车道身份不再上界面', async () => {
    setUsage({
      cache_available: true,
      cache_session_pct: 90.81,
      cache_lane_model: 'openai/deepseek-v4.1-flash',
      cache_lane_api_base: 'https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1',
      cache_keepalive_session_pct: 30.97,
    });
    const wrapper = mountPanel();

    // 车道身份行（模型 @ 接口）已下线：数据字段仍在，界面不再渲染
    expect(wrapper.find('[data-test="ctx-usage-cache-lane"]').exists()).toBe(false);
    expect(wrapper.get('[data-test="ctx-usage-cache"]').text()).toContain('缓存命中率');

    // 保活单列：与主口径同行的另一个数字，1 位小数
    const keepalive = wrapper.get('[data-test="ctx-usage-cache-keepalive"]');
    expect(keepalive.text()).toContain('保活命中率');
    expect(keepalive.text()).toContain('31.0%');
    wrapper.unmount();
  });

  it('保活未报告（null）时整列隐藏，绝不显示 0%', async () => {
    setUsage({
      cache_available: true,
      cache_session_pct: 90.81,
      cache_lane_model: 'openai/local-vllm',
      cache_lane_api_base: 'http://127.0.0.1:8000/v1',
      cache_keepalive_session_pct: null,
    });
    const wrapper = mountPanel();
    expect(wrapper.find('[data-test="ctx-usage-cache-keepalive"]').exists()).toBe(false);
    // 车道行已下线：无论后端有没有车道字段，界面都不渲染它
    expect(wrapper.find('[data-test="ctx-usage-cache-lane"]').exists()).toBe(false);

    setUsage({ cache_lane_model: '', cache_lane_api_base: '' });
    await wrapper.vm.$nextTick();
    expect(wrapper.find('[data-test="ctx-usage-cache-lane"]').exists()).toBe(false);
    wrapper.unmount();
  });
});
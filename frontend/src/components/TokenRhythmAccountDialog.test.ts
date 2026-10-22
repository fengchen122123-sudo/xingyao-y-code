import { flushPromises, mount, type VueWrapper } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import { afterEach, expect, it, vi } from 'vitest';
import { nextTick } from 'vue';
import TokenRhythmAccountDialog from './TokenRhythmAccountDialog.vue';
import { useAppStore } from '../stores/appStore';

// 桥接 mock（写法与 TokenRhythmAccountPanel.test.ts 一致）：弹窗只剩「用量明细 /
// 个人信息」两个视图，各对应一个官方接口，默认都返回 ok；单个用例按需覆盖某个接口
function okUsage() {
  return { ok: true, data: { summary: null, list: [] } };
}
function okProfile() {
  return { ok: true, data: {} };
}

function installBridge(overrides: Record<string, any> = {}) {
  const api: Record<string, any> = {
    tr_usage_detail: vi.fn(async () => okUsage()),
    tr_profile: vi.fn(async () => okProfile()),
    ...overrides,
  };
  (window as any).pywebview = { api };
  return api;
}

let wrapper: VueWrapper | null = null;

/** 建 store + 挂组件（弹窗默认关闭，由用例自己调 openTrDialog） */
function setup() {
  const pinia = createPinia();
  setActivePinia(pinia);
  const store = useAppStore();
  wrapper = mount(TokenRhythmAccountDialog, {
    // 组件用 Teleport 挂到 body；测试里 stub 掉，内容仍留在组件内可查询
    global: { plugins: [pinia], stubs: { teleport: true } },
  });
  return { store, wrapper };
}

/** 打开弹窗并等首次请求落地（组件自己不加载数据，必须显式触发） */
async function openDialog(store: ReturnType<typeof useAppStore>, tab: 'usage' | 'profile') {
  store.openTrDialog(tab);
  await flushPromises();
  await nextTick();
}

afterEach(() => {
  wrapper?.unmount();
  wrapper = null;
  delete (window as any).pywebview;
  vi.restoreAllMocks();
});

it('用量请求未返回时展示“正在加载…”，返回后渲染调用日志', async () => {
  let resolveUsage: ((value: any) => void) | null = null;
  installBridge({
    tr_usage_detail: vi.fn(() => new Promise((resolve) => {
      resolveUsage = resolve;
    })),
  });
  const { store, wrapper: w } = setup();

  store.openTrDialog('usage');
  await nextTick();

  expect(w!.get('[data-test="trd-loading"]').text()).toBe('正在加载…');
  expect(w!.find('[data-test="trd-log-item"]').exists()).toBe(false);

  resolveUsage!({
    ok: true,
    data: {
      summary: { calls: 3, totalTokens: 1200, inputTokens: 800, outputTokens: 400, costCny: '0.25' },
      list: [{ actualModelId: 'qwen-max', status: 'OK', totalTokens: 400, time: '10:00' }],
    },
  });
  await flushPromises();

  expect(w!.find('[data-test="trd-loading"]').exists()).toBe(false);
  expect(w!.findAll('[data-test="trd-log-item"]')).toHaveLength(1);
});

it('用量接口失败展示后端原因，点重试再拉一次', async () => {
  const api = installBridge({
    tr_usage_detail: vi.fn(async () => ({ ok: false, msg: '请先登录基元律动账号' })),
  });
  const { store, wrapper: w } = setup();

  await openDialog(store, 'usage');

  expect(w!.get('[data-test="trd-error"]').text()).toBe('请先登录基元律动账号');
  expect(api.tr_usage_detail).toHaveBeenCalledTimes(1);

  await w!.get('[data-test="trd-retry"]').trigger('click');
  await flushPromises();

  expect(api.tr_usage_detail).toHaveBeenCalledTimes(2);
});

it('用量视图：summary 指标千分位 + 调用日志列表，首次请求带 7d / 第 1 页', async () => {
  const api = installBridge({
    tr_usage_detail: vi.fn(async () => ({
      ok: true,
      data: {
        summary: { calls: 120, totalTokens: 45000, inputTokens: 30000, outputTokens: 15000, costCny: '1.5' },
        list: [{ actualModelId: 'qwen-max', status: 'OK', totalTokens: 900, time: '10:00' }],
      },
    })),
  });
  const { store, wrapper: w } = setup();

  await openDialog(store, 'usage');

  expect(w!.get('[data-test="trd-calls"]').text()).toBe('120');
  expect(w!.get('[data-test="trd-total-tokens"]').text()).toBe('45,000');
  expect(w!.get('[data-test="trd-input-tokens"]').text()).toBe('30,000');
  expect(w!.get('[data-test="trd-output-tokens"]').text()).toBe('15,000');
  expect(w!.get('[data-test="trd-cost"]').text()).toBe('¥1.50');
  const logs = w!.findAll('[data-test="trd-log-item"]');
  expect(logs).toHaveLength(1);
  expect(logs[0].text()).toContain('qwen-max');
  // 默认时间窗 7d、第 1 页、每页 20 条
  expect(api.tr_usage_detail).toHaveBeenCalledTimes(1);
  expect(api.tr_usage_detail.mock.calls[0]).toEqual(['7d', 1, 20]);
});

it('个人信息视图：昵称、手机号打码、原始手机号与状态中文', async () => {
  installBridge({
    tr_profile: vi.fn(async () => ({
      ok: true,
      data: {
        id: '7',
        nickname: '晨',
        phone: '13800138000',
        email: 'a@b.com',
        status: 'active',
        createdAt: '2026-01-02T03:04:05Z',
      },
    })),
  });
  const { store, wrapper: w } = setup();

  await openDialog(store, 'profile');

  expect(w!.find('[data-test="trd-profile"]').exists()).toBe(true);
  expect(w!.get('[data-test="trd-profile-name"]').text()).toBe('晨');
  expect(w!.get('[data-test="trd-avatar"]').text()).toBe('晨');
  expect(w!.get('[data-test="trd-profile-account"]').text()).toBe('138****8000');
  expect(w!.get('[data-test="trd-field-id"]').text()).toBe('7');
  expect(w!.get('[data-test="trd-field-phone"]').text()).toBe('13800138000');
  expect(w!.get('[data-test="trd-field-email"]').text()).toBe('a@b.com');
  expect(w!.get('[data-test="trd-field-status"]').text()).toBe('正常');
});

it('按 Esc 关闭弹窗', async () => {
  installBridge();
  const { store } = setup();

  await openDialog(store, 'usage');
  expect(store.trDialog.show).toBe(true);

  document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
  await nextTick();

  expect(store.trDialog.show).toBe(false);
});

it('点遮罩关闭弹窗，重新打开切到个人信息视图会重新拉数据', async () => {
  const api = installBridge();
  const { store, wrapper: w } = setup();

  await openDialog(store, 'usage');
  expect(store.trDialog.show).toBe(true);

  await w!.get('[data-test="tr-dialog-mask"]').trigger('click');
  await nextTick();
  expect(store.trDialog.show).toBe(false);

  await openDialog(store, 'profile');
  expect(store.trDialog.show).toBe(true);
  expect(api.tr_profile).toHaveBeenCalledTimes(1);
  expect(w!.find('[data-test="trd-profile"]').exists()).toBe(true);
});

it('顶部 tab 栏只剩用量明细与个人信息，充值 tab 已彻底删除', async () => {
  installBridge();
  const { store, wrapper: w } = setup();

  await openDialog(store, 'usage');

  // 本次需求的核心回归：充值 tab 与其视图整体移除
  expect(w!.find('[data-test="trd-tab-recharge"]').exists()).toBe(false);
  expect(w!.find('[data-test="tr-dialog"]').text()).not.toContain('充值');
  // 剩下两个入口仍在，且默认停在用量明细
  expect(w!.find('[data-test="trd-tab-usage"]').exists()).toBe(true);
  expect(w!.find('[data-test="trd-tab-profile"]').exists()).toBe(true);
  expect(w!.find('[data-test="trd-usage"]').exists()).toBe(true);

  // 点个人信息 tab 能正常切过去
  await w!.get('[data-test="trd-tab-profile"]').trigger('click');
  await flushPromises();
  expect(store.trDialog.tab).toBe('profile');
  expect(w!.find('[data-test="trd-profile"]').exists()).toBe(true);
  expect(w!.find('[data-test="trd-usage"]').exists()).toBe(false);
});

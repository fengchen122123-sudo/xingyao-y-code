import { flushPromises, mount, type VueWrapper } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import { afterEach, expect, it, vi } from 'vitest';
import { nextTick } from 'vue';
import TokenRhythmAccountPanel from './TokenRhythmAccountPanel.vue';
import { useAppStore } from '../stores/appStore';

// 桥接 mock（写法与 TokenRhythmLoginModal.test.ts 一致）：默认已登录且总览接口返回
// 一份完整数据；单个用例按需覆盖某个方法
function okOverview(overrides: Record<string, any> = {}) {
  return {
    ok: true,
    user: {
      id: 7,
      name: '阿晨',
      nickname: '晨',
      phone: '13800138000',
      email: 'achen@example.com',
      status: 'active',
    },
    wallet: { available_balance_cny: '298.09757398', frozen_balance_cny: '', currency: 'CNY' },
    usage_today: {
      calls: 12,
      total_tokens: 34567,
      input_tokens: 30000,
      output_tokens: 4567,
      cost_cny: '1.23456789',
    },
    ...overrides,
  };
}

function installBridge(overrides: Record<string, any> = {}) {
  const api: Record<string, any> = {
    tr_auth_status: vi.fn(async () => ({
      ok: true,
      logged_in: true,
      user: { name: '阿晨', phone: '13800138000' },
      configured: true,
    })),
    tr_account_overview: vi.fn(async () => okOverview()),
    tr_auth_logout: vi.fn(async () => ({ ok: true, msg: '已退出基元律动账号' })),
    open_external_url: vi.fn(async () => ({ ok: true })),
    ...overrides,
  };
  (window as any).pywebview = { api };
  return api;
}

let wrapper: VueWrapper | null = null;

/** 建 store + 挂组件（浮层默认关闭，由用例自己调 openTrAccountPanel） */
function setup() {
  const pinia = createPinia();
  setActivePinia(pinia);
  const store = useAppStore();
  // 前置条件：已登录（未登录时点卡片走的是登录弹窗，不归本组件管）
  store.trAuth.status = 'loggedIn';
  wrapper = mount(TokenRhythmAccountPanel, {
    // 组件用 Teleport 挂到 body（避免被侧栏裁剪）；测试里 stub 掉，内容仍留在组件内可查询
    global: { plugins: [pinia], stubs: { teleport: true } },
  });
  return { store, wrapper };
}

/** 打开浮层并等首次总览请求落地 */
async function openPanel(store: ReturnType<typeof useAppStore>) {
  store.openTrAccountPanel();
  await flushPromises();
  await nextTick();
}

afterEach(() => {
  wrapper?.unmount();
  wrapper = null;
  delete (window as any).pywebview;
  vi.restoreAllMocks();
  // 用例里伪造的卡片节点必须清掉，避免污染后续断言
  document.querySelectorAll('[data-test="side-user-card"]').forEach((el) => el.remove());
});

it('加载成功后展示两位小数余额、今日用量与千分位 Token', async () => {
  installBridge();
  const { store, wrapper: w } = setup();

  await openPanel(store);

  // 头部：昵称 + 手机号掩码（只显示中间四位）
  expect(w!.get('[data-test="tr-acc-name"]').text()).toBe('阿晨');
  expect(w!.get('[data-test="tr-acc-account"]').text()).toBe('138****8000');
  expect(w!.get('[data-test="tr-acc-avatar"]').text()).toBe('阿');
  // 余额：298.09757398 → ¥298.10；今日已用 1.23456789 → ¥1.23
  expect(w!.get('[data-test="tr-acc-balance"]').text()).toBe('¥298.10');
  expect(w!.get('[data-test="tr-acc-today-cost"]').text()).toContain('¥1.23');
  // 今日用量：调用次数、Token 千分位、输入/输出
  expect(w!.get('[data-test="tr-acc-calls"]').text()).toBe('12');
  expect(w!.get('[data-test="tr-acc-tokens"]').text()).toBe('34,567');
  const io = w!.get('[data-test="tr-acc-io"]').text();
  expect(io).toContain('30,000');
  expect(io).toContain('4,567');
  // 底部来源说明固定存在
  expect(w!.get('[data-test="tr-account-panel"]').text()).toContain('数据来自基元律动官方接口');
});

it('打开时无数据显示“正在加载…”，数据返回后替换成余额', async () => {
  let resolveOverview: ((value: any) => void) | null = null;
  installBridge({
    tr_account_overview: vi.fn(() => new Promise((resolve) => {
      resolveOverview = resolve;
    })),
  });
  const { store, wrapper: w } = setup();

  store.openTrAccountPanel();
  await nextTick();

  expect(w!.get('[data-test="tr-acc-loading"]').text()).toBe('正在加载…');
  expect(w!.find('[data-test="tr-acc-balance"]').exists()).toBe(false);

  resolveOverview!(okOverview());
  await flushPromises();

  expect(w!.find('[data-test="tr-acc-loading"]').exists()).toBe(false);
  expect(w!.get('[data-test="tr-acc-balance"]').text()).toBe('¥298.10');
});

it('未登录（后端 ok=false）展示后端原因与重试按钮，重试成功后展示数据', async () => {
  const api = installBridge({
    tr_account_overview: vi
      .fn()
      .mockResolvedValueOnce({ ok: false, msg: '尚未登录基元律动账号' })
      .mockResolvedValueOnce(okOverview()),
  });
  const { store, wrapper: w } = setup();

  await openPanel(store);

  expect(w!.get('[data-test="tr-acc-error"]').text()).toBe('尚未登录基元律动账号');
  expect(w!.find('[data-test="tr-acc-retry"]').exists()).toBe(true);
  expect(w!.find('[data-test="tr-acc-balance"]').exists()).toBe(false);

  await w!.get('[data-test="tr-acc-retry"]').trigger('click');
  await flushPromises();

  expect(api.tr_account_overview).toHaveBeenCalledTimes(2);
  expect(w!.find('[data-test="tr-acc-error"]').exists()).toBe(false);
  expect(w!.get('[data-test="tr-acc-balance"]').text()).toBe('¥298.10');
});

it('充值 / 用量明细 / 个人信息 在应用内右栏浏览器打开对应官方页面', async () => {
  const api = installBridge();
  const { store, wrapper: w } = setup();

  const cases: Array<[string, string]> = [
    ['tr-acc-recharge', 'https://tokenrhythm.studio/account'],
    ['tr-acc-usage-detail', 'https://tokenrhythm.studio/account/usage'],
    ['tr-acc-profile', 'https://tokenrhythm.studio/account/profile'],
  ];
  for (const [btn, url] of cases) {
    // 每次点击都会收起浮层，下一个入口要重新打开
    await openPanel(store);
    await w!.get(`[data-test="${btn}"]`).trigger('click');
    await nextTick();
    // 导航请求交给右栏 BrowserPane 消费；nonce 自增保证同一入口重复点击也能触发
    expect(store.browserNavRequest).toEqual({ url, path: '' });
    expect(store.rightPanelTabs).toContain('browser');
    expect(store.rightPanelActiveTab).toBe('browser');
    // 浮层收起：不压住刚摊开的右栏
    expect(store.trAccountPanel.show).toBe(false);
  }
  // 全程不拉系统浏览器
  expect(api.open_external_url).not.toHaveBeenCalled();
});

it('宿主不支持内嵌浏览器时回退系统浏览器打开', async () => {
  const api = installBridge();
  const { store, wrapper: w } = setup();
  // 非 Windows 桌面端 GUI：BrowserPane 判定宿主不支持后置 false
  store.embeddedBrowser.supported = false;

  await openPanel(store);
  await w!.get('[data-test="tr-acc-recharge"]').trigger('click');
  await flushPromises();

  expect(api.open_external_url).toHaveBeenCalledWith('https://tokenrhythm.studio/account');
  // 不当成右栏导航请求：否则 BrowserPane 会拿空 path 去走源码预览
  expect(store.browserNavRequest).toBeNull();
  // 回退路径同样收起浮层
  expect(store.trAccountPanel.show).toBe(false);
});

it('退出登录成功后关闭浮层并清空账号快照', async () => {
  const api = installBridge({
    // 退出后后端权威状态回到未登录：组件会再探测一次，卡片随之回到"星瑶Code"
    tr_auth_status: vi.fn(async () => ({ ok: true, logged_in: false, user: null, configured: false })),
  });
  const { store, wrapper: w } = setup();
  await openPanel(store);
  expect(store.trOverview.data).not.toBeNull();

  await w!.get('[data-test="tr-acc-logout"]').trigger('click');
  await flushPromises();

  expect(api.tr_auth_logout).toHaveBeenCalledTimes(1);
  expect(store.trAccountPanel.show).toBe(false);
  expect(store.trOverview.data).toBeNull();
  expect(store.trAuth.status).toBe('anonymous');
  expect(w!.find('[data-test="tr-account-panel"]').exists()).toBe(false);
});

it('退出登录失败时保留浮层并展示后端原因', async () => {
  installBridge({
    tr_auth_logout: vi.fn(async () => ({ ok: false, msg: '网络异常，退出失败' })),
  });
  const { store, wrapper: w } = setup();
  await openPanel(store);

  await w!.get('[data-test="tr-acc-logout"]').trigger('click');
  await flushPromises();

  expect(w!.get('[data-test="tr-acc-logout-error"]').text()).toBe('网络异常，退出失败');
  expect(store.trAccountPanel.show).toBe(true);
});

it('点击浮层外部关闭，点浮层内部与左下角卡片本身不关闭', async () => {
  installBridge();
  const { store, wrapper: w } = setup();
  await openPanel(store);

  // 浮层内部：不关闭
  await w!.get('[data-test="tr-account-panel"]').trigger('mousedown');
  await nextTick();
  expect(store.trAccountPanel.show).toBe(true);

  // 左下角用户卡片自身：由它负责 toggle，不算"外部"
  const card = document.createElement('button');
  card.setAttribute('data-test', 'side-user-card');
  document.body.appendChild(card);
  card.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
  await nextTick();
  expect(store.trAccountPanel.show).toBe(true);
  card.remove();

  // 其它任意位置：关闭
  document.body.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
  await nextTick();
  expect(store.trAccountPanel.show).toBe(false);
  expect(w!.find('[data-test="tr-account-panel"]').exists()).toBe(false);
});

it('组件卸载后移除 document 监听，外部点击不再触发关闭', async () => {
  installBridge();
  const { store, wrapper: w } = setup();
  await openPanel(store);

  w!.unmount();
  wrapper = null;

  document.body.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
  await nextTick();
  expect(store.trAccountPanel.show).toBe(true);
});

it('partial=true 时在底部提示部分数据获取失败，且不打断展示', async () => {
  installBridge({
    tr_account_overview: vi.fn(async () => okOverview({ partial: true })),
  });
  const { store, wrapper: w } = setup();

  await openPanel(store);

  expect(w!.get('[data-test="tr-acc-partial"]').text()).toContain('部分数据获取失败');
  // 余额仍然正常展示（不是被错误页顶掉）
  expect(w!.get('[data-test="tr-acc-balance"]').text()).toBe('¥298.10');
});

it('字段缺失时显示占位符，不出现 undefined / NaN', async () => {
  installBridge({
    tr_account_overview: vi.fn(async () => ({ ok: true, user: {}, wallet: {}, usage_today: {} })),
  });
  const { store, wrapper: w } = setup();

  await openPanel(store);

  expect(w!.get('[data-test="tr-acc-balance"]').text()).toBe('—');
  expect(w!.get('[data-test="tr-acc-tokens"]').text()).toBe('—');
  expect(w!.get('[data-test="tr-acc-calls"]').text()).toBe('—');
  expect(w!.get('[data-test="tr-acc-account"]').text()).toBe('—');
  const text = w!.get('[data-test="tr-account-panel"]').text();
  expect(text).not.toContain('undefined');
  expect(text).not.toContain('NaN');
});

it('非人民币币种显示币种代码而不是 ¥', async () => {
  installBridge({
    tr_account_overview: vi.fn(async () => okOverview({
      wallet: { available_balance_cny: '12.5', frozen_balance_cny: '', currency: 'USD' },
    })),
  });
  const { store, wrapper: w } = setup();

  await openPanel(store);

  expect(w!.get('[data-test="tr-acc-balance"]').text()).toBe('12.50 USD');
});

it('桥接不支持 tr_account_overview 时给出可重试的中文提示', async () => {
  installBridge();
  delete (window as any).pywebview.api.tr_account_overview;
  const { store, wrapper: w } = setup();

  await openPanel(store);

  expect(w!.get('[data-test="tr-acc-error"]').text()).toContain('不支持账号总览');
  expect(w!.find('[data-test="tr-acc-retry"]').exists()).toBe(true);
});

it('有临时额度时展示两位小数金额与最近到期日', async () => {
  const iso = '2026-11-01T10:20:30+08:00';
  installBridge({
    tr_account_overview: vi.fn(async () => okOverview({
      temporary_credits: {
        total_cny: '12.30000000',
        count: 2,
        nearest_expires_at: iso,
        nearest_amount_cny: '5.00000000',
      },
    })),
  });
  const { store, wrapper: w } = setup();

  await openPanel(store);

  // 金额沿用 amountText + withCurrency：12.30000000 → ¥12.30
  expect(w!.get('[data-test="tr-acc-temporary"]').text()).toBe('¥12.30');
  // 到期日按本地时区拼 YYYY-MM-DD：用同一个 Date 推算期望值，避免依赖运行环境时区
  const date = new Date(iso);
  const expected = `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`;
  // 小字同时展示金额：5.00000000 → ¥5.00
  expect(w!.get('[data-test="tr-acc-temporary-expire"]').text()).toBe(`最近到期 ${expected} ¥5.00`);
  // 行标签固定存在（说明这一行不会被隐藏）
  expect(w!.get('[data-test="tr-account-panel"]').text()).toContain('临时额度');
});

it('最近到期那笔金额为 0 时只显示日期，不追加金额', async () => {
  const iso = '2026-12-05T09:00:00+08:00';
  installBridge({
    tr_account_overview: vi.fn(async () => okOverview({
      temporary_credits: {
        total_cny: '3.00000000',
        count: 1,
        nearest_expires_at: iso,
        nearest_amount_cny: '0',
      },
    })),
  });
  const { store, wrapper: w } = setup();

  await openPanel(store);

  const date = new Date(iso);
  const expected = `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`;
  expect(w!.get('[data-test="tr-acc-temporary-expire"]').text()).toBe(`最近到期 ${expected}`.trim());
});

it('无临时额度时展示占位符「—」且不出现到期小字', async () => {
  installBridge({
    tr_account_overview: vi.fn(async () => okOverview({
      temporary_credits: { total_cny: '0', count: 0, nearest_expires_at: '2026-11-01T10:20:30+08:00' },
    })),
  });
  const { store, wrapper: w } = setup();

  await openPanel(store);

  expect(w!.get('[data-test="tr-acc-temporary"]').text()).toBe('—');
  // 整行不隐藏（避免布局跳动），只是小字位置留空
  expect(w!.get('[data-test="tr-account-panel"]').text()).toContain('临时额度');
  expect(w!.find('[data-test="tr-acc-temporary-expire"]').exists()).toBe(false);
});

it('到期时间非法字符串时只显示金额，不显示到期小字', async () => {
  installBridge({
    tr_account_overview: vi.fn(async () => okOverview({
      temporary_credits: { total_cny: '5.5', count: 1, nearest_expires_at: '不是时间' },
    })),
  });
  const { store, wrapper: w } = setup();

  await openPanel(store);

  expect(w!.get('[data-test="tr-acc-temporary"]').text()).toBe('¥5.50');
  expect(w!.find('[data-test="tr-acc-temporary-expire"]').exists()).toBe(false);
  expect(w!.get('[data-test="tr-account-panel"]').text()).not.toContain('NaN');
});

it('接口未返回 temporary_credits 时临时额度显示占位符', async () => {
  installBridge({
    tr_account_overview: vi.fn(async () => ({
      ok: true,
      user: { name: '阿晨' },
      wallet: { available_balance_cny: '9.9', currency: 'CNY' },
      usage_today: { cost_cny: '0.1', calls: 1, total_tokens: 10, input_tokens: 6, output_tokens: 4 },
    })),
  });
  const { store, wrapper: w } = setup();

  await openPanel(store);

  expect(w!.get('[data-test="tr-acc-temporary"]').text()).toBe('—');
  expect(w!.find('[data-test="tr-acc-temporary-expire"]').exists()).toBe(false);
});

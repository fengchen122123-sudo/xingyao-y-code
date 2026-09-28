import { createPinia, setActivePinia } from 'pinia';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { nextTick } from 'vue';
import { mount } from '@vue/test-utils';

import BrowserPane from './BrowserPane.vue';
import { useAppStore } from '../stores/appStore';

// pywebview 两步注入（api.js 先给空 {}，finish.js 之后才填方法并触发
// pywebviewready）的等待宽限期，与组件内 BRIDGE_GRACE_MS 保持一致
const BRIDGE_GRACE_MS = 5000;

// jsdom 无 ResizeObserver：显式提供空实现，覆盖「存在但不回调」的分支
class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}

// 遮挡检测的命中结果：默认返回表面自身（不遮挡）
let surfaceEl: Element | null = null;

function domRect(x: number, y: number, w: number, h: number): DOMRect {
  return {
    x, y, left: x, top: y, width: w, height: h, right: x + w, bottom: y + h,
    toJSON: () => ({}),
  } as DOMRect;
}

function mockApi() {
  return {
    browser_pane_open: vi.fn().mockResolvedValue({ ok: true }),
    browser_pane_set_bounds: vi.fn().mockResolvedValue({ ok: true }),
    browser_pane_navigate: vi.fn().mockResolvedValue({ ok: true }),
    browser_pane_back: vi.fn().mockResolvedValue({ ok: true }),
    browser_pane_forward: vi.fn().mockResolvedValue({ ok: true }),
    browser_pane_reload: vi.fn().mockResolvedValue({ ok: true }),
    browser_pane_stop: vi.fn().mockResolvedValue({ ok: true }),
    browser_pane_hide: vi.fn().mockResolvedValue({ ok: true }),
    browser_pane_get_state: vi.fn().mockResolvedValue({ ok: true, state: null }),
  };
}

beforeEach(() => {
  setActivePinia(createPinia());
  delete (window as any).pywebview;
  surfaceEl = null;
  (globalThis as any).ResizeObserver = ResizeObserverStub;
  // jsdom 的 getBoundingClientRect 恒为 0 矩形，会让「宽高 < 2 视为不可见」
  // 命中，open 永远不会被调用：给所有元素一个真实的表面矩形；自由尺寸的
  // 视口框单独给一个居中小矩形，用于验证矩形上报源切换
  vi.spyOn(Element.prototype, 'getBoundingClientRect').mockImplementation(function (this: Element) {
    if (this.classList.contains('bp-frame')) return domRect(14, 25, 272, 350);
    return domRect(0, 0, 300, 400);
  });
  // 命中结果跟随当前矩形上报源：视口框存在时返回框自身（不遮挡），否则表面
  (document as any).elementFromPoint = vi.fn(() => (
    (document.querySelector('[data-test="bp-frame"]') as Element | null) ?? surfaceEl
  ));
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

async function mountPane(active = true, attach = false) {
  // attach=true：挂到 document 上。自由尺寸用例必须如此——矩形上报源的
  // elementFromPoint 探测与菜单外点关闭都依赖 document 级查询/事件传播，
  // 脱离 document 的挂载树收不到 document 捕获监听、querySelector 也找不到
  const wrapper = mount(
    BrowserPane,
    attach ? { props: { active }, attachTo: document.body } : { props: { active } },
  );
  surfaceEl = wrapper.find('[data-test="bp-surface"]').element;
  // 等 composable 的 onMounted → nextTick(syncNow) 跑完（首次矩形上报/open）
  await nextTick();
  await nextTick();
  return wrapper;
}

it('挂载时桥未注入：先等待不判死，宽限超时后才显示不支持态文案并禁用工具栏', async () => {
  vi.useFakeTimers();
  const wrapper = await mountPane(true);

  // 启动竞态窗口期：桥尚未注入 → 等待态，绝不提前锁死「宿主不支持」
  expect(wrapper.find('[data-test="bp-bridge-waiting"]').exists()).toBe(true);
  expect(wrapper.find('[data-test="bp-unsupported"]').exists()).toBe(false);
  expect(useAppStore().embeddedBrowser.supported).toBe(true);

  // 宽限期耗尽仍无桥（纯浏览器调试等永无 pywebview 宿主的环境）→ 判缺失
  vi.advanceTimersByTime(BRIDGE_GRACE_MS + 1);
  await nextTick();

  const notice = wrapper.find('[data-test="bp-unsupported"]');
  expect(notice.exists()).toBe(true);
  expect(notice.text()).toContain('浏览器面板需要桌面端 GUI 宿主支持');
  expect(notice.text()).toContain('内置浏览器仅在 Windows 桌面端 GUI 中可用。');
  // 工具栏整体禁用，且不渲染起始页
  expect(wrapper.find('[data-test="bp-back"]').attributes('disabled')).toBeDefined();
  expect(wrapper.find('[data-test="bp-forward"]').attributes('disabled')).toBeDefined();
  expect(wrapper.find('[data-test="bp-addr"]').attributes('disabled')).toBeDefined();
  expect(wrapper.find('[data-test="bp-start"]').exists()).toBe(false);
  wrapper.unmount();
});

it('桥注入晚于挂载：pywebviewready 事件后恢复就绪并校准状态（两步注入竞态回归）', async () => {
  const api = mockApi();
  const store = useAppStore();
  const wrapper = await mountPane(true);

  // 挂载早于桥注入（RightPanel v-show 常驻挂载的常态）：等待而非判死
  expect(wrapper.find('[data-test="bp-bridge-waiting"]').exists()).toBe(true);
  expect(wrapper.find('[data-test="bp-unsupported"]').exists()).toBe(false);
  expect(store.embeddedBrowser.supported).toBe(true);

  // 宿主完成第二步注入（finish.js 填方法并触发 pywebviewready）
  (window as any).pywebview = { api };
  window.dispatchEvent(new Event('pywebviewready'));
  await nextTick();
  await nextTick();

  expect(wrapper.find('[data-test="bp-unsupported"]').exists()).toBe(false);
  // 就绪触发了 calibrate（get_state 校准）
  expect(api.browser_pane_get_state).toHaveBeenCalledTimes(1);
  // url 仍为空 → 回到起始页；工具栏解除禁用
  expect(wrapper.find('[data-test="bp-start"]').exists()).toBe(true);
  expect(wrapper.find('[data-test="bp-addr"]').attributes('disabled')).toBeUndefined();
  wrapper.unmount();
});

it('active 且已有页面时同步矩形并首次 open(rect, "")', async () => {
  const api = mockApi();
  (window as any).pywebview = { api };
  // 非起始页：允许原生表面显示（起始页态会主动 hide）
  useAppStore().embeddedBrowser.url = 'https://example.com';

  const wrapper = await mountPane(true);

  await vi.waitFor(() => expect(api.browser_pane_open).toHaveBeenCalled());
  const [rect, url] = api.browser_pane_open.mock.calls[0];
  expect(rect).toEqual({ x: 0, y: 0, width: 300, height: 400 });
  expect(url).toBe('');
  expect(wrapper.find('[data-test="bp-unsupported"]').exists()).toBe(false);
  wrapper.unmount();
});

it('地址栏回车提交：普通词走必应搜索，域名补 https://', async () => {
  const api = mockApi();
  (window as any).pywebview = { api };
  useAppStore().embeddedBrowser.url = 'https://example.com';

  const wrapper = await mountPane(true);
  const addr = wrapper.find('[data-test="bp-addr"]');

  await addr.trigger('focus');
  await addr.setValue('hello world');
  await addr.trigger('keydown', { key: 'Enter' });
  expect(api.browser_pane_navigate).toHaveBeenCalledWith(
    'https://www.bing.com/search?q=hello%20world',
  );

  await addr.trigger('focus');
  await addr.setValue('example.com');
  await addr.trigger('keydown', { key: 'Enter' });
  expect(api.browser_pane_navigate).toHaveBeenCalledWith('https://example.com');
  wrapper.unmount();
});

it('IME 组合态下的 Enter 不提交（交由输入法上屏）', async () => {
  const api = mockApi();
  (window as any).pywebview = { api };
  useAppStore().embeddedBrowser.url = 'https://example.com';

  const wrapper = await mountPane(true);
  const addr = wrapper.find('[data-test="bp-addr"]');
  await addr.trigger('focus');
  await addr.setValue('中文候选');
  await addr.trigger('keydown', { key: 'Enter', isComposing: true });
  expect(api.browser_pane_navigate).not.toHaveBeenCalled();
  wrapper.unmount();
});

it('起始页快捷卡片点击 → navigate 对应站点', async () => {
  const api = mockApi();
  (window as any).pywebview = { api };

  const wrapper = await mountPane(true);
  expect(wrapper.find('[data-test="bp-start"]').exists()).toBe(true);

  await wrapper.find('[data-test="bp-site-github"]').trigger('click');
  // 首次导航被串行化到 open 之后（B1），用 waitFor 等 Promise 链落地
  await vi.waitFor(() =>
    expect(api.browser_pane_navigate).toHaveBeenCalledWith('https://github.com'),
  );
  wrapper.unmount();
});

it('首次导航串行等待 open 落地后才发出（B1 竞态回归）', async () => {
  const api = mockApi();
  let resolveOpen: (value: unknown) => void = () => {};
  api.browser_pane_open.mockReturnValue(
    new Promise((resolve) => { resolveOpen = resolve; }),
  );
  (window as any).pywebview = { api };

  const wrapper = await mountPane(true);
  // 起始页态：表面隐藏，open 尚未调用
  expect(api.browser_pane_open).not.toHaveBeenCalled();

  await wrapper.find('[data-test="bp-site-github"]').trigger('click');
  await vi.waitFor(() => expect(api.browser_pane_open).toHaveBeenCalled());
  // open 还挂着（模拟 WebView2 创建/内核初始化耗时）：navigate 不得抢跑
  expect(api.browser_pane_navigate).not.toHaveBeenCalled();

  resolveOpen({ ok: true });
  await vi.waitFor(() =>
    expect(api.browser_pane_navigate).toHaveBeenCalledWith('https://github.com'),
  );
  wrapper.unmount();
});

it('矩形未变化时 set_bounds 去重不重发', async () => {
  const api = mockApi();
  (window as any).pywebview = { api };
  useAppStore().embeddedBrowser.url = 'https://example.com';

  const wrapper = await mountPane(true);
  await vi.waitFor(() => expect(api.browser_pane_open).toHaveBeenCalled());

  // 多次触发同步源：矩形不变 → 不应产生任何 set_bounds 调用
  window.dispatchEvent(new Event('resize'));
  window.dispatchEvent(new Event('resize'));
  await nextTick();
  await nextTick();
  expect(api.browser_pane_set_bounds).not.toHaveBeenCalled();
  wrapper.unmount();
});

it('表面被 DOM 覆盖层遮挡时让宿主 hide 原生表面', async () => {
  const api = mockApi();
  (window as any).pywebview = { api };
  useAppStore().embeddedBrowser.url = 'https://example.com';

  const wrapper = await mountPane(true);
  await vi.waitFor(() => expect(api.browser_pane_open).toHaveBeenCalled());

  // elementFromPoint 命中 null（模拟设置页/弹窗压住表面中心）→ 判遮挡
  surfaceEl = null;
  window.dispatchEvent(new Event('resize'));
  await vi.waitFor(() => expect(api.browser_pane_hide).toHaveBeenCalled());
  wrapper.unmount();
});

it('组件卸载时让宿主 hide 原生表面（清理不残留）', async () => {
  const api = mockApi();
  (window as any).pywebview = { api };
  useAppStore().embeddedBrowser.url = 'https://example.com';

  const wrapper = await mountPane(true);
  await vi.waitFor(() => expect(api.browser_pane_open).toHaveBeenCalled());

  wrapper.unmount();
  expect(api.browser_pane_hide).toHaveBeenCalled();
});

it('can_go_back=false 时后退禁用；前进/刷新/停止调对应方法', async () => {
  const api = mockApi();
  (window as any).pywebview = { api };
  const store = useAppStore();
  store.embeddedBrowser.url = 'https://example.com';
  store.embeddedBrowser.can_go_back = false;
  store.embeddedBrowser.can_go_forward = true;

  const wrapper = await mountPane(true);

  expect(wrapper.find('[data-test="bp-back"]').attributes('disabled')).toBeDefined();
  expect(wrapper.find('[data-test="bp-forward"]').attributes('disabled')).toBeUndefined();

  await wrapper.find('[data-test="bp-forward"]').trigger('click');
  expect(api.browser_pane_forward).toHaveBeenCalled();

  await wrapper.find('[data-test="bp-reload"]').trigger('click');
  expect(api.browser_pane_reload).toHaveBeenCalled();

  // loading 时刷新按钮变停止：图标旋转 + 调用 browser_pane_stop
  store.embeddedBrowser.loading = true;
  await nextTick();
  await wrapper.find('[data-test="bp-reload"]').trigger('click');
  expect(api.browser_pane_stop).toHaveBeenCalled();
  wrapper.unmount();
});

it('页签切走（active 由 true 变 false）时让宿主 hide 原生表面', async () => {
  const api = mockApi();
  (window as any).pywebview = { api };
  useAppStore().embeddedBrowser.url = 'https://example.com';

  const wrapper = await mountPane(true);
  await vi.waitFor(() => expect(api.browser_pane_open).toHaveBeenCalled());
  expect(api.browser_pane_hide).not.toHaveBeenCalled();

  // 切走（hide 只在可见性翻转时调用，去重避免重复指令）
  await wrapper.setProps({ active: false });
  await vi.waitFor(() => expect(api.browser_pane_hide).toHaveBeenCalled());
  wrapper.unmount();
});

it('open 返回 unsupported_platform 时切不支持态', async () => {
  const api = mockApi();
  api.browser_pane_open.mockResolvedValue({ ok: false, error: 'unsupported_platform' });
  (window as any).pywebview = { api };
  useAppStore().embeddedBrowser.url = 'https://example.com';

  const wrapper = await mountPane(true);

  await vi.waitFor(() =>
    expect(wrapper.find('[data-test="bp-unsupported"]').exists()).toBe(true),
  );
  wrapper.unmount();
});

// ── 自由尺寸（自定义视口）──

it('自由尺寸：应用预设后矩形上报切到视口框，适配面板恢复整面', async () => {
  const api = mockApi();
  (window as any).pywebview = { api };
  useAppStore().embeddedBrowser.url = 'https://example.com';

  const wrapper = await mountPane(true, true);
  await vi.waitFor(() => expect(api.browser_pane_open).toHaveBeenCalled());
  // 默认整面矩形
  expect(api.browser_pane_open.mock.calls[0][0]).toEqual({ x: 0, y: 0, width: 300, height: 400 });

  await wrapper.find('[data-test="bp-size-toggle"]').trigger('click');
  expect(wrapper.find('[data-test="bp-size-menu"]').exists()).toBe(true);
  await wrapper.find('[data-test="bp-size-phone"]').trigger('click');
  await nextTick();
  await nextTick();

  // 视口框渲染、菜单随选择关闭；菜单开→合使表面经历 hide→show 翻转，
  // 矩形经 open 重报（open 幂等重定位），末次调用携带视口框矩形与缩放
  // （jsdom 无布局，clientWidth=0 → 不缩放，zoom 回落 1）
  expect(wrapper.find('[data-test="bp-frame"]').exists()).toBe(true);
  expect(wrapper.find('[data-test="bp-size-menu"]').exists()).toBe(false);
  await vi.waitFor(() => {
    const calls = api.browser_pane_open.mock.calls;
    expect(calls.length).toBeGreaterThanOrEqual(2);
    expect(calls[calls.length - 1]).toEqual([{ x: 14, y: 25, width: 272, height: 350 }, '', 1]);
  });

  // 适配面板：视口框卸载，上报源切回整块表面
  await wrapper.find('[data-test="bp-size-toggle"]').trigger('click');
  await wrapper.find('[data-test="bp-size-fit"]').trigger('click');
  await nextTick();
  await nextTick();
  expect(wrapper.find('[data-test="bp-frame"]').exists()).toBe(false);
  await vi.waitFor(() => {
    const calls = api.browser_pane_open.mock.calls;
    expect(calls[calls.length - 1][0]).toEqual({ x: 0, y: 0, width: 300, height: 400 });
  });
  wrapper.unmount();
});

it('自由尺寸菜单打开时让宿主 hide 原生表面；点菜单外区域关闭并恢复', async () => {
  const api = mockApi();
  (window as any).pywebview = { api };
  useAppStore().embeddedBrowser.url = 'https://example.com';

  const wrapper = await mountPane(true, true);
  await vi.waitFor(() => expect(api.browser_pane_open).toHaveBeenCalled());
  expect(api.browser_pane_hide).not.toHaveBeenCalled();

  await wrapper.find('[data-test="bp-size-toggle"]').trigger('click');
  expect(wrapper.find('[data-test="bp-size-menu"]').exists()).toBe(true);
  await vi.waitFor(() => expect(api.browser_pane_hide).toHaveBeenCalled());

  // 菜单外 pointerdown（捕获监听）：菜单关闭、原生表面恢复整面
  await wrapper.find('[data-test="bp-addr"]').trigger('pointerdown');
  await vi.waitFor(() =>
    expect(wrapper.find('[data-test="bp-size-menu"]').exists()).toBe(false),
  );
  await vi.waitFor(() => {
    const calls = api.browser_pane_open.mock.calls;
    expect(calls.length).toBeGreaterThanOrEqual(2);
    expect(calls[calls.length - 1][0]).toEqual({ x: 0, y: 0, width: 300, height: 400 });
  });
  wrapper.unmount();
});

it('画笔截图模式让宿主 hide 原生表面；模式关闭后自动恢复', async () => {
  const api = mockApi();
  (window as any).pywebview = { api };
  const store = useAppStore();
  useAppStore().embeddedBrowser.url = 'https://example.com';

  const wrapper = await mountPane(true, true);
  await vi.waitFor(() => expect(api.browser_pane_open).toHaveBeenCalled());
  expect(api.browser_pane_hide).not.toHaveBeenCalled();

  // 冻结帧覆盖层接管面板：原生表面必须让位（其像素已在快照里）
  store.rightPanelSnipActive = true;
  await nextTick();
  await vi.waitFor(() => expect(api.browser_pane_hide).toHaveBeenCalled());

  // snip 关闭：shouldShowSurface 翻回 true，既有 watch 自动 re-show
  store.rightPanelSnipActive = false;
  await nextTick();
  await vi.waitFor(() => {
    const calls = api.browser_pane_open.mock.calls;
    expect(calls.length).toBeGreaterThanOrEqual(2);
    expect(calls[calls.length - 1][0]).toEqual({ x: 0, y: 0, width: 300, height: 400 });
  });
  wrapper.unmount();
});

it('画笔截图模式优先于自动化视口：起始页保显例外也不生效', async () => {
  const api = mockApi();
  (window as any).pywebview = { api };
  const store = useAppStore();
  store.embeddedBrowser.automationViewport = [1280, 800];

  const wrapper = await mountPane(true, true);
  await vi.waitFor(() => expect(api.browser_pane_open).toHaveBeenCalled());

  store.rightPanelSnipActive = true;
  await nextTick();
  await vi.waitFor(() => expect(api.browser_pane_hide).toHaveBeenCalled());
  wrapper.unmount();
});

// ── 星瑶自动化呈现回归（会话区被浏览器铺满 / 首次 observe 挂死事故）──

it('自动化视口下发时即使还在起始页也打开原生表面（隐藏 WebView2 截图会挂起的回归）', async () => {
  const api = mockApi();
  (window as any).pywebview = { api };
  const store = useAppStore();
  const wrapper = await mountPane(true, true);

  // 起始页态（url 为空）：无自动化时不打开原生表面，DOM 起始页可见
  expect(store.embeddedBrowser.url).toBe('');
  await vi.waitFor(() => expect(api.browser_pane_get_state).toHaveBeenCalled());
  expect(api.browser_pane_open).not.toHaveBeenCalled();

  // 后端下发自动化视口：表面立即打开（自动化马上要经 CDP 导航，
  // 隐藏控件会让首次 observe 截图挂起 → backend 被判封闭）
  store.embeddedBrowser.automationViewport = [1440, 900];
  await nextTick();
  await nextTick();
  await vi.waitFor(() => expect(api.browser_pane_open).toHaveBeenCalled());
  const [rect, url, zoom] = api.browser_pane_open.mock.calls[0];
  expect(url).toBe('');
  expect(zoom).toBe(1); // 量的是整块表面（框未渲染），缩放必须恒等 1
  expect(rect).toEqual({ x: 0, y: 0, width: 300, height: 400 });
  wrapper.unmount();
});

it('自动化视口框矩形越出占位区时被夹紧回占位区内（铺满会话区事故的兜底回归）', async () => {
  const api = mockApi();
  (window as any).pywebview = { api };
  useAppStore().embeddedBrowser.url = 'https://example.com';

  // 事故形态重现：视口框按原始逻辑尺寸渲染（占位区不可测量时兜底缩放=1），
  // 框矩形（1440×900，中心锚定）大幅越出 300×400 的占位区
  const rectSpy = vi.spyOn(Element.prototype, 'getBoundingClientRect').mockImplementation(function (this: Element) {
    if (this.classList.contains('bp-frame')) return domRect(-570, 100, 1440, 900);
    return domRect(0, 0, 300, 400);
  });

  const store = useAppStore();
  const wrapper = await mountPane(true, true);
  store.embeddedBrowser.automationViewport = [1440, 900];
  await nextTick();
  await nextTick();
  await vi.waitFor(() => expect(wrapper.find('[data-test="bp-frame"]').exists()).toBe(true));

  // 框矩形被夹紧回占位区（交集 = (0,100,300,300)）并作为 set_bounds 重报：
  // 原生控件绝不可能铺到会话区
  await vi.waitFor(() => expect(api.browser_pane_set_bounds).toHaveBeenCalled());
  expect(api.browser_pane_set_bounds.mock.calls[0][0]).toEqual({ x: 0, y: 100, width: 300, height: 300 });
  const reported = [...api.browser_pane_open.mock.calls, ...api.browser_pane_set_bounds.mock.calls];
  expect(reported.length).toBeGreaterThan(0);
  for (const [rect] of reported) {
    expect(rect.x).toBeGreaterThanOrEqual(0);
    expect(rect.y).toBeGreaterThanOrEqual(0);
    expect(rect.x + rect.width).toBeLessThanOrEqual(300);
    expect(rect.y + rect.height).toBeLessThanOrEqual(400);
  }
  expect(rectSpy).toHaveBeenCalled();
  wrapper.unmount();
});

import { createPinia, setActivePinia } from 'pinia';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { nextTick } from 'vue';
import { mount } from '@vue/test-utils';

import BrowserPane from './BrowserPane.vue';
import { useAppStore } from '../stores/appStore';

// pywebview 两步注入（api.js 先给空 {}，finish.js 之后才填方法并触发
// pywebviewready）的等待宽限期，与组件内 BRIDGE_GRACE_MS 保持一致
const BRIDGE_GRACE_MS = 5000;

class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}

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

  expect(wrapper.find('[data-test="bp-bridge-waiting"]').exists()).toBe(true);
  expect(wrapper.find('[data-test="bp-unsupported"]').exists()).toBe(false);
  expect(store.embeddedBrowser.supported).toBe(true);

  // 宿主完成第二步注入（finish.js 填方法并触发 pywebviewready）
  (window as any).pywebview = { api };
  window.dispatchEvent(new Event('pywebviewready'));
  await nextTick();
  await nextTick();

  expect(wrapper.find('[data-test="bp-unsupported"]').exists()).toBe(false);
  expect(api.browser_pane_get_state).toHaveBeenCalledTimes(1);
  expect(wrapper.find('[data-test="bp-start"]').exists()).toBe(true);
  expect(wrapper.find('[data-test="bp-addr"]').attributes('disabled')).toBeUndefined();
  wrapper.unmount();
});

it('Office 文档转换期间显示进行中浮层，优先于起始页，结束后消失', async () => {
  const store = useAppStore();
  const wrapper = await mountPane(true);
  (window as any).pywebview = { api: mockApi() };
  window.dispatchEvent(new Event('pywebviewready'));
  await nextTick();
  await nextTick();

  // 转换中：浮层盖过起始页，给出文件名与耗时预期（否则用户会以为点击没生效）
  store.officePreviewPending = { path: 'D:\\proj\\季度报告.pptx', name: '季度报告.pptx' };
  await nextTick();

  const pending = wrapper.find('[data-test="bp-office-pending"]');
  expect(pending.exists()).toBe(true);
  expect(pending.text()).toContain('季度报告.pptx');
  expect(pending.text()).toContain('首次转换需要');
  expect(wrapper.find('[data-test="bp-start"]').exists()).toBe(false);

  store.officePreviewPending = null;
  await nextTick();
  expect(wrapper.find('[data-test="bp-office-pending"]').exists()).toBe(false);
  wrapper.unmount();
});

it('active 且已有页面时同步矩形并首次 open(rect, "")', async () => {
  const api = mockApi();
  (window as any).pywebview = { api };
  useAppStore().embeddedBrowser.url = 'https://example.com';

  const wrapper = await mountPane(true);

  await vi.waitFor(() => expect(api.browser_pane_open).toHaveBeenCalled());
  const [rect, url] = api.browser_pane_open.mock.calls[0];
  expect(rect).toEqual({ x: 0, y: 0, width: 300, height: 400 });
  expect(url).toBe('');
  expect(wrapper.find('[data-test="bp-unsupported"]').exists()).toBe(false);
  wrapper.unmount();
});

it('浏览器操控期间在工具栏提供停止入口，run 结束后消失', async () => {
  // 浏览器模式不再渲染顶部安全条：停止入口必须在浏览器工具栏可见，
  // 否则用户失去一键停止能力（只剩 Ctrl+Alt+Esc 热键）
  const api = {
    ...mockApi(),
    stop_computer_control: vi.fn().mockResolvedValue({ ok: true, run_id: 'run-1', status: 'stopped' }),
  };
  (window as any).pywebview = { api };
  const store = useAppStore();
  store.computerRun = {
    ...store.computerRun,
    run_id: 'run-1',
    status: 'running',
    target: 'browser',
  };

  const wrapper = await mountPane(true);
  const stop = wrapper.find('[data-test="bp-computer-stop"]');
  expect(stop.exists()).toBe(true);

  await stop.trigger('click');
  expect(api.stop_computer_control).toHaveBeenCalledWith('run-1');

  store.computerRun = { ...store.computerRun, status: 'stopped' };
  await nextTick();
  expect(wrapper.find('[data-test="bp-computer-stop"]').exists()).toBe(false);
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

it('自由尺寸：应用预设后矩形上报切到视口框，适配面板恢复整面', async () => {
  const api = mockApi();
  (window as any).pywebview = { api };
  useAppStore().embeddedBrowser.url = 'https://example.com';

  const wrapper = await mountPane(true, true);
  await vi.waitFor(() => expect(api.browser_pane_open).toHaveBeenCalled());
  expect(api.browser_pane_open.mock.calls[0][0]).toEqual({ x: 0, y: 0, width: 300, height: 400 });

  await wrapper.find('[data-test="bp-size-toggle"]').trigger('click');
  expect(wrapper.find('[data-test="bp-size-menu"]').exists()).toBe(true);
  await wrapper.find('[data-test="bp-size-phone"]').trigger('click');
  await nextTick();
  await nextTick();

  expect(wrapper.find('[data-test="bp-frame"]').exists()).toBe(true);
  expect(wrapper.find('[data-test="bp-size-menu"]').exists()).toBe(false);
  await vi.waitFor(() => {
    const calls = api.browser_pane_open.mock.calls;
    expect(calls.length).toBeGreaterThanOrEqual(2);
    expect(calls[calls.length - 1].slice(0, 3)).toEqual([{ x: 14, y: 25, width: 272, height: 350 }, '', 1]);
    expect(typeof calls[calls.length - 1][4]).toBe('number');
  });

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

it('自动化视口下发时即使还在起始页也打开原生表面（隐藏 WebView2 截图会挂起的回归）', async () => {
  const api = mockApi();
  (window as any).pywebview = { api };
  const store = useAppStore();
  const wrapper = await mountPane(true, true);

  expect(store.embeddedBrowser.url).toBe('');
  await vi.waitFor(() => expect(api.browser_pane_get_state).toHaveBeenCalled());
  expect(api.browser_pane_open).not.toHaveBeenCalled();

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

it('外部导航请求驱动内嵌浏览器导航，并乐观写入地址栏', async () => {
  const api = mockApi();
  (window as any).pywebview = { api };
  const store = useAppStore();

  const wrapper = await mountPane(true);

  store.browserNavRequest = { url: 'file:///D:/proj/index.html', path: 'D:\\proj\\index.html' };
  store.browserNavNonce += 1;
  await nextTick();

  await vi.waitFor(() =>
    expect(api.browser_pane_navigate).toHaveBeenCalledWith('file:///D:/proj/index.html'),
  );
  expect(store.embeddedBrowser.url).toBe('file:///D:/proj/index.html');
  wrapper.unmount();
});

it('请求到达时桥还没就绪：先挂起，pywebviewready 后补导航', async () => {
  const store = useAppStore();
  store.browserNavRequest = { url: 'file:///D:/proj/index.html', path: 'D:\\proj\\index.html' };
  store.browserNavNonce += 1;

  const wrapper = await mountPane(true);
  expect(store.embeddedBrowser.url).toBe('');

  const api = mockApi();
  (window as any).pywebview = { api };
  window.dispatchEvent(new Event('pywebviewready'));
  await nextTick();

  await vi.waitFor(() =>
    expect(api.browser_pane_navigate).toHaveBeenCalledWith('file:///D:/proj/index.html'),
  );
  wrapper.unmount();
});

it('宿主不支持内嵌浏览器时回退源码预览并提示', async () => {
  vi.useFakeTimers();
  const store = useAppStore();
  const previewCalls: string[] = [];
  const notices: string[] = [];
  store.openFilePreview = (async (p: string) => { previewCalls.push(p); }) as any;
  store.notifyChat = ((m: string) => { notices.push(m); }) as any;

  const wrapper = await mountPane(true);
  vi.advanceTimersByTime(BRIDGE_GRACE_MS + 1);
  await nextTick();

  store.browserNavRequest = { url: 'file:///D:/proj/index.html', path: 'D:\\proj\\index.html' };
  store.browserNavNonce += 1;
  await nextTick();

  expect(previewCalls).toEqual(['D:\\proj\\index.html']);
  expect(notices.join('')).toContain('内嵌浏览器不可用');
  wrapper.unmount();
});

it('宿主不支持内嵌浏览器且请求非本地文件（path 为空）时回退系统浏览器', async () => {
  vi.useFakeTimers();
  const store = useAppStore();
  const previewCalls: string[] = [];
  const opened: string[] = [];
  store.openFilePreview = (async (p: string) => { previewCalls.push(p); }) as any;
  store.openExternalUrl = (async (u: string) => { opened.push(u); return true; }) as any;
  store.notifyChat = (() => {}) as any;

  const wrapper = await mountPane(true);
  vi.advanceTimersByTime(BRIDGE_GRACE_MS + 1);
  await nextTick();

  store.browserNavRequest = { url: 'https://tokenrhythm.studio/account', path: '' };
  store.browserNavNonce += 1;
  await nextTick();

  expect(opened).toEqual(['https://tokenrhythm.studio/account']);
  // 不能拿空 path 去走源码预览（那只会弹一个读文件失败）
  expect(previewCalls).toEqual([]);
  wrapper.unmount();
});

it('5 点采样命中局部边缘遮挡浮层时立即 hide 原生表面', async () => {
  const api = mockApi();
  (window as any).pywebview = { api };
  useAppStore().embeddedBrowser.url = 'https://example.com';

  const wrapper = await mountPane(true);
  await vi.waitFor(() => expect(api.browser_pane_open).toHaveBeenCalled());

  const modalCorner = document.createElement('div');
  (document as any).elementFromPoint = vi.fn((x: number, y: number) => {
    // 中心点 (150, 200) 仍露出表面，但右下采样点被角上浮层遮挡
    if (x > 250 && y > 350) return modalCorner;
    return surfaceEl;
  });

  window.dispatchEvent(new Event('resize'));
  await vi.waitFor(() => expect(api.browser_pane_hide).toHaveBeenCalled());
  wrapper.unmount();
});

it('已知全局弹窗（confirmModal/filePreview）通过显式宿主隐藏门控立刻 hide 原生表面', async () => {
  const api = mockApi();
  (window as any).pywebview = { api };
  const store = useAppStore();
  store.embeddedBrowser.url = 'https://example.com';

  const wrapper = await mountPane(true);
  await vi.waitFor(() => expect(api.browser_pane_open).toHaveBeenCalled());

  store.confirmModal.show = true;
  await nextTick();
  await vi.waitFor(() => expect(api.browser_pane_hide).toHaveBeenCalled());

  store.confirmModal.show = false;
  await nextTick();
  await vi.waitFor(() => expect(api.browser_pane_open.mock.calls.length).toBeGreaterThanOrEqual(2));
  wrapper.unmount();
});

it('宿主串行代次门控：hide 后迟到 resolve 的 open ack 不得复活 ready 或重现原生表面', async () => {
  const api = mockApi();
  let resolveOpen: (v: unknown) => void = () => {};
  api.browser_pane_open.mockImplementation(
    () => new Promise((resolve) => { resolveOpen = resolve; }),
  );
  (window as any).pywebview = { api };
  const store = useAppStore();
  store.embeddedBrowser.url = 'https://example.com';

  const wrapper = await mountPane(true);
  await vi.waitFor(() => expect(api.browser_pane_open).toHaveBeenCalled());
  const openArgs = api.browser_pane_open.mock.calls[0];
  expect(openArgs.length).toBeGreaterThanOrEqual(5);
  const firstGen = openArgs[4];
  expect(typeof firstGen).toBe('number');

  // open 仍在路上就切走触发 hide
  await wrapper.setProps({ active: false });
  await vi.waitFor(() => expect(api.browser_pane_hide).toHaveBeenCalled());

  // 迟到 resolve 的旧 open 返回 ack 后，不得把相位改回 ready
  resolveOpen({ ok: true, ack: true, surface_generation: firstGen, phase: 'ready' });
  await nextTick();
  await nextTick();
  expect(wrapper.find('[data-test="bp-surface-phase"]').attributes('data-phase')).toBe('hidden');
  wrapper.unmount();
});

it('330px 窄面板遇 1440×900 视口时不篡改右栏宽度、隐藏被裁的原生视图并显示缩略图/展开提示，拖宽至 >=392px 后恢复', async () => {
  const api = mockApi();
  (window as any).pywebview = { api };
  const store = useAppStore();
  store.rightPanelWidth = 330;
  store.embeddedBrowser.url = 'https://example.com';
  store.embeddedBrowser.automationViewport = [1440, 900];
  store.computerRun = {
    ...store.computerRun,
    run_id: 'run-1',
    status: 'running',
    target: 'browser',
    browser: 'embedded',
    browser_mode: 'embedded',
    preview_url: '/computer-preview/tok/full.png',
  };

  let measuredWidth = 330;
  const cwDesc = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'clientWidth');
  const chDesc = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'clientHeight');
  Object.defineProperty(HTMLElement.prototype, 'clientWidth', { configurable: true, get: () => measuredWidth });
  Object.defineProperty(HTMLElement.prototype, 'clientHeight', { configurable: true, get: () => 600 });
  try {
    const wrapper = await mountPane(true, true);
    await nextTick();
    expect(store.rightPanelWidth).toBe(330);
    expect(wrapper.find('[data-test="bp-viewport-constrained"]').exists()).toBe(true);
    expect(wrapper.find('[data-test="bp-viewport-constrained"]').text()).toContain('392');
    expect(wrapper.get('[data-test="bp-viewport-thumb"]').attributes('src')).toBe('/computer-preview/tok/full.png');
    expect(api.browser_pane_open).not.toHaveBeenCalled();

    measuredWidth = 440;
    window.dispatchEvent(new Event('resize'));
    await nextTick();
    await nextTick();
    expect(wrapper.find('[data-test="bp-viewport-constrained"]').exists()).toBe(false);
    await vi.waitFor(() => expect(api.browser_pane_open).toHaveBeenCalled());
    wrapper.unmount();
  } finally {
    if (cwDesc) Object.defineProperty(HTMLElement.prototype, 'clientWidth', cwDesc);
    if (chDesc) Object.defineProperty(HTMLElement.prototype, 'clientHeight', chDesc);
  }
});

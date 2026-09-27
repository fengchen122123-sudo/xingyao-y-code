import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, expect, it } from 'vitest';
import { useAppStore } from './appStore';

beforeEach(() => setActivePinia(createPinia()));

it('embedded_browser 事件合并到 embeddedBrowser 状态并强制字段类型', () => {
  const store = useAppStore();
  expect(store.embeddedBrowser.url).toBe('');
  expect(store.embeddedBrowser.supported).toBe(true);

  store.handleBridgeEvent({
    type: 'embedded_browser',
    data: {
      url: 'https://example.com',
      title: 'Example',
      can_go_back: 1, // 后端契约是 bool，这里容忍真值并强转
      can_go_forward: false,
      loading: true,
      ready: true,
      visible: true,
      // 多余字段应被忽略
      extra: 'ignored',
    },
  } as any);

  expect(store.embeddedBrowser.url).toBe('https://example.com');
  expect(store.embeddedBrowser.title).toBe('Example');
  expect(store.embeddedBrowser.can_go_back).toBe(true);
  expect(store.embeddedBrowser.can_go_forward).toBe(false);
  expect(store.embeddedBrowser.loading).toBe(true);
  expect(store.embeddedBrowser.visible).toBe(true);

  // 缺省字段回落为空/假，不残留上一次的值
  store.handleBridgeEvent({ type: 'embedded_browser', data: { url: 'about:blank' } } as any);
  expect(store.embeddedBrowser.url).toBe('about:blank');
  expect(store.embeddedBrowser.title).toBe('');
  expect(store.embeddedBrowser.can_go_back).toBe(false);
  expect(store.embeddedBrowser.loading).toBe(false);
});

it('embedded_browser_automation 事件写入项目分辨率并展开面板到浏览器页签', () => {
  const store = useAppStore();
  store.rightPanelCollapsed = true;
  store.rightPanelPicker = true;
  expect(store.embeddedBrowser.automationViewport).toBeNull();

  store.handleBridgeEvent({ type: 'embedded_browser_automation', data: { width: 1280, height: 800 } } as any);
  expect(store.embeddedBrowser.automationViewport).toEqual([1280, 800]);
  expect(store.rightPanelCollapsed).toBe(false);
  expect(store.rightPanelPicker).toBe(false);

  // 非法/缺省分辨率：状态不变（不写入 NaN 视口）
  store.handleBridgeEvent({ type: 'embedded_browser_automation', data: {} } as any);
  expect(store.embeddedBrowser.automationViewport).toEqual([1280, 800]);
});

it('embedded_browser_automation 相同分辨率重复下发仍换新引用（可重复触发组件 watch）', () => {
  const store = useAppStore();
  store.handleBridgeEvent({ type: 'embedded_browser_automation', data: { width: 375, height: 667 } } as any);
  const first = store.embeddedBrowser.automationViewport;
  store.handleBridgeEvent({ type: 'embedded_browser_automation', data: { width: 375, height: 667 } } as any);
  const second = store.embeddedBrowser.automationViewport;
  expect(second).toEqual([375, 667]);
  expect(second).not.toBe(first);
});

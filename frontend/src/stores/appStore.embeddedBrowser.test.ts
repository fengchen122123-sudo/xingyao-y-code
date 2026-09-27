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

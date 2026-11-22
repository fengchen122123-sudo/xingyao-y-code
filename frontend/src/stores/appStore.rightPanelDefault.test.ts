import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, expect, it } from 'vitest';
import { useAppStore } from './appStore';

beforeEach(() => setActivePinia(createPinia()));

it('右侧面板默认收起，展开需手动触发', () => {
  const store = useAppStore();
  expect(store.rightPanelCollapsed).toBe(true);
  store.toggleRightPanel();
  expect(store.rightPanelCollapsed).toBe(false);
  store.toggleRightPanel();
  expect(store.rightPanelCollapsed).toBe(true);
});

it('中性展开先显示“打开标签页”选择页，定向开页签跳过；收起即离开', () => {
  const store = useAppStore();
  store.toggleRightPanel();
  expect(store.rightPanelPicker).toBe(true);

  store.openRightPanelTab('git');
  expect(store.rightPanelPicker).toBe(false);
  expect(store.rightPanelCollapsed).toBe(false);
  expect(store.rightPanelTabs).toEqual(['git']);
  expect(store.rightPanelActiveTab).toBe('git');

  store.toggleRightPanel();
  expect(store.rightPanelCollapsed).toBe(true);

  store.toggleRightPanel();
  expect(store.rightPanelCollapsed).toBe(false);
  expect(store.rightPanelPicker).toBe(false);
  expect(store.rightPanelActiveTab).toBe('git');
});

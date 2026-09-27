import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, expect, it } from 'vitest';
import { useAppStore } from './appStore';

// 右侧计划/任务/终端/文件/Git 栏默认收起：启动不摊开主对话区，
// 仅由顶栏“切换面板”按钮或底栏 Git 分支项手动展开（不持久化）。

beforeEach(() => setActivePinia(createPinia()));

it('右侧面板默认收起，展开需手动触发', () => {
  const store = useAppStore();
  expect(store.rightPanelCollapsed).toBe(true);
  store.toggleRightPanel();
  expect(store.rightPanelCollapsed).toBe(false);
  store.toggleRightPanel();
  expect(store.rightPanelCollapsed).toBe(true);
});

it('中性展开先显示“打开标签页”选择页，定向展开跳过；收起即离开', () => {
  const store = useAppStore();
  store.toggleRightPanel();
  expect(store.rightPanelPicker).toBe(true);

  // 定向入口（Git 分支项等）：直达页签不经选择页
  store.expandRightPanelDirect();
  expect(store.rightPanelPicker).toBe(false);
  expect(store.rightPanelCollapsed).toBe(false);

  store.toggleRightPanel();
  expect(store.rightPanelCollapsed).toBe(true);
  expect(store.rightPanelPicker).toBe(false);

  // 再次中性展开：重新先显示选择页
  store.toggleRightPanel();
  expect(store.rightPanelPicker).toBe(true);
});

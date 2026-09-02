import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, describe, expect, it } from 'vitest';

import {
  RIGHT_PANEL_WIDTH_KEY,
  SIDEBAR_WIDTH_KEY,
} from '../composables/usePanelResize';
import { useAppStore } from './appStore';

beforeEach(() => {
  localStorage.removeItem(SIDEBAR_WIDTH_KEY);
  localStorage.removeItem(RIGHT_PANEL_WIDTH_KEY);
  setActivePinia(createPinia());
});

describe('面板宽度状态', () => {
  it('无存储时用默认宽度：侧栏 240，右面板 330', () => {
    const store = useAppStore();
    expect(store.sidebarWidth).toBe(240);
    expect(store.rightPanelWidth).toBe(330);
  });

  it('启动时从 localStorage 恢复上次拖拽的宽度', () => {
    localStorage.setItem(SIDEBAR_WIDTH_KEY, '300');
    localStorage.setItem(RIGHT_PANEL_WIDTH_KEY, '400');
    const store = useAppStore();
    expect(store.sidebarWidth).toBe(300);
    expect(store.rightPanelWidth).toBe(400);
  });

  it('非法存储回默认，越界存储夹取到合法区间', () => {
    localStorage.setItem(SIDEBAR_WIDTH_KEY, '99999');
    localStorage.setItem(RIGHT_PANEL_WIDTH_KEY, 'abc');
    const store = useAppStore();
    expect(store.sidebarWidth).toBe(480);
    expect(store.rightPanelWidth).toBe(330);
  });

  it('侧栏最小宽度即默认宽度 240：低于 240 的存储值恢复时夹回 240', () => {
    // 侧栏内容（菜单/会话项）在 240px 以下会挤压错位，故收窄下限=默认值
    localStorage.setItem(SIDEBAR_WIDTH_KEY, '180');
    const store = useAppStore();
    expect(store.sidebarWidth).toBe(240);
  });
});

import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, describe, expect, it } from 'vitest';
import { mount } from '@vue/test-utils';
import { nextTick } from 'vue';

import App from './App.vue';
import { useAppStore } from './stores/appStore';
import { setChatLayoutWidth } from './composables/chatLayoutWidth';
import { RIGHT_PANEL_DEFAULT_WIDTH, RIGHT_PANEL_WIDTH_KEY } from './composables/usePanelResize';
import { pickWideDefaultTab, wideRightPanelDefaultWidth } from './composables/wideSplitPanel';

const stubs = {
  Topbar: true, Sidebar: true, KimiEmptyState: true, TurnGroup: true,
  InputArea: true, RightPanel: true, ConfirmModal: true, UpdateModal: true,
  PermCard: true, ImageLightbox: true, Subpage: true, SettingsPage: true,
  AppIcon: true, MessageNavRail: true, RemoteDirBrowser: true,
};

function setViewportWidth(width: number) {
  Object.defineProperty(window, 'innerWidth', { value: width, writable: true, configurable: true });
}

beforeEach(() => {
  localStorage.clear();
  setActivePinia(createPinia());
  delete (window as any).pywebview;
  setViewportWidth(1024);
  setChatLayoutWidth('wide');
});

describe('wideSplitPanel helpers', () => {
  it('宽屏按视口 28% 计算右面板缺省宽度并夹取到 [380, 680]', () => {
    expect(wideRightPanelDefaultWidth(1178)).toBe(RIGHT_PANEL_DEFAULT_WIDTH);
    expect(wideRightPanelDefaultWidth(1440)).toBe(403);
    expect(wideRightPanelDefaultWidth(1920)).toBe(538);
    expect(wideRightPanelDefaultWidth(2800)).toBe(680);
  });

  it('缺省页签优先级：后台任务 > 子代理 > 项目文件树 > 选择页', () => {
    expect(pickWideDefaultTab({ workdir: '/proj', bgTaskCount: 1, subagentTaskCount: 1 })).toBe('tasks');
    expect(pickWideDefaultTab({ workdir: '/proj', bgTaskCount: 0, subagentTaskCount: 2 })).toBe('subagent');
    expect(pickWideDefaultTab({ workdir: '/proj', bgTaskCount: 0, subagentTaskCount: 0 })).toBe('files');
    expect(pickWideDefaultTab({ workdir: 'GLOBAL', bgTaskCount: 0, subagentTaskCount: 0 })).toBeNull();
    expect(pickWideDefaultTab({ workdir: '', bgTaskCount: 0, subagentTaskCount: 0 })).toBeNull();
  });
});

describe('App 宽屏 / 最大化侧边联动分栏', () => {
  it('普通窗口收起；放大到宽屏（>=1440）自动展开右侧工作台并切到文件树，退回窄窗后自动收起', async () => {
    setViewportWidth(1178);
    const store = useAppStore();
    store.initialized = true;
    store.currentSessionName = 's1';
    store.workdir = 'D:/proj';

    const wrapper = mount(App, { global: { stubs } });
    await nextTick();
    expect(store.rightPanelCollapsed).toBe(true);
    expect(store.rightPanelWidth).toBe(RIGHT_PANEL_DEFAULT_WIDTH);

    // 模拟最大化到 1920px 宽屏
    setViewportWidth(1920);
    window.dispatchEvent(new Event('resize'));
    await nextTick();

    expect(store.rightPanelCollapsed).toBe(false);
    expect(store.rightPanelActiveTab).toBe('files');
    expect(store.rightPanelWidth).toBe(538);

    // 模拟还原回 1178px 普通窗口：联动打开的面板自动收起并还原默认宽度
    setViewportWidth(1178);
    window.dispatchEvent(new Event('resize'));
    await nextTick();

    expect(store.rightPanelCollapsed).toBe(true);
    expect(store.rightPanelWidth).toBe(RIGHT_PANEL_DEFAULT_WIDTH);
    wrapper.unmount();
  });

  it('用户在宽屏下手动收起右侧栏后不再强行弹回；重新进入宽屏时才恢复联动', async () => {
    setViewportWidth(1920);
    const store = useAppStore();
    store.initialized = true;
    store.currentSessionName = 's1';
    store.workdir = 'D:/proj';

    const wrapper = mount(App, { global: { stubs } });
    await nextTick();
    expect(store.rightPanelCollapsed).toBe(false);

    // 用户主动关闭右侧栏
    store.toggleRightPanel();
    await nextTick();
    expect(store.rightPanelCollapsed).toBe(true);

    // 切会话时尊重用户本次宽屏内的隐藏意图，不强行弹回
    store.currentSessionName = 's2';
    await nextTick();
    expect(store.rightPanelCollapsed).toBe(true);

    // 退出宽屏再重新最大化，联动机制重新生效
    setViewportWidth(1178);
    window.dispatchEvent(new Event('resize'));
    await nextTick();
    setViewportWidth(1920);
    window.dispatchEvent(new Event('resize'));
    await nextTick();
    expect(store.rightPanelCollapsed).toBe(false);
    wrapper.unmount();
  });

  it('用户曾自定义右面板宽度时，宽屏联动保留自定义比例不被覆写', async () => {
    localStorage.setItem(RIGHT_PANEL_WIDTH_KEY, '0.35');
    setViewportWidth(1920);
    const store = useAppStore();
    store.initialized = true;
    store.rightPanelWidth = 672;
    store.workdir = 'D:/proj';

    const wrapper = mount(App, { global: { stubs } });
    await nextTick();

    expect(store.rightPanelCollapsed).toBe(false);
    expect(store.rightPanelWidth).toBe(672);
    wrapper.unmount();
  });

  it('切到 compact / full 模式时不触发自动展开，并收起先前联动打开的右侧栏', async () => {
    setViewportWidth(1920);
    const store = useAppStore();
    store.initialized = true;
    store.workdir = 'D:/proj';

    const wrapper = mount(App, { global: { stubs } });
    await nextTick();
    expect(store.rightPanelCollapsed).toBe(false);

    setChatLayoutWidth('compact');
    await nextTick();
    expect(store.rightPanelCollapsed).toBe(true);

    setChatLayoutWidth('wide');
    await nextTick();
    expect(store.rightPanelCollapsed).toBe(false);

    setChatLayoutWidth('full');
    await nextTick();
    expect(store.rightPanelCollapsed).toBe(true);
    wrapper.unmount();
  });
});

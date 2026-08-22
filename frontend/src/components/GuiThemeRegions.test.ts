/// <reference types="node" />
import { beforeEach, describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { mount } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';

import App from '../App.vue';
import { useAppStore } from '../stores/appStore';

// jsdom 下 import.meta.url 非 file 协议且 CSS 的 ?raw 导入会被置空，
// 源码断言改从 vitest 根目录（frontend/）直接读文件。
const runtimeSource = readFileSync(resolve(process.cwd(), 'src/themes/runtime.ts'), 'utf-8');
const styleSource = readFileSync(resolve(process.cwd(), 'src/style.css'), 'utf-8');

const REGION_IDS = [
  'app_header', 'sidebar', 'conversation_toolbar', 'workspace',
  'composer', 'right_panel', 'statusbar',
] as const;

function piniaWithInitializedStore() {
  const pinia = createPinia();
  setActivePinia(pinia);
  const store = useAppStore();
  store.initialized = true;
  store.guiThemeError = null;
  return pinia;
}

// App 的重子组件（Topbar/Sidebar/InputArea/RightPanel）保持真实挂载，
// 只桩掉与主题无关的深依赖，确保七个分区标记来自真实 DOM 根节点。
const shallowStubs = {
  TurnGroup: true,
  KimiEmptyState: true,
  ConfirmModal: true,
  UpdateModal: true,
  PermCard: true,
  ImageLightbox: true,
  Subpage: true,
  SettingsPage: true,
  AppIcon: true,
};

beforeEach(() => {
  delete (window as any).pywebview;
});

describe('GUI theme regions', () => {
  it('marks all public theme regions with stable IDs', () => {
    piniaWithInitializedStore();
    const store = useAppStore();
    store.messages = [{ role: 'user', content: 'hello' }];

    const wrapper = mount(App, { global: { stubs: shallowStubs } });

    const ids = wrapper.findAll('[data-theme-region]').map(node => node.attributes('data-theme-region'));
    expect(new Set(ids)).toEqual(new Set(REGION_IDS));

    wrapper.unmount();
  });

  it('keeps region markers on the existing layout roots', () => {
    piniaWithInitializedStore();
    const store = useAppStore();
    store.messages = [{ role: 'user', content: 'hello' }];

    const wrapper = mount(App, { global: { stubs: shallowStubs } });

    const markers: Record<string, string> = {};
    for (const node of wrapper.findAll('[data-theme-region]')) {
      markers[node.attributes('data-theme-region') as string] = node.attributes('id') || '';
    }
    // 现有根节点直接打标，不引入额外包装层
    expect(markers).toEqual({
      app_header: 'topbar',
      sidebar: 'sidebar',
      conversation_toolbar: 'chat-header',
      workspace: 'chat-scroll',
      composer: 'input-area',
      right_panel: 'rightbar',
      statusbar: 'statusbar',
    });

    wrapper.unmount();
  });
});

describe('GUI theme wiring sources', () => {

  it('runtime never injects style elements or user HTML', () => {
    expect(runtimeSource).not.toMatch(/innerHTML/);
    expect(runtimeSource).not.toMatch(/insertAdjacentHTML/);
    expect(runtimeSource).not.toMatch(/data-user-theme/);
    expect(runtimeSource).not.toMatch(/outerHTML/);
  });

  it('style.css aliases every region to the unified region variables', () => {
    for (const region of REGION_IDS) {
      expect(styleSource).toContain(`[data-theme-region="${region}"]`);
    }
    expect(styleSource).toContain('--theme-region-bg:');
    expect(styleSource).toContain('color-mix(in srgb, var(--theme-region-bg)');
    expect(styleSource).toMatch(/--bg0:\s*var\(--theme-background-app\)/);
    expect(styleSource).toMatch(/--bg1:\s*var\(--theme-background-panel\)/);
    // 背景图使用不可交互伪元素，位于内容后方
    expect(styleSource).toMatch(/\[data-theme-region\]::before/);
    expect(styleSource).toMatch(/pointer-events:\s*none/);
  });

  it('runtime toggles the appearance class on the document root as well', () => {
    // :root.light 是无主题启动时的浅色语义默认值来源；
    // 运行时必须同步 html 上的 light 类，主题变量才能覆盖它。
    expect(runtimeSource).toMatch(/documentElement\.classList\.toggle\('light'/);
    expect(runtimeSource).toMatch(/ownerDocument\.documentElement/);
  });
});

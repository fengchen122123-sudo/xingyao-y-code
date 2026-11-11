import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, expect, it } from 'vitest';
import { mount } from '@vue/test-utils';

import App from './App.vue';
import { useAppStore } from './stores/appStore';

// 子组件全部 stub：只断言底部状态栏「技能」项的显示文本
const stubs = {
  Topbar: true, Sidebar: true, KimiEmptyState: true, TurnGroup: true,
  InputArea: true, RightPanel: true, ConfirmModal: true, UpdateModal: true,
  PermCard: true, ImageLightbox: true, Subpage: true, SettingsPage: true,
  AppIcon: true, MessageNavRail: true, RemoteDirBrowser: true,
};

beforeEach(() => {
  localStorage.clear();
  setActivePinia(createPinia());
  delete (window as any).pywebview;
});

function mountWithSkills(usedSkills: string[]) {
  const store = useAppStore();
  store.initialized = true;
  store.usedSkills = usedSkills;
  const wrapper = mount(App, { global: { stubs } });
  return { store, wrapper };
}

it('未使用技能时状态栏技能项显示「无」', () => {
  const { wrapper } = mountWithSkills([]);
  const el = wrapper.find('[data-test="status-skill"]');
  expect(el.text()).toBe('技能: 无');
  expect(el.attributes('title')).toBe('本回合未使用任何技能');
  wrapper.unmount();
});

it('技能较少时状态栏完整显示全部技能名、不截断', () => {
  const { wrapper } = mountWithSkills(['brainstorming', 'writing-plans']);
  const el = wrapper.find('[data-test="status-skill"]');
  expect(el.text()).toBe('技能: brainstorming、writing-plans');
  wrapper.unmount();
});

it('技能较多时截断并以省略号结尾，完整列表保留在 title', () => {
  const skills = [
    'systematic-debugging',
    'test-driven-development',
    'verification-before-completion',
  ];
  const { wrapper } = mountWithSkills(skills);
  const el = wrapper.find('[data-test="status-skill"]');
  // 前两个放得下、第三个超出预算：截断显示并以 "…" 结尾，不允许把全部名称堆进状态栏
  expect(el.text()).toBe('技能: systematic-debugging、test-driven-development…');
  // 完整列表通过 title 保留，hover 可查
  expect(el.attributes('title')).toBe('本回合使用的技能：' + skills.join('、'));
  wrapper.unmount();
});
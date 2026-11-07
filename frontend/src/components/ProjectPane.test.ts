import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, expect, it, vi } from 'vitest';
import { mount } from '@vue/test-utils';

import ProjectPane from './ProjectPane.vue';
import { useAppStore } from '../stores/appStore';

// 后端契约（core.project_profile.read_state）：sections 恒含三节，缺的为空串
const WORKDIR = 'D:/demo/长夜书';

function profilePayload(overrides: Record<string, unknown> = {}) {
  return {
    ok: true,
    workdir: WORKDIR,
    exists: true,
    updated: '2026-11-06 10:00:00',
    sections: {
      技术栈: '- Python 3.11 + PyQt5',
      架构决策: '- 前端 Vue 3 + Pinia',
      硬约束: '- 不得改动 gui_bridge 桥签名',
    },
    ...overrides,
  };
}

function mountPane(impl: (...args: any[]) => any, extraApi: Record<string, any> = {}) {
  const getProfile = vi.fn(impl);
  (window as any).pywebview = {
    api: {
      get_project_profile: getProfile,
      save_project_profile: vi.fn(async (_workdir: string, section: string, content: string) =>
        profilePayload({
          sections: { ...profilePayload().sections, [section]: content },
        })),
      ...extraApi,
    },
  };
  const pinia = createPinia();
  setActivePinia(pinia);
  const store = useAppStore();
  store.workdir = WORKDIR;
  const wrapper = mount(ProjectPane, {
    props: { active: true },
    global: { plugins: [pinia] },
  });
  return { wrapper, getProfile };
}

beforeEach(() => {
  setActivePinia(createPinia());
  delete (window as any).pywebview;
});

it('正常态：渲染三节内容与更新时间', async () => {
  const { wrapper, getProfile } = mountPane(async () => profilePayload());

  await vi.waitFor(() => {
    expect(wrapper.findAll('[data-test="project-section"]')).toHaveLength(3);
  });

  expect(getProfile).toHaveBeenCalledWith(WORKDIR);
  const cards = wrapper.findAll('[data-test="project-section"]');
  expect(cards[0].text()).toContain('技术栈');
  expect(cards[0].text()).toContain('Python 3.11 + PyQt5');
  expect(cards[2].text()).toContain('不得改动 gui_bridge 桥签名');
  expect(wrapper.get('[data-test="project-updated"]').text()).toContain('2026-11-06');
  wrapper.unmount();
});

it('空态：没有画像时给出引导，点「开始记录」进入编辑', async () => {
  const { wrapper } = mountPane(async () => ({
    ok: true,
    exists: false,
    updated: '',
    sections: { 技术栈: '', 架构决策: '', 硬约束: '' },
  }));

  await vi.waitFor(() => {
    expect(wrapper.find('[data-test="project-empty"]').exists()).toBe(true);
  });
  await wrapper.get('[data-test="project-create"]').trigger('click');
  expect(wrapper.findAll('[data-test="project-editor"]')).toHaveLength(1);
  wrapper.unmount();
});

it('编辑保存：整节覆盖后调用桥并更新展示', async () => {
  const { wrapper } = mountPane(async () => profilePayload());

  await vi.waitFor(() => {
    expect(wrapper.findAll('[data-test="project-section"]')).toHaveLength(3);
  });

  const first = wrapper.findAll('[data-test="project-section"]')[0];
  await first.get('[data-test="project-edit"]').trigger('click');
  const editor = first.get('[data-test="project-editor"]');
  await editor.setValue('- Node 20 + Vite');
  await first.get('[data-test="project-save"]').trigger('click');

  await vi.waitFor(() => {
    expect(wrapper.get('[data-test="project-hint"]').text()).toContain('已保存');
  });
  const api = (window as any).pywebview.api;
  expect(api.save_project_profile).toHaveBeenCalledWith(WORKDIR, '技术栈', '- Node 20 + Vite');
  expect(wrapper.findAll('[data-test="project-section"]')[0].text()).toContain('Node 20 + Vite');
  wrapper.unmount();
});

it('错误态：后端回 error 时显示错误文本与重试按钮', async () => {
  const { wrapper } = mountPane(async () => ({ ok: false, error: '项目文件夹不存在: D:/demo/x' }));

  await vi.waitFor(() => {
    expect(wrapper.find('[data-test="project-error"]').exists()).toBe(true);
  });
  expect(wrapper.get('[data-test="project-error"]').text()).toContain('项目文件夹不存在');
  expect(wrapper.find('[data-test="project-retry"]').exists()).toBe(true);
  wrapper.unmount();
});

it('没有项目目录（GLOBAL）：显示空态引导且不调桥', async () => {
  const getProfile = vi.fn();
  (window as any).pywebview = { api: { get_project_profile: getProfile } };
  const pinia = createPinia();
  setActivePinia(pinia);
  const store = useAppStore();
  store.workdir = 'GLOBAL';
  const wrapper = mount(ProjectPane, {
    props: { active: true },
    global: { plugins: [pinia] },
  });

  await vi.waitFor(() => {
    expect(wrapper.find('[data-test="project-empty"]').exists()).toBe(true);
  });
  expect(getProfile).not.toHaveBeenCalled();
  expect(wrapper.text()).toContain('还没有进入项目目录');
  wrapper.unmount();
});
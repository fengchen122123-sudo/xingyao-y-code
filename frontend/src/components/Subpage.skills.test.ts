import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushPromises, mount } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';

import Subpage from './Subpage.vue';
import { useAppStore } from '../stores/appStore';

function mountSubpage(subType: 'skills' | 'mcp' | 'plugins' | 'models' = 'skills', api: Record<string, unknown> = {}) {
  (window as any).pywebview = { api };
  return mount(Subpage, {
    props: {
      visible: true,
      subType,
    },
  });
}

describe('Subpage - Skills Refresh', () => {
  beforeEach(() => {
    setActivePinia(createPinia());
  });

  afterEach(() => {
    delete (window as any).pywebview;
  });

  it('renders refresh button on skills subpage', () => {
    const wrapper = mountSubpage('skills');
    const refreshBtn = wrapper.find('[data-test="refresh-skills-btn"]');
    expect(refreshBtn.exists()).toBe(true);
    expect(refreshBtn.text()).toContain('刷新技能');
  });

  it('calls store.refreshSkills and updates skill list when clicked', async () => {
    const store = useAppStore();
    store.skills = [
      { id: 'skill-old', name: '旧技能', description: '', enabled: true, source: 'user' },
    ];

    const newSkills = [
      { id: 'skill-old', name: '旧技能', description: '', enabled: true, source: 'user' },
      { id: 'skill-new', name: '新扫描到的技能', description: '', enabled: false, source: 'builtin' },
    ];

    const refreshMock = vi.fn().mockResolvedValue({
      ok: true,
      skills: newSkills,
    });

    const wrapper = mountSubpage('skills', { refresh_skills: refreshMock });
    const refreshBtn = wrapper.get('[data-test="refresh-skills-btn"]');

    await refreshBtn.trigger('click');
    await flushPromises();

    expect(refreshMock).toHaveBeenCalledTimes(1);
    expect(store.skills).toEqual(newSkills);
  });
});

describe('Subpage - 来源筛选（全部/内置/用户）', () => {
  beforeEach(() => {
    setActivePinia(createPinia());
  });

  afterEach(() => {
    delete (window as any).pywebview;
  });

  function visibleNames(wrapper: ReturnType<typeof mount>): string[] {
    return wrapper.findAll('.sub-card .sc-name').map(n => n.text());
  }

  it('技能页默认显示全部，可按内置/用户筛选', async () => {
    const store = useAppStore();
    store.skills = [
      { id: 's1', name: '内置技能A', description: '', enabled: true, source: 'builtin' },
      { id: 's2', name: '用户技能B', description: '', enabled: true, source: 'user' },
      { id: 's3', name: '旧技能C', description: '', enabled: false }, // 无 source 按用户算
    ];
    const wrapper = mountSubpage('skills');

    expect(wrapper.findAll('[data-test="source-filter"] .seg-btn')).toHaveLength(3);
    expect(visibleNames(wrapper)).toHaveLength(3);

    await wrapper.get('[data-test="src-filter-builtin"]').trigger('click');
    expect(visibleNames(wrapper)).toEqual(['内置技能A 内置']);

    await wrapper.get('[data-test="src-filter-user"]').trigger('click');
    const userNames = visibleNames(wrapper).join('|');
    expect(userNames).toContain('用户技能B');
    expect(userNames).toContain('旧技能C');
    expect(userNames).not.toContain('内置技能A');

    await wrapper.get('[data-test="src-filter-all"]').trigger('click');
    expect(visibleNames(wrapper)).toHaveLength(3);
  });

  it('技能页筛选无结果时显示空态提示', async () => {
    const store = useAppStore();
    store.skills = [
      { id: 's1', name: '只有内置', description: '', enabled: true, source: 'builtin' },
    ];
    const wrapper = mountSubpage('skills');

    await wrapper.get('[data-test="src-filter-user"]').trigger('click');
    expect(wrapper.findAll('.sub-card')).toHaveLength(0);
    expect(wrapper.text()).toContain('没有符合筛选条件的技能');
  });

  it('插件页可按内置/用户筛选，非 user 来源按内置算', async () => {
    const store = useAppStore();
    store.plugins = [
      { name: 'builtin-one', version: '1.0', description: '', source: 'builtin', status: 'loaded', enabled: true, tools: [] },
      { name: 'user-one', version: '1.0', description: '', source: 'user', status: 'loaded', enabled: true, tools: [] },
    ];
    const wrapper = mountSubpage('plugins');

    expect(wrapper.findAll('[data-test="source-filter"] .seg-btn')).toHaveLength(3);
    expect(visibleNames(wrapper)).toHaveLength(2);

    await wrapper.get('[data-test="src-filter-builtin"]').trigger('click');
    expect(visibleNames(wrapper).join('|')).toContain('builtin-one');
    expect(visibleNames(wrapper).join('|')).not.toContain('user-one');

    await wrapper.get('[data-test="src-filter-user"]').trigger('click');
    expect(visibleNames(wrapper).join('|')).toContain('user-one');
    expect(visibleNames(wrapper).join('|')).not.toContain('builtin-one');
  });

  it('切换子页时筛选重置为全部', async () => {
    const store = useAppStore();
    store.skills = [
      { id: 's1', name: '内置技能A', description: '', enabled: true, source: 'builtin' },
    ];
    const wrapper = mountSubpage('skills');
    await wrapper.get('[data-test="src-filter-builtin"]').trigger('click');
    expect(visibleNames(wrapper)).toHaveLength(1);

    await wrapper.setProps({ subType: 'plugins' });
    store.plugins = [
      { name: 'user-one', version: '1.0', description: '', source: 'user', status: 'loaded', enabled: true, tools: [] },
    ];
    await flushPromises();
    expect(visibleNames(wrapper)).toHaveLength(1);
    expect(wrapper.get('[data-test="src-filter-all"]').classes()).toContain('active');
  });
});

describe('Subpage - 恢复内置版本', () => {
  beforeEach(() => {
    setActivePinia(createPinia());
  });

  afterEach(() => {
    delete (window as any).pywebview;
  });

  it('仅对已偏离内置版本（from_builtin 且 source 非 builtin）的技能显示恢复按钮', () => {
    const store = useAppStore();
    store.skills = [
      // 被用户改过：曾由内置释放但当前 source 非 builtin → 显示恢复入口
      { id: 'drifted', name: '被改过的内置技能', description: '', enabled: true, source: 'user', from_builtin: true },
      // 仍是内置：自身即内置源，无需恢复 → 不显示
      { id: 'pure', name: '纯内置技能', description: '', enabled: true, source: 'builtin', from_builtin: true },
      // 纯用户技能：并非内置释放 → 不显示
      { id: 'user-skill', name: '用户技能', description: '', enabled: true, source: 'user' },
    ];
    const wrapper = mountSubpage('skills');

    expect(wrapper.findAll('[data-test="restore-skill-btn"]')).toHaveLength(1);
    const cards = wrapper.findAll('.sub-card');
    expect(cards[0].find('[data-test="restore-skill-btn"]').exists()).toBe(true);
    expect(cards[1].find('[data-test="restore-skill-btn"]').exists()).toBe(false);
    expect(cards[2].find('[data-test="restore-skill-btn"]').exists()).toBe(false);
  });

  it('点击恢复按钮经二次确认后调用 restore_skill 并更新技能列表', async () => {
    const store = useAppStore();
    store.skills = [
      { id: 'drifted', name: '被改过的内置技能', description: '', enabled: true, source: 'user', from_builtin: true },
    ];
    const restored = [
      { id: 'drifted', name: '被改过的内置技能', description: '', enabled: true, source: 'builtin', from_builtin: true },
    ];
    const restoreMock = vi.fn().mockResolvedValue({ ok: true, skills: restored });
    const askConfirm = vi.spyOn(store, 'askConfirm').mockResolvedValue(true);

    const wrapper = mountSubpage('skills', { restore_skill: restoreMock });
    await wrapper.get('[data-test="restore-skill-btn"]').trigger('click');
    await flushPromises();

    // 破坏性操作必须先二次确认（复用项目统一的 askConfirm），确认后才调用后端
    expect(askConfirm).toHaveBeenCalledTimes(1);
    expect(restoreMock).toHaveBeenCalledWith('drifted');
    expect(store.skills).toEqual(restored);
  });

  it('二次确认取消时不调用 restore_skill', async () => {
    const store = useAppStore();
    store.skills = [
      { id: 'drifted', name: '被改过的内置技能', description: '', enabled: true, source: 'user', from_builtin: true },
    ];
    const restoreMock = vi.fn().mockResolvedValue({ ok: true, skills: [] });
    vi.spyOn(store, 'askConfirm').mockResolvedValue(false);

    const wrapper = mountSubpage('skills', { restore_skill: restoreMock });
    await wrapper.get('[data-test="restore-skill-btn"]').trigger('click');
    await flushPromises();

    expect(restoreMock).not.toHaveBeenCalled();
  });
});

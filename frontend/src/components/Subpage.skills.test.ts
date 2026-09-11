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

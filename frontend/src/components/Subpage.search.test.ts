import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { mount } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';

import Subpage from './Subpage.vue';
import { useAppStore } from '../stores/appStore';

function mountSubpage(subType: 'skills' | 'mcp' | 'plugins' | 'models' = 'skills') {
  (window as any).pywebview = { api: {} };
  return mount(Subpage, { props: { visible: true, subType } });
}

function cardNames(wrapper: ReturnType<typeof mount>): string[] {
  return wrapper.findAll('.sub-card .sc-name').map(n => n.text());
}

async function typeSearch(wrapper: ReturnType<typeof mount>, kw: string) {
  await wrapper.get('[data-test="sub-search-input"]').setValue(kw);
}

describe('Subpage - 关键词搜索', () => {
  beforeEach(() => {
    setActivePinia(createPinia());
  });

  afterEach(() => {
    delete (window as any).pywebview;
  });

  it('技能页按名称与介绍过滤，多关键词需全部命中且忽略大小写', async () => {
    const store = useAppStore();
    store.skills = [
      { id: 'cover', name: '封面图生成', description: '生成文章封面图', enabled: true, source: 'builtin' },
      { id: 'story', name: '网文写作', description: '长篇与短篇写作助手', enabled: true, source: 'builtin' },
      { id: 'video', name: '讲解视频', description: '把文章做成带旁白的视频', enabled: true, source: 'user' },
    ];
    const wrapper = mountSubpage('skills');
    expect(cardNames(wrapper)).toHaveLength(3);

    await typeSearch(wrapper, '网文'); // 名称命中
    expect(cardNames(wrapper).join('|')).toContain('网文写作');

    await typeSearch(wrapper, 'COVER'); // 介绍命中且大小写不敏感
    expect(cardNames(wrapper).join('|')).toContain('封面图生成');

    await typeSearch(wrapper, '文章 视频'); // 两个词须同时命中
    const hit = cardNames(wrapper).join('|');
    expect(hit).toContain('讲解视频');
    expect(hit).not.toContain('网文写作');
    expect(hit).not.toContain('封面图生成');

    await typeSearch(wrapper, ''); // 清空恢复全量
    expect(cardNames(wrapper)).toHaveLength(3);
  });

  it('技能页搜索与来源筛选叠加，无结果时给出空态', async () => {
    const store = useAppStore();
    store.skills = [
      { id: 'video', name: '讲解视频', description: '', enabled: true, source: 'user' },
      { id: 'cover', name: '封面图生成', description: '', enabled: true, source: 'builtin' },
    ];
    const wrapper = mountSubpage('skills');

    await typeSearch(wrapper, '视频');
    expect(cardNames(wrapper).join('|')).toContain('讲解视频');

    await wrapper.get('[data-test="src-filter-builtin"]').trigger('click');
    expect(wrapper.findAll('.sub-card')).toHaveLength(0);
    expect(wrapper.text()).toContain('没有符合筛选条件的技能');
  });

  it('插件页按名称、介绍与工具名过滤', async () => {
    const store = useAppStore();
    store.plugins = [
      { name: 'tokenrhythm-image', version: '1.0', description: '基元律动生图插件', source: 'user', status: 'loaded', enabled: true, tools: ['tokenrhythm_generate_image'] },
      { name: 'workbuddy_connect', version: '1.0', description: 'WorkBuddy 接入', source: 'builtin', status: 'loaded', enabled: true, tools: ['workbuddy_status'] },
    ];
    const wrapper = mountSubpage('plugins');

    await typeSearch(wrapper, '生图');
    expect(cardNames(wrapper).join('|')).toContain('tokenrhythm-image');

    await typeSearch(wrapper, 'workbuddy_status'); // 工具名命中
    expect(cardNames(wrapper).join('|')).toContain('workbuddy_connect');
  });

  it('MCP 页按服务名过滤，无匹配时显示空态', async () => {
    const store = useAppStore();
    store.mcps = [
      { name: 'github', enabled: true, connected: true, tools_count: 3 },
      { name: 'filesystem', enabled: false, connected: false, tools_count: 0 },
    ];
    const wrapper = mountSubpage('mcp');
    expect(wrapper.findAll('.sub-card')).toHaveLength(2);

    await typeSearch(wrapper, 'github');
    expect(cardNames(wrapper).join('|')).toContain('github');
    expect(cardNames(wrapper).join('|')).not.toContain('filesystem');

    await typeSearch(wrapper, 'zzz');
    expect(wrapper.findAll('.sub-card')).toHaveLength(0);
    expect(wrapper.text()).toContain('没有匹配的 MCP 服务');
  });

  it('切换子页时清空关键词并恢复完整列表', async () => {
    const store = useAppStore();
    store.skills = [
      { id: 's1', name: '技能A', description: '', enabled: true, source: 'user' },
    ];
    const wrapper = mountSubpage('skills');
    await typeSearch(wrapper, '不存在的词');
    expect(wrapper.findAll('.sub-card')).toHaveLength(0);

    store.plugins = [
      { name: 'p1', version: '1.0', description: '', source: 'user', status: 'loaded', enabled: true, tools: [] },
    ];
    await wrapper.setProps({ subType: 'plugins' });
    const input = wrapper.get('[data-test="sub-search-input"]').element as HTMLInputElement;
    expect(input.value).toBe('');
    expect(wrapper.findAll('.sub-card')).toHaveLength(1);
  });
});

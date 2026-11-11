import { mount } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import { expect, it, vi } from 'vitest';
import { nextTick } from 'vue';
import Sidebar from './Sidebar.vue';
import SessionSearchModal from './SessionSearchModal.vue';
import { useAppStore } from '../stores/appStore';

it('点击侧边栏搜索按钮打开会话搜索弹窗', async () => {
  const pinia = createPinia();
  setActivePinia(pinia);
  const wrapper = mount(Sidebar, { global: { plugins: [pinia] } });
  const store = useAppStore();

  expect(store.sessionSearchModal.show).toBe(false);
  const btn = wrapper.find('[data-test="btn-search-sessions"]');
  expect(btn.exists()).toBe(true);
  await btn.trigger('click');

  expect(store.sessionSearchModal.show).toBe(true);
});

it('搜索弹窗输入关键词并检索会话与展示高亮切片', async () => {
  const search_sessions_full = vi.fn(async (_query: string, _scope: string) => ({
    ok: true,
    total: 2,
    results: [
      {
        type: 'title',
        session_name: 'session_api_router',
        display_name: '重构 API 路由',
        workdir: 'E:\\demo',
        workdir_label: 'demo',
        updated_at: '2026-09-17 19:30:00',
        msg_count: 12,
        role: null,
        snippet: null,
      },
      {
        type: 'content',
        session_name: 'session_chat',
        display_name: '常规咨询',
        workdir: 'GLOBAL',
        workdir_label: '自由对话',
        updated_at: '2026-09-17 18:00:00',
        msg_count: 5,
        role: 'user',
        snippet: '...请问这个 API 应该怎么调用...',
      },
    ],
  }));

  const switch_session = vi.fn(async () => ({ ok: true }));

  (window as any).pywebview = {
    api: {
      search_sessions_full,
      switch_session,
      get_sessions: vi.fn(async () => []),
      get_message_queue: vi.fn(async () => []),
      get_workspace_state: vi.fn(async () => ({ ok: true })),
    },
  };

  const pinia = createPinia();
  setActivePinia(pinia);
  const store = useAppStore();
  store.openSessionSearchModal();

  const wrapper = mount(SessionSearchModal, { global: { plugins: [pinia] } });

  expect(wrapper.find('[data-test="session-search-modal"]').exists()).toBe(true);

  const input = wrapper.find('[data-test="session-search-input"]');
  expect(input.exists()).toBe(true);

  // 输入关键词
  await input.setValue('API');
  // 等待 debounce
  await new Promise((r) => setTimeout(r, 200));
  await nextTick();

  expect(search_sessions_full).toHaveBeenCalledWith('API', 'all', undefined, 30);

  // 检查渲染出来的条目
  const items = wrapper.findAll('.result-item');
  expect(items.length).toBe(2);

  // 第一条：标题匹配
  expect(items[0].find('.type-badge').text()).toBe('标题匹配');
  expect(items[0].html()).toContain('<mark>API</mark>');

  // 第二条：内容匹配
  expect(items[1].find('.type-badge').text()).toBe('用户提问');
  expect(items[1].find('.item-snippet').html()).toContain('<mark>API</mark>');

  // 点击第一条，触发跳转
  await items[0].trigger('click');
  expect(store.sessionSearchModal.show).toBe(false);
});

it('「包含已归档」开关默认关闭，开启后检索请求才带上 include_archived', async () => {
  const search_sessions_full = vi.fn(async () => ({ ok: true, total: 0, results: [] }));

  (window as any).pywebview = {
    api: { search_sessions_full, switch_session: vi.fn(async () => ({ ok: true })) },
  };

  const pinia = createPinia();
  setActivePinia(pinia);
  const store = useAppStore();
  store.openSessionSearchModal();
  const wrapper = mount(SessionSearchModal, { global: { plugins: [pinia] } });

  const toggle = wrapper.get('[data-test="search-include-archived"]');
  expect((toggle.element as HTMLInputElement).checked).toBe(false);

  await wrapper.get('[data-test="session-search-input"]').setValue('归档');
  await new Promise((r) => setTimeout(r, 200));
  await nextTick();
  // 默认关闭：保持原有 4 参签名不变（旧后端不接受第 5 参）
  expect(search_sessions_full).toHaveBeenLastCalledWith('归档', 'all', undefined, 30);

  await toggle.setValue(true);
  await new Promise((r) => setTimeout(r, 0));
  expect(search_sessions_full).toHaveBeenLastCalledWith('归档', 'all', undefined, 30, true);
});

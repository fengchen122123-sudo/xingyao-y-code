import { mount } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, expect, it } from 'vitest';
import Sidebar from './Sidebar.vue';
import { useAppStore } from '../stores/appStore';

const COLLAPSED_KEY = 'ycode-sidebar-collapsed-groups';
const EXPANDED_KEY = 'ycode-sidebar-expanded-groups';

function makeGroup(path: string, count = 1) {
  return {
    folder_path: path,
    folder_name: path.split('/').pop(),
    is_global: false,
    is_current: false,
    sessions: Array.from({ length: count }, (_, i) => ({
      name: `${path}-s${i}`,
      display_name: `会话 ${i}`,
      updated_at: '',
    })),
  };
}

function mountSidebar(groups: any[]) {
  const pinia = createPinia();
  setActivePinia(pinia);
  const store = useAppStore();
  store.sessionGroups = groups as any;
  return mount(Sidebar, { global: { plugins: [pinia] } });
}

/** 分组会话区是否被 v-show 隐藏（jsdom 无布局引擎，直接断言 inline display 更可靠） */
function groupHidden(wrapper: any) {
  return (wrapper.get('.group-sessions').attributes('style') || '').includes('display: none');
}

beforeEach(() => {
  localStorage.clear();
  delete (window as any).pywebview;
});

it('折叠分组后写入 localStorage，重新挂载仍保持折叠', async () => {
  const group = makeGroup('D:/proj/a');
  const w1 = mountSidebar([group]);

  await w1.get('.group-header').trigger('click');
  expect(groupHidden(w1)).toBe(true);
  expect(localStorage.getItem(COLLAPSED_KEY)).toContain('D:/proj/a');
  w1.unmount();

  // 重新挂载（等价于切换页面/重启前端）后应恢复折叠态
  const w2 = mountSidebar([group]);
  expect(groupHidden(w2)).toBe(true);
  expect(w2.get('.g-arrow').text()).toBe('▸');
  w2.unmount();
});

it('再次展开分组后清除存储，不留残留', async () => {
  const group = makeGroup('D:/proj/a');
  localStorage.setItem(COLLAPSED_KEY, JSON.stringify(['D:/proj/a']));
  const w1 = mountSidebar([group]);
  expect(groupHidden(w1)).toBe(true);

  await w1.get('.group-header').trigger('click');

  expect(groupHidden(w1)).toBe(false);
  expect(localStorage.getItem(COLLAPSED_KEY)).toBeNull();
  w1.unmount();

  const w2 = mountSidebar([group]);
  expect(groupHidden(w2)).toBe(false);
  w2.unmount();
});

it('存储内容损坏时按全部展开处理，不抛错', async () => {
  localStorage.setItem(COLLAPSED_KEY, '{不是合法 JSON');
  const wrapper = mountSidebar([makeGroup('D:/proj/a')]);
  expect(groupHidden(wrapper)).toBe(false);
  wrapper.unmount();
});

it('「显示更多」的展开状态同样持久化', async () => {
  const group = makeGroup('D:/proj/a', 6);
  const w1 = mountSidebar([group]);
  expect(w1.findAll('.session-item').length).toBe(5);

  await w1.get('.more-toggle').trigger('click');
  expect(w1.findAll('.session-item').length).toBe(6);
  expect(localStorage.getItem(EXPANDED_KEY)).toContain('D:/proj/a');
  w1.unmount();

  const w2 = mountSidebar([group]);
  expect(w2.findAll('.session-item').length).toBe(6);
  w2.unmount();
});

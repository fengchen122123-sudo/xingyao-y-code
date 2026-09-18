import { mount } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, expect, it } from 'vitest';
import Sidebar from './Sidebar.vue';
import { useAppStore } from '../stores/appStore';

const ORDER_KEY = 'ycode-sidebar-group-order';
const PROJECTS = ['D:/proj/a', 'D:/proj/b', 'D:/proj/c'];

function makeGroup(path: string) {
  return {
    folder_path: path,
    folder_name: path.split('/').pop(),
    is_global: false,
    is_current: false,
    sessions: [{ name: `${path}-s0`, display_name: '会话 0', updated_at: '' }],
  };
}

function makeGlobal() {
  return { folder_path: 'GLOBAL', folder_name: '对话', is_global: true, is_current: false, sessions: [] };
}

function mountSidebar(groups: any[]) {
  const pinia = createPinia();
  setActivePinia(pinia);
  const store = useAppStore();
  store.sessionGroups = groups as any;
  return mount(Sidebar, { global: { plugins: [pinia] } });
}

/** 分组渲染顺序（含 GLOBAL）：读 data-group-path 比读标题文本更稳 */
function orderOf(wrapper: any): string[] {
  return wrapper.findAll('.group-header').map((h: any) => h.attributes('data-group-path'));
}

/** 按路径定位分组根节点，避免依赖 GLOBAL 是否占了第 0 位 */
function groupByPath(wrapper: any, path: string) {
  const idx = orderOf(wrapper).indexOf(path);
  return wrapper.findAll('.session-group')[idx];
}

/** 拖拽源：在 group-header 上派发带 dataTransfer 的 dragstart */
async function dragStart(wrapper: any, path: string) {
  await groupByPath(wrapper, path).find('.group-header').trigger('dragstart', {
    dataTransfer: { effectAllowed: '', setData: () => {} },
  });
}

/** 落到目标的 y 坐标：上半区（0~10）插到前，下半区（10~20）插到后 */
async function dropOn(wrapper: any, path: string, clientY: number) {
  const el = groupByPath(wrapper, path);
  await el.trigger('dragover', { clientY });
  await el.trigger('drop', { clientY });
}

const HALF_BEFORE = 5;
const HALF_AFTER = 15;

function makeThreeProjects() {
  return [makeGlobal(), ...PROJECTS.map(makeGroup)];
}

beforeEach(() => {
  localStorage.clear();
  delete (window as any).pywebview;
  // jsdom 无布局引擎，getBoundingClientRect 恒为 0，会让"上半区/下半区"判定失效；
  // 统一桩成 top=0 / height=20 的固定盒，使落点中线逻辑真正被覆盖
  (Element.prototype as any).getBoundingClientRect = () => ({
    top: 0, bottom: 20, height: 20, left: 0, right: 0, width: 0, x: 0, y: 0,
    toJSON: () => ({}),
  });
});

it('拖到目标下半区时插到目标之后，顺序落盘并在重新挂载后保持', async () => {
  const groups = makeThreeProjects();
  const w1 = mountSidebar(groups);
  expect(orderOf(w1)).toEqual(['GLOBAL', ...PROJECTS]);

  await dragStart(w1, 'D:/proj/a');
  await dropOn(w1, 'D:/proj/b', HALF_AFTER); // 落在 b 的下半区 → 插到 b 之后

  expect(orderOf(w1)).toEqual(['GLOBAL', 'D:/proj/b', 'D:/proj/a', 'D:/proj/c']);
  expect(JSON.parse(localStorage.getItem(ORDER_KEY) as string)).toEqual([
    'D:/proj/b',
    'D:/proj/a',
    'D:/proj/c',
  ]);
  w1.unmount();

  // 重新挂载（等价于重启前端）后仍是用户排好的顺序
  const w2 = mountSidebar(groups);
  expect(orderOf(w2)).toEqual(['GLOBAL', 'D:/proj/b', 'D:/proj/a', 'D:/proj/c']);
  w2.unmount();
});

it('拖到目标上半区时插到目标之前', async () => {
  const w = mountSidebar(makeThreeProjects());

  await dragStart(w, 'D:/proj/c');
  await dropOn(w, 'D:/proj/a', HALF_BEFORE); // 落在 a 的上半区 → 插到 a 之前

  expect(orderOf(w)).toEqual(['GLOBAL', 'D:/proj/c', 'D:/proj/a', 'D:/proj/b']);
  w.unmount();
});

it('「对话」分组固定最顶、不可拖动，也不能作为落点', async () => {
  const w = mountSidebar(makeThreeProjects());
  const headers = w.findAll('.group-header');
  expect(headers[0].attributes('draggable')).toBe('false');
  expect(headers[1].attributes('draggable')).toBe('true');

  await dragStart(w, 'D:/proj/c'); // 拖 c 到「对话」上
  await dropOn(w, 'GLOBAL', HALF_AFTER);

  expect(orderOf(w)).toEqual(['GLOBAL', ...PROJECTS]);
  expect(localStorage.getItem(ORDER_KEY)).toBeNull();
  w.unmount();
});

it('未记录的新分组追加在末尾，不影响已排好的顺序', async () => {
  localStorage.setItem(ORDER_KEY, JSON.stringify(['D:/proj/c']));
  const w = mountSidebar(makeThreeProjects());

  // c 按记忆排到最前，a/b 未记录故保持后端字母序排在后面
  expect(orderOf(w)).toEqual(['GLOBAL', 'D:/proj/c', 'D:/proj/a', 'D:/proj/b']);
  w.unmount();
});

it('右键菜单「上移 / 下移 / 置顶」按动作调整顺序', async () => {
  const w = mountSidebar(makeThreeProjects());
  const openMenu = async (path: string) => {
    const idx = orderOf(w).indexOf(path);
    await w.findAll('.group-header')[idx].trigger('contextmenu', { clientX: 10, clientY: 10 });
  };

  await openMenu('D:/proj/b');
  expect(w.find('[data-test="group-ctx-menu"]').exists()).toBe(true);
  await w.get('[data-test="group-move-up"]').trigger('click');
  expect(orderOf(w)).toEqual(['GLOBAL', 'D:/proj/b', 'D:/proj/a', 'D:/proj/c']);
  // 执行后菜单自动关闭
  expect(w.find('[data-test="group-ctx-menu"]').exists()).toBe(false);

  await openMenu('D:/proj/a');
  await w.get('[data-test="group-move-down"]').trigger('click');
  expect(orderOf(w)).toEqual(['GLOBAL', 'D:/proj/b', 'D:/proj/c', 'D:/proj/a']);

  await openMenu('D:/proj/a');
  await w.get('[data-test="group-move-top"]').trigger('click');
  expect(orderOf(w)).toEqual(['GLOBAL', 'D:/proj/a', 'D:/proj/b', 'D:/proj/c']);
  expect(JSON.parse(localStorage.getItem(ORDER_KEY) as string)).toEqual([
    'D:/proj/a',
    'D:/proj/b',
    'D:/proj/c',
  ]);
  w.unmount();
});

it('首个分组不能上移/置顶、末个分组不能下移，菜单项置灰且逻辑拒绝越界', async () => {
  const w = mountSidebar(makeThreeProjects());

  await w.findAll('.group-header')[1].trigger('contextmenu', { clientX: 10, clientY: 10 });
  expect(w.get('[data-test="group-move-up"]').classes()).toContain('disabled');
  expect(w.get('[data-test="group-move-top"]').classes()).toContain('disabled');
  expect(w.get('[data-test="group-move-down"]').classes()).not.toContain('disabled');
  // 置灰只是交互降级，逻辑层同样要拒绝越界移动
  await w.get('[data-test="group-move-up"]').trigger('click');
  expect(orderOf(w)).toEqual(['GLOBAL', ...PROJECTS]);

  await w.findAll('.group-header')[3].trigger('contextmenu', { clientX: 10, clientY: 10 });
  expect(w.get('[data-test="group-move-down"]').classes()).toContain('disabled');
  w.unmount();
});

it('「对话」分组不弹出排序菜单，点击菜单外自动关闭', async () => {
  const w = mountSidebar(makeThreeProjects());

  await w.findAll('.group-header')[0].trigger('contextmenu', { clientX: 10, clientY: 10 });
  expect(w.find('[data-test="group-ctx-menu"]').exists()).toBe(false);

  await w.findAll('.group-header')[1].trigger('contextmenu', { clientX: 10, clientY: 10 });
  expect(w.find('[data-test="group-ctx-menu"]').exists()).toBe(true);
  document.dispatchEvent(new MouseEvent('mousedown'));
  await w.vm.$nextTick();
  expect(w.find('[data-test="group-ctx-menu"]').exists()).toBe(false);
  w.unmount();
});

it('顺序存储损坏时退回后端顺序，不抛错', () => {
  localStorage.setItem(ORDER_KEY, '{不是合法 JSON');
  const w = mountSidebar([makeGlobal(), makeGroup('D:/proj/b'), makeGroup('D:/proj/a')]);

  expect(orderOf(w)).toEqual(['GLOBAL', 'D:/proj/b', 'D:/proj/a']);
  w.unmount();
});

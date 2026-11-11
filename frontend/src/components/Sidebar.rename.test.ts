import { mount, type VueWrapper } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import { afterEach, expect, it, vi } from 'vitest';
import { nextTick } from 'vue';
import Sidebar from './Sidebar.vue';
import { useAppStore } from '../stores/appStore';

// 挂载到 document.body：重命名输入框的自动聚焦 / 全选需要真实文档焦点环境，
// jsdom 下 detached 节点拿不到 activeElement。
const wrappers: VueWrapper[] = [];
afterEach(() => {
  wrappers.splice(0).forEach((w) => w.unmount());
  delete (window as any).pywebview;
});

// 每个用例自建会话数据（不 mock store，测真实行为）
function sessionGroups() {
  return [
    {
      folder_path: 'GLOBAL',
      folder_name: '对话',
      is_global: true,
      is_current: true,
      sessions: [
        { name: 'session-a', display_name: '会话 A', updated_at: '', workdir: 'GLOBAL' },
        { name: 'session-b', display_name: '会话 B', updated_at: '', workdir: 'GLOBAL' },
      ],
    },
  ];
}

function setup(api: Record<string, any> = {}) {
  (window as any).pywebview = { api };
  const pinia = createPinia();
  setActivePinia(pinia);
  const store = useAppStore();
  store.sessionGroups = sessionGroups() as any;
  const wrapper = mount(Sidebar, { global: { plugins: [pinia] }, attachTo: document.body });
  wrappers.push(wrapper);
  return { wrapper, store };
}

// 右键第 index 个会话项；用原生 MouseEvent 以便携带 clientX/clientY（菜单跟随鼠标坐标）
async function openMenu(
  wrapper: VueWrapper,
  index = 0,
  at: { clientX: number; clientY: number } = { clientX: 40, clientY: 60 },
) {
  const el = wrapper.findAll('.session-item')[index].element as HTMLElement;
  el.dispatchEvent(new MouseEvent('contextmenu', { bubbles: true, cancelable: true, ...at }));
  await nextTick();
}

async function clickRename(wrapper: VueWrapper) {
  await wrapper.find('[data-test="ctx-rename"]').trigger('click');
  await nextTick();
}

it('右键会话项弹出菜单，含重命名与删除两项并跟随鼠标坐标', async () => {
  const { wrapper } = setup();
  expect(wrapper.find('[data-test="session-ctx-menu"]').exists()).toBe(false);

  await openMenu(wrapper, 0, { clientX: 40, clientY: 60 });

  const menu = wrapper.find('[data-test="session-ctx-menu"]');
  expect(menu.exists()).toBe(true);
  expect(menu.find('[data-test="ctx-rename"]').text()).toContain('重命名');
  expect(menu.find('[data-test="ctx-delete"]').text()).toContain('删除');
  expect((menu.element as HTMLElement).style.left).toBe('40px');
  expect((menu.element as HTMLElement).style.top).toBe('60px');
});

it('菜单在视口右下边缘时向内收拢，不溢出屏幕', async () => {
  const { wrapper } = setup();
  await openMenu(wrapper, 0, {
    clientX: window.innerWidth + 500,
    clientY: window.innerHeight + 500,
  });

  const menu = wrapper.find('[data-test="session-ctx-menu"]');
  const left = parseInt((menu.element as HTMLElement).style.left, 10);
  const top = parseInt((menu.element as HTMLElement).style.top, 10);
  expect(left).toBeLessThan(window.innerWidth);
  expect(top).toBeLessThan(window.innerHeight);
});

it('点击菜单外部 / 按 Esc / 滚动窗口都会关闭菜单', async () => {
  const { wrapper } = setup();

  await openMenu(wrapper);
  document.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
  await nextTick();
  expect(wrapper.find('[data-test="session-ctx-menu"]').exists()).toBe(false);

  await openMenu(wrapper);
  document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
  await nextTick();
  expect(wrapper.find('[data-test="session-ctx-menu"]').exists()).toBe(false);

  await openMenu(wrapper);
  window.dispatchEvent(new Event('resize'));
  await nextTick();
  expect(wrapper.find('[data-test="session-ctx-menu"]').exists()).toBe(false);
});

it('点重命名后关闭菜单，条目就地变成预填显示名的输入框并自动聚焦全选', async () => {
  const { wrapper } = setup();
  await openMenu(wrapper);
  await clickRename(wrapper);

  expect(wrapper.find('[data-test="session-ctx-menu"]').exists()).toBe(false);
  const input = wrapper.find('[data-test="session-rename-input"]');
  expect(input.exists()).toBe(true);
  const el = input.element as HTMLInputElement;
  expect(el.value).toBe('会话 A');
  expect(document.activeElement).toBe(el);
  expect(el.selectionStart).toBe(0);
  expect(el.selectionEnd).toBe('会话 A'.length);
});

it('输入新名按 Enter → 以 (旧名, 新名) 调用 rename_session 并用返回 sessions 刷新列表', async () => {
  const renamed = sessionGroups();
  renamed[0].sessions[0].display_name = '新名字';
  const rename_session = vi.fn(async () => ({ ok: true, name: '新名字', sessions: renamed }));
  const { wrapper, store } = setup({ rename_session });

  await openMenu(wrapper);
  await clickRename(wrapper);
  const input = wrapper.find('[data-test="session-rename-input"]');
  await input.setValue('新名字');
  await input.trigger('keydown.enter');
  await new Promise((r) => setTimeout(r, 0));

  expect(rename_session).toHaveBeenCalledTimes(1);
  expect(rename_session).toHaveBeenCalledWith('session-a', '新名字');
  expect(store.sessionGroups).toEqual(renamed);
  expect(wrapper.find('[data-test="session-rename-input"]').exists()).toBe(false);
});

it('按 Esc 取消编辑 → 不发请求且输入框消失', async () => {
  const rename_session = vi.fn();
  const { wrapper, store } = setup({ rename_session });

  await openMenu(wrapper);
  await clickRename(wrapper);
  const input = wrapper.find('[data-test="session-rename-input"]');
  await input.setValue('写了一半');
  await input.trigger('keydown.esc');
  await new Promise((r) => setTimeout(r, 0));

  expect(rename_session).not.toHaveBeenCalled();
  expect(wrapper.find('[data-test="session-rename-input"]').exists()).toBe(false);
  expect(wrapper.find('.session-item .t').text()).toBe('会话 A');
  expect(store.sessionGroups[0].sessions[0].name).toBe('session-a');
});

it('值为空或与原名相同 → 不发请求并静默退出编辑态', async () => {
  const rename_session = vi.fn();
  const { wrapper } = setup({ rename_session });

  // 情形一：未改动直接回车
  await openMenu(wrapper);
  await clickRename(wrapper);
  await wrapper.find('[data-test="session-rename-input"]').trigger('keydown.enter');
  await new Promise((r) => setTimeout(r, 0));
  expect(wrapper.find('[data-test="session-rename-input"]').exists()).toBe(false);

  // 情形二：只输入空白
  await openMenu(wrapper);
  await clickRename(wrapper);
  const input = wrapper.find('[data-test="session-rename-input"]');
  await input.setValue('   ');
  await input.trigger('keydown.enter');
  await new Promise((r) => setTimeout(r, 0));

  expect(rename_session).not.toHaveBeenCalled();
  expect(wrapper.find('[data-test="session-rename-input"]').exists()).toBe(false);
});

it('输入框失焦时保存', async () => {
  const rename_session = vi.fn(async () => ({ ok: true, name: '新名', sessions: sessionGroups() }));
  const { wrapper } = setup({ rename_session });

  await openMenu(wrapper);
  await clickRename(wrapper);
  const input = wrapper.find('[data-test="session-rename-input"]');
  await input.setValue('新名');
  await input.trigger('blur');
  await new Promise((r) => setTimeout(r, 0));

  expect(rename_session).toHaveBeenCalledWith('session-a', '新名');
});

it('后端返回 ok:false → 弹出提示且列表不变', async () => {
  const rename_session = vi.fn(async () => ({ ok: false, msg: '该会话正在运行回合' }));
  const { wrapper, store } = setup({ rename_session });
  const before = JSON.parse(JSON.stringify(store.sessionGroups));

  await openMenu(wrapper);
  await clickRename(wrapper);
  const input = wrapper.find('[data-test="session-rename-input"]');
  await input.setValue('新名字');
  await input.trigger('keydown.enter');
  await new Promise((r) => setTimeout(r, 0));

  expect(store.confirmModal.show).toBe(true);
  expect(store.confirmModal.title).toBe('会话管理');
  expect(store.confirmModal.message).toContain('重命名失败');
  expect(store.confirmModal.message).toContain('该会话正在运行回合');
  expect(store.sessionGroups).toEqual(before);
});

it('点删除 → 关闭菜单并进入二次确认态，确认后才按旧名真正删除', async () => {
  const delete_session = vi.fn();
  const { wrapper } = setup({ delete_session });

  await openMenu(wrapper);
  await wrapper.find('[data-test="ctx-delete"]').trigger('click');
  await new Promise((r) => setTimeout(r, 0));

  expect(wrapper.find('[data-test="session-ctx-menu"]').exists()).toBe(false);
  expect(wrapper.find('.session-item.confirming').exists()).toBe(true);
  expect(wrapper.find('.session-item .s-confirm').exists()).toBe(true);
  expect(delete_session).not.toHaveBeenCalled();

  // 二次确认才真正落盘删除，且用的是该会话当时的名字
  await wrapper.find('.session-item .s-confirm').trigger('click');
  await new Promise((r) => setTimeout(r, 0));
  expect(delete_session).toHaveBeenCalledWith('session-a');
});

it('编辑态下点击输入框不会触发会话切换', async () => {
  const switch_session = vi.fn();
  const { wrapper, store } = setup({ switch_session });
  store.currentSessionName = 'session-a';

  // 右键第二个会话（非当前会话）后进入编辑态
  await openMenu(wrapper, 1);
  await clickRename(wrapper);
  await wrapper.find('[data-test="session-rename-input"]').trigger('click');
  await new Promise((r) => setTimeout(r, 0));

  // 真实行为断言：点击输入框走的是 @click.stop，没有触发 onSessionClick
  expect(switch_session).not.toHaveBeenCalled();
  expect(store.currentSessionName).toBe('session-a');
  expect(wrapper.find('[data-test="session-rename-input"]').exists()).toBe(true);
});

it('事件丢失时，重命名当前会话仍按后端返回的最终名迁移当前会话名', async () => {
  // 撞名场景：后端把新名改成带后缀的最终名，且此处不注入 session_renamed 事件，
  // 模拟事件在重连窗口期丢失——前端必须自己按 res.name 兜底迁移。
  const renamed = sessionGroups();
  renamed[0].sessions[0].name = '新名字_1';
  renamed[0].sessions[0].display_name = '新名字_1';
  const rename_session = vi.fn(async () => ({ ok: true, name: '新名字_1', sessions: renamed }));
  const { store } = setup({ rename_session });
  store.currentSessionName = 'session-a';
  store.currentSessionDisplayName = '会话 A';

  await store.renameSession('session-a', '新名字');
  await new Promise((r) => setTimeout(r, 0));

  // 否则侧栏高亮消失，后续 switch_session / send_message 仍拿旧名
  expect(store.currentSessionName).toBe('新名字_1');
  expect(store.currentSessionDisplayName).toBe('新名字_1');
});

// 派发原生 keydown：IME 的 isComposing / keyCode 只能经 KeyboardEventInit 设置，
// vue-test-utils 的 trigger(..., { isComposing }) 走 Object.assign 会写不进只读属性
function dispatchKey(el: Element, key: string, init: KeyboardEventInit = {}) {
  const ev = new KeyboardEvent('keydown', { key, bubbles: true, cancelable: true, ...init });
  el.dispatchEvent(ev);
  return ev;
}

it('IME 组合态的 Enter（isComposing:true）→ 不提交、不改名、不调用 rename，且仍留在编辑态', async () => {
  const rename_session = vi.fn();
  const { wrapper, store } = setup({ rename_session });

  await openMenu(wrapper);
  await clickRename(wrapper);
  const input = wrapper.find('[data-test="session-rename-input"]');
  // 组合中文字尚未上屏：v-model 里是中间态，若此刻提交会被「与原名相同」静默吞掉
  await input.setValue('候选词还没上屏');
  const ev = dispatchKey(input.element, 'Enter', { isComposing: true });
  await new Promise((r) => setTimeout(r, 0));

  expect(rename_session).not.toHaveBeenCalled();
  expect(store.sessionGroups[0].sessions[0].display_name).toBe('会话 A');
  // 组合态不 preventDefault，把上屏交给输入法自己处理
  expect(ev.defaultPrevented).toBe(false);
  expect(wrapper.find('[data-test="session-rename-input"]').exists()).toBe(true);
});

it('非组合态的 Enter（isComposing:false）→ 正常提交重命名', async () => {
  const renamed = sessionGroups();
  renamed[0].sessions[0].display_name = '新名字';
  const rename_session = vi.fn(async () => ({ ok: true, name: '新名字', sessions: renamed }));
  const { wrapper } = setup({ rename_session });

  await openMenu(wrapper);
  await clickRename(wrapper);
  const input = wrapper.find('[data-test="session-rename-input"]');
  await input.setValue('新名字');
  const ev = dispatchKey(input.element, 'Enter', { isComposing: false });
  await new Promise((r) => setTimeout(r, 0));

  expect(rename_session).toHaveBeenCalledTimes(1);
  expect(rename_session).toHaveBeenCalledWith('session-a', '新名字');
  expect(ev.defaultPrevented).toBe(true);
  expect(wrapper.find('[data-test="session-rename-input"]').exists()).toBe(false);
});

it('IME 组合态的 Esc（isComposing:true）→ 仅取消候选词，不退出编辑态', async () => {
  const rename_session = vi.fn();
  const { wrapper } = setup({ rename_session });

  await openMenu(wrapper);
  await clickRename(wrapper);
  const input = wrapper.find('[data-test="session-rename-input"]');
  const ev = dispatchKey(input.element, 'Escape', { isComposing: true });
  await new Promise((r) => setTimeout(r, 0));

  expect(ev.defaultPrevented).toBe(false);
  expect(wrapper.find('[data-test="session-rename-input"]').exists()).toBe(true);
  expect(rename_session).not.toHaveBeenCalled();
});

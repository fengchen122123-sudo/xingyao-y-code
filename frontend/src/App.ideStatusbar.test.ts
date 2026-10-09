import { mount } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, expect, it, vi } from 'vitest';

import App from './App.vue';
import { useAppStore } from './stores/appStore';
import { useIdeStore } from './stores/ideStore';

// 子组件全部 stub（照搬 App.panelResize.test.ts）：本用例只断言 App 模板里状态栏的
// 接线，不依赖任何子组件的行为——包括 EditorPane（光标上报的接线由它自己的用例负责，
// 这里断言的是「状态栏读 store 的哪个字段」）。
// teleport 也 stub：IDE 模式下 App 会把对话区传送进 #ide-chat-host，而分离容器里的
// 目标节点还不在 document 中（IdeLayout.test.ts 同款处理）。
const stubs = {
  Topbar: true, Sidebar: true, KimiEmptyState: true, TurnGroup: true,
  InputArea: true, RightPanel: true, ConfirmModal: true, UpdateModal: true,
  PermCard: true, ImageLightbox: true, Subpage: true, SettingsPage: true,
  AppIcon: true, MessageNavRail: true, RemoteDirBrowser: true,
  IdeLayout: true, TerminalPane: true, BackgroundTasksBar: true, teleport: true,
};

/**
 * pywebview 桥 stub。**必须在 mount() 之前装好**：App 挂载会立刻用到桥
 * （后台任务轮询、窗口状态查询），ideStore.openFile 也要经 ide_read_file 拿内容。
 * 这里只提供打开文件这条主链上的三个方法，其余缺失的桥方法由调用方各自静默降级。
 */
function installBridge() {
  const api = {
    ide_read_file: vi.fn(async (path: string) => ({
      ok: true, path, content: 'x = 1\n', encoding: 'utf-8', bom: false, eol: 'lf', hash: 'h1',
    })),
    ide_set_open_files: vi.fn(async () => ({ ok: true, count: 1 })),
    ide_save_layout: vi.fn(async () => ({ ok: true })),
  };
  (window as any).pywebview = { api };
  return api;
}

beforeEach(() => {
  localStorage.clear();
  setActivePinia(createPinia());
  installBridge();
});

function mountApp() {
  const store = useAppStore();
  store.initialized = true;
  const wrapper = mount(App, { global: { stubs } });
  return { store, ide: useIdeStore(), wrapper };
}

it('对话模式下状态栏没有 IDE 徽标，也没有编辑器段（打开标签也不出现）', async () => {
  const { ide, wrapper } = mountApp();

  expect(wrapper.find('[data-test="ide-status-mode"]').exists()).toBe(false);
  expect(wrapper.find('[data-test="ide-status-cursor"]').exists()).toBe(false);
  expect(wrapper.find('[data-test="status-editor"]').exists()).toBe(false);

  // 即便有活动标签，对话模式下编辑器段也不得出现：状态栏必须与加入本段之前完全一致
  await ide.openFile('C:/ws/a.py');
  await wrapper.vm.$nextTick();
  expect(ide.activeTab?.path).toBe('C:/ws/a.py');
  expect(wrapper.find('[data-test="ide-status-mode"]').exists()).toBe(false);
  expect(wrapper.find('[data-test="ide-status-cursor"]').exists()).toBe(false);
  expect(wrapper.find('[data-test="status-editor"]').exists()).toBe(false);

  wrapper.unmount();
});

it('切到 IDE 模式并打开标签后徽标与编辑器段都出现，光标初始为「行 1，列 1」', async () => {
  const { store, ide, wrapper } = mountApp();

  store.toggleIdeMode();
  await wrapper.vm.$nextTick();
  // 只有徽标：还没有活动标签，编辑器段没有数据源，不能凭空渲染
  expect(wrapper.find('[data-test="ide-status-mode"]').text()).toBe('IDE');
  expect(wrapper.find('[data-test="status-editor"]').exists()).toBe(false);

  await ide.openFile('C:/ws/a.py');
  await wrapper.vm.$nextTick();

  expect(wrapper.find('[data-test="ide-status-cursor"]').text()).toBe('行 1，列 1');
  expect(wrapper.find('[data-test="ide-status-indent"]').text()).toBe('空格：4');
  expect(wrapper.find('[data-test="ide-status-encoding"]').text()).toBe('UTF-8');
  expect(wrapper.find('[data-test="ide-status-eol"]').text()).toBe('LF');

  wrapper.unmount();
});

it('语言段按活动标签路径显示友好名：.py → Python、.ts → TypeScript', async () => {
  const { store, ide, wrapper } = mountApp();
  store.toggleIdeMode();

  await ide.openFile('C:/ws/a.py');
  await wrapper.vm.$nextTick();
  expect(wrapper.find('[data-test="ide-status-language"]').text()).toBe('Python');

  await ide.openFile('C:/ws/b.ts');
  await wrapper.vm.$nextTick();
  expect(wrapper.find('[data-test="ide-status-language"]').text()).toBe('TypeScript');

  wrapper.unmount();
});

it('切标签或无活动标签时光标段不显示陈旧数据', async () => {
  const { store, ide, wrapper } = mountApp();
  store.toggleIdeMode();
  await ide.openFile('C:/ws/a.py');
  await wrapper.vm.$nextTick();

  // 先摆一个「非初始」的行列，证明这段文字确实跟着 store 走（而不是写死的 1,1）
  ide.setCursor(12, 5);
  await wrapper.vm.$nextTick();
  expect(wrapper.find('[data-test="ide-status-cursor"]').text()).toBe('行 12，列 5');

  // 切到另一个标签：上一个文件的行列立即失效，回到 1,1 等新编辑器实例上报
  await ide.openFile('C:/ws/b.ts');
  await wrapper.vm.$nextTick();
  expect(ide.activeTab?.path).toBe('C:/ws/b.ts');
  expect(wrapper.find('[data-test="ide-status-cursor"]').text()).toBe('行 1，列 1');

  // 关掉当前标签：活动标签换成邻居（a.py），行列同样不得沿用 b.ts 之外的历史值
  ide.closeTab('C:/ws/b.ts');
  await wrapper.vm.$nextTick();
  expect(ide.activeTab?.path).toBe('C:/ws/a.py');
  expect(wrapper.find('[data-test="ide-status-cursor"]').text()).toBe('行 1，列 1');

  // 关掉全部标签：没有活动标签就没有数据源，整段退场，而不是留着上次的 12,5
  ide.closeTab('C:/ws/a.py');
  await wrapper.vm.$nextTick();
  expect(ide.activeTab).toBe(null);
  expect(wrapper.find('[data-test="status-editor"]').exists()).toBe(false);
  expect(wrapper.find('[data-test="ide-status-cursor"]').exists()).toBe(false);

  wrapper.unmount();
});

it('换行符与编码段读活动标签的真实值（crlf → CRLF、gbk → GBK），不是写死的常量', async () => {
  const { store, ide, wrapper } = mountApp();
  // 覆盖读取结果：一个 CRLF + GBK 的标签
  (window as any).pywebview.api.ide_read_file = vi.fn(async (path: string) => ({
    ok: true, path, content: 'x = 1\n', encoding: 'gbk', bom: false, eol: 'crlf', hash: 'h1',
  }));
  store.toggleIdeMode();

  await ide.openFile('C:/ws/legacy.py');
  await wrapper.vm.$nextTick();

  expect(wrapper.find('[data-test="ide-status-eol"]').text()).toBe('CRLF');
  expect(wrapper.find('[data-test="ide-status-encoding"]').text()).toBe('GBK');

  wrapper.unmount();
});

it('语言推断不出语言 id 时，.vue 仍显示公认名字 Vue', async () => {
  const { store, ide, wrapper } = mountApp();
  store.toggleIdeMode();

  await ide.openFile('C:/ws/App.vue');
  await wrapper.vm.$nextTick();

  expect(wrapper.find('[data-test="ide-status-language"]').text()).toBe('Vue');

  wrapper.unmount();
});

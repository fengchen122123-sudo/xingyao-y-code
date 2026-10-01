import { mount } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, expect, it, vi } from 'vitest';
import { nextTick } from 'vue';
import Sidebar from './Sidebar.vue';
import { useAppStore } from '../stores/appStore';

// 版本选择会写入 localStorage（记住上次选择），逐用例清空避免相互影响
beforeEach(() => {
  localStorage.clear();
});

it('点击手机图标后调用 get_remote_bind_qr 并渲染二维码 img', async () => {
  const get_remote_bind_qr = vi.fn(async () => ({
    ok: true,
    url: 'http://192.168.1.5:8765/m/?token=abc123',
    token: 'abc123',
    qr: 'data:image/png;base64,QUJD',
    expires_in: 86400,
  }));
  (window as any).pywebview = { api: { get_remote_bind_qr } };

  const pinia = createPinia();
  setActivePinia(pinia);
  const wrapper = mount(Sidebar, { global: { plugins: [pinia] } });

  const btn = wrapper.find('[data-test="remote-bind-button"]');
  expect(btn.exists()).toBe(true);
  await btn.trigger('click');
  // 等待异步 api 调用与响应式更新落定（宏任务 tick）
  await new Promise((r) => setTimeout(r, 0));

  expect(get_remote_bind_qr).toHaveBeenCalledTimes(1);
  // 默认版本为移动端：第二参数为 true
  expect(get_remote_bind_qr).toHaveBeenNthCalledWith(1, false, true);
  const modal = wrapper.find('[data-test="remote-bind-modal"]');
  expect(modal.exists()).toBe(true);
  const img = modal.find('img');
  expect(img.exists()).toBe(true);
  expect(img.attributes('src')).toBe('data:image/png;base64,QUJD');
});

it('获取二维码失败时不显示弹窗，并弹出错误提示', async () => {
  const get_remote_bind_qr = vi.fn(async () => ({ ok: false, msg: '局域网服务未就绪' }));
  (window as any).pywebview = { api: { get_remote_bind_qr } };

  const pinia = createPinia();
  setActivePinia(pinia);
  const wrapper = mount(Sidebar, { global: { plugins: [pinia] } });
  const store = useAppStore();

  await wrapper.find('[data-test="remote-bind-button"]').trigger('click');
  await new Promise((r) => setTimeout(r, 0));

  expect(wrapper.find('[data-test="remote-bind-modal"]').exists()).toBe(false);
  expect(store.confirmModal.show).toBe(true);
  expect(store.confirmModal.notice).toBe(true);
  expect(store.confirmModal.title).toBe('手机访问');
});

it('点击弹窗遮罩不关闭，仅点击关闭按钮才关闭', async () => {
  const get_remote_bind_qr = vi.fn(async () => ({
    ok: true,
    url: 'http://192.168.1.5:8765/?token=abc123',
    token: 'abc123',
    qr: 'data:image/png;base64,QUJD',
    expires_in: 86400,
  }));
  (window as any).pywebview = { api: { get_remote_bind_qr } };

  const pinia = createPinia();
  setActivePinia(pinia);
  const wrapper = mount(Sidebar, { global: { plugins: [pinia] } });

  await wrapper.find('[data-test="remote-bind-button"]').trigger('click');
  await new Promise((r) => setTimeout(r, 0));
  expect(wrapper.find('[data-test="remote-bind-modal"]').exists()).toBe(true);

  // 点击遮罩（弹窗外区域）不应触发关闭
  await wrapper.find('[data-test="remote-bind-modal"]').trigger('click');
  expect(wrapper.find('[data-test="remote-bind-modal"]').exists()).toBe(true);

  // 仅点击“关闭”按钮才关闭
  await wrapper.find('[data-test="remote-bind-close"]').trigger('click');
  expect(wrapper.find('[data-test="remote-bind-modal"]').exists()).toBe(false);
});

it('重复打开弹窗直接复用同一 token（不强制重签）', async () => {
  const get_remote_bind_qr = vi.fn(async () => ({
    ok: true,
    url: 'http://192.168.1.5:8765/?token=abc123',
    token: 'abc123',
    qr: 'data:image/png;base64,QUJD',
    expires_in: 86400,
  }));
  (window as any).pywebview = { api: { get_remote_bind_qr } };

  const pinia = createPinia();
  setActivePinia(pinia);
  const wrapper = mount(Sidebar, { global: { plugins: [pinia] } });

  await wrapper.find('[data-test="remote-bind-button"]').trigger('click');
  await new Promise((r) => setTimeout(r, 0));
  // 关闭后再打开：仍应不带 force_new 调用，由后端决定复用
  await wrapper.find('[data-test="remote-bind-close"]').trigger('click');
  await wrapper.find('[data-test="remote-bind-button"]').trigger('click');
  await new Promise((r) => setTimeout(r, 0));

  expect(get_remote_bind_qr).toHaveBeenCalledTimes(2);
  expect(get_remote_bind_qr).toHaveBeenNthCalledWith(1, false, true);
  expect(get_remote_bind_qr).toHaveBeenNthCalledWith(2, false, true);
});

it('点击刷新按钮以 force_new 重新获取 token', async () => {
  const get_remote_bind_qr = vi
    .fn()
    .mockResolvedValueOnce({
      ok: true,
      url: 'http://192.168.1.5:8765/?token=old',
      token: 'old',
      qr: 'data:image/png;base64,OLD',
      expires_in: 86400,
    })
    .mockResolvedValueOnce({
      ok: true,
      url: 'http://192.168.1.5:8765/?token=new',
      token: 'new',
      qr: 'data:image/png;base64,NEW',
      expires_in: 86400,
    });
  (window as any).pywebview = { api: { get_remote_bind_qr } };

  const pinia = createPinia();
  setActivePinia(pinia);
  const wrapper = mount(Sidebar, { global: { plugins: [pinia] } });

  await wrapper.find('[data-test="remote-bind-button"]').trigger('click');
  await new Promise((r) => setTimeout(r, 0));
  expect(wrapper.find('[data-test="remote-bind-modal"]').exists()).toBe(true);

  const refreshBtn = wrapper.find('[data-test="remote-bind-refresh"]');
  expect(refreshBtn.exists()).toBe(true);
  await refreshBtn.trigger('click');
  await new Promise((r) => setTimeout(r, 0));

  expect(get_remote_bind_qr).toHaveBeenLastCalledWith(true, true);
  const modal = wrapper.find('[data-test="remote-bind-modal"]');
  expect(modal.find('img').attributes('src')).toBe('data:image/png;base64,NEW');
});

it('永久有效（expires_in=0）时不做倒计时自动刷新', async () => {
  vi.useFakeTimers();
  try {
    const get_remote_bind_qr = vi.fn(async () => ({
      ok: true,
      url: 'http://192.168.1.5:8765/?token=forever',
      token: 'forever',
      qr: 'data:image/png;base64,FOREVER',
      expires_in: 0,
    }));
    (window as any).pywebview = { api: { get_remote_bind_qr } };

    const pinia = createPinia();
    setActivePinia(pinia);
    const wrapper = mount(Sidebar, { global: { plugins: [pinia] } });

    await wrapper.find('[data-test="remote-bind-button"]').trigger('click');
    await vi.advanceTimersByTimeAsync(0);

    // 文案显示“永久有效”
    expect(wrapper.find('[data-test="remote-bind-hint"]').text()).toContain('永久有效');

    // 推进 3 秒：永久 token 不应触发自动刷新（调用次数保持 1）
    await vi.advanceTimersByTimeAsync(3000);
    expect(get_remote_bind_qr).toHaveBeenCalledTimes(1);
  } finally {
    vi.useRealTimers();
  }
});

it('单入口默认移动端，切换 web远端后以单参签名请求并记住选择', async () => {
  const get_remote_bind_qr = vi.fn(async (_forceNew: boolean, mobile?: boolean) => ({
    ok: true,
    url: mobile
      ? 'http://192.168.1.5:8765/m/?token=abc123'
      : 'http://192.168.1.5:8765/?token=abc123',
    token: 'abc123',
    qr: mobile ? 'data:image/png;base64,MOB' : 'data:image/png;base64,WEB',
    expires_in: 0,
  }));
  (window as any).pywebview = { api: { get_remote_bind_qr } };

  const pinia = createPinia();
  setActivePinia(pinia);
  const wrapper = mount(Sidebar, { global: { plugins: [pinia] } });

  await wrapper.find('[data-test="remote-bind-button"]').trigger('click');
  await new Promise((r) => setTimeout(r, 0));

  // 默认移动端：带 mobile 参数，标题直接显示版本名
  expect(get_remote_bind_qr).toHaveBeenNthCalledWith(1, false, true);
  let modal = wrapper.find('[data-test="remote-bind-modal"]');
  expect(modal.find('h3').text()).toBe('移动端');
  expect(modal.find('img').attributes('src')).toBe('data:image/png;base64,MOB');

  // 切到 web远端：单参签名（保持旧路径契约），二维码与标题随之切换
  await wrapper.find('[data-test="bind-mode-desktop"]').trigger('click');
  await new Promise((r) => setTimeout(r, 0));
  expect(get_remote_bind_qr).toHaveBeenLastCalledWith(false);
  modal = wrapper.find('[data-test="remote-bind-modal"]');
  expect(modal.find('h3').text()).toBe('web远端');
  expect(modal.find('img').attributes('src')).toBe('data:image/png;base64,WEB');
  expect(localStorage.getItem('ycode-remote-bind-mode')).toBe('desktop');

  // 再切回移动端：恢复带参签名
  await wrapper.find('[data-test="bind-mode-mobile"]').trigger('click');
  await new Promise((r) => setTimeout(r, 0));
  expect(get_remote_bind_qr).toHaveBeenLastCalledWith(false, true);

  // 关闭后重新打开：沿用上次记住的版本（移动端）
  await wrapper.find('[data-test="remote-bind-close"]').trigger('click');
  await wrapper.find('[data-test="remote-bind-button"]').trigger('click');
  await new Promise((r) => setTimeout(r, 0));
  expect(get_remote_bind_qr).toHaveBeenLastCalledWith(false, true);
  expect(wrapper.find('[data-test="remote-bind-modal"] h3').text()).toBe('移动端');
});

it('子页面打开时点击新会话会立即返回对话页面', async () => {
  delete (window as any).pywebview;
  const pinia = createPinia();
  setActivePinia(pinia);
  const store = useAppStore();
  store.openSubpage('skills');
  const wrapper = mount(Sidebar, { global: { plugins: [pinia] } });

  await wrapper.findAll('.menu-item')[0].trigger('click');

  expect(store.subpage.show).toBe(false);
  expect(store.pendingWorkdir).toBe('GLOBAL');
  wrapper.unmount();
});

it('子页面打开时点击历史会话会立即返回对话页面', async () => {
  delete (window as any).pywebview;
  const pinia = createPinia();
  setActivePinia(pinia);
  const store = useAppStore();
  store.sessionGroups = [{
    folder_path: 'GLOBAL',
    folder_name: '对话',
    is_global: true,
    is_current: true,
    sessions: [{ name: 'session-a', display_name: '历史会话 A', updated_at: '' }],
  }] as any;
  store.openSubpage('plugins');
  const wrapper = mount(Sidebar, { global: { plugins: [pinia] } });

  await wrapper.get('.session-item').trigger('click');

  expect(store.subpage.show).toBe(false);
  expect(store.currentSessionName).toBe('session-a');
  wrapper.unmount();
});

it('桌面端根元素内联宽度跟随 store.sidebarWidth，折叠时负 margin 跟随实际宽度', async () => {
  delete (window as any).pywebview;
  const pinia = createPinia();
  setActivePinia(pinia);
  const store = useAppStore();
  store.sidebarWidth = 300;
  const wrapper = mount(Sidebar, { global: { plugins: [pinia] } });

  const root = wrapper.find('#sidebar');
  expect(root.attributes('style')).toContain('width: 300px');
  expect(root.attributes('style')).not.toContain('margin-left');

  store.sidebarCollapsed = true;
  await nextTick();
  expect(root.attributes('style')).toContain('margin-left: -300px');
  wrapper.unmount();
});

it('默认主题下不渲染 QQ2009 专属的用户卡与搜索框', () => {
  delete (window as any).pywebview;
  const pinia = createPinia();
  setActivePinia(pinia);
  const wrapper = mount(Sidebar, { global: { plugins: [pinia] } });

  expect(wrapper.find('.qq09-user-card').exists()).toBe(false);
  expect(wrapper.find('[data-test="qq09-search"]').exists()).toBe(false);
  wrapper.unmount();
});

it('QQ2009 主题激活时渲染用户卡与搜索框，点击搜索框打开会话搜索弹窗', async () => {
  delete (window as any).pywebview;
  const pinia = createPinia();
  setActivePinia(pinia);
  const store = useAppStore();
  // 本文件没有既有的主题构造器，这里只给最小对象；GuiTheme 结构复杂，用 as any 断言
  store.guiTheme = { id: 'builtin.qq2009' } as any;
  const searchSpy = vi.spyOn(store, 'openSessionSearchModal');
  const wrapper = mount(Sidebar, { global: { plugins: [pinia] } });

  expect(wrapper.find('.qq09-user-card').exists()).toBe(true);
  expect(wrapper.find('.qq09-user-card').text()).toContain('木偶');
  const search = wrapper.find('[data-test="qq09-search"]');
  expect(search.exists()).toBe(true);

  await search.trigger('click');

  expect(searchSpy).toHaveBeenCalledTimes(1);
  expect(store.sessionSearchModal.show).toBe(true);
  wrapper.unmount();
});

// ---- QQ2009 用户卡昵称就地编辑（昵称存 localStorage，文件顶部 beforeEach 已
// localStorage.clear()，逐用例隔离） ----
function mountQq09() {
  delete (window as any).pywebview;
  const pinia = createPinia();
  setActivePinia(pinia);
  const store = useAppStore();
  store.guiTheme = { id: 'builtin.qq2009' } as any;
  const wrapper = mount(Sidebar, { global: { plugins: [pinia] } });
  return { wrapper, store };
}

it('QQ2009 用户卡默认渲染昵称「木偶」与铅笔编辑按钮，号码固定 (260711)', () => {
  const { wrapper } = mountQq09();

  expect(wrapper.find('.qq09-user-name-text').text()).toBe('木偶');
  expect(wrapper.find('[data-test="qq09-name-edit"]').exists()).toBe(true);
  expect(wrapper.find('.qq09-user-no').text()).toContain('(260711)');
  expect(wrapper.find('[data-test="qq09-name-input"]').exists()).toBe(false);
  wrapper.unmount();
});

it('点击铅笔进入编辑态，回车保存新昵称并写入 localStorage，再次编辑可见新昵称', async () => {
  const { wrapper } = mountQq09();

  await wrapper.find('[data-test="qq09-name-edit"]').trigger('click');
  const input = wrapper.find('[data-test="qq09-name-input"]');
  expect(input.exists()).toBe(true);
  expect((input.element as HTMLInputElement).value).toBe('木偶');

  await input.setValue('超级无敌长昵称测试一二三四五六');
  await input.trigger('keyup.enter');

  expect(wrapper.find('.qq09-user-name-text').text()).toBe('超级无敌长昵称测试一二三四五六');
  expect(localStorage.getItem('qq09.sidebarUserName')).toBe('超级无敌长昵称测试一二三四五六');
  // 保存后退出编辑态
  expect(wrapper.find('[data-test="qq09-name-input"]').exists()).toBe(false);

  // 再次进入编辑态：草稿即当前昵称
  await wrapper.find('[data-test="qq09-name-edit"]').trigger('click');
  expect((wrapper.find('[data-test="qq09-name-input"]').element as HTMLInputElement).value)
    .toBe('超级无敌长昵称测试一二三四五六');
  wrapper.unmount();
});

it('输入纯空格回车不保存：昵称保持「木偶」且不写 localStorage', async () => {
  const { wrapper } = mountQq09();

  await wrapper.find('[data-test="qq09-name-edit"]').trigger('click');
  const input = wrapper.find('[data-test="qq09-name-input"]');
  await input.setValue('   ');
  await input.trigger('keyup.enter');

  expect(wrapper.find('.qq09-user-name-text').text()).toBe('木偶');
  expect(localStorage.getItem('qq09.sidebarUserName')).toBeNull();
  wrapper.unmount();
});

it('编辑态按 Esc 退出并放弃改动，昵称不变', async () => {
  const { wrapper } = mountQq09();

  await wrapper.find('[data-test="qq09-name-edit"]').trigger('click');
  const input = wrapper.find('[data-test="qq09-name-input"]');
  await input.setValue('这个名字不该被保存');
  await input.trigger('keyup.esc');

  expect(wrapper.find('[data-test="qq09-name-input"]').exists()).toBe(false);
  expect(wrapper.find('.qq09-user-name-text').text()).toBe('木偶');
  expect(localStorage.getItem('qq09.sidebarUserName')).toBeNull();
  wrapper.unmount();
});

it('localStorage 预置昵称「阿木」时初始渲染「阿木」', () => {
  localStorage.setItem('qq09.sidebarUserName', '阿木');
  const { wrapper } = mountQq09();

  expect(wrapper.find('.qq09-user-name-text').text()).toBe('阿木');
  wrapper.unmount();
});

// ---- QQ2009 用户卡在线状态切换（状态存 localStorage key 'qq09.sidebarStatus'，
// 文件顶部 beforeEach 已 localStorage.clear()，逐用例隔离） ----
it('QQ2009 用户卡默认渲染在线状态「我在线上」，状态点为在线绿点', () => {
  const { wrapper } = mountQq09();

  expect(wrapper.find('.qq09-status-label').text()).toBe('我在线上');
  expect(wrapper.find('.qq09-st-icon[data-st="online"]').exists()).toBe(true);
  expect(wrapper.find('.qq09-status-dot').attributes('data-status')).toBe('online');
  // 初始不展开菜单
  expect(wrapper.find('[data-test="qq09-status-menu"]').exists()).toBe(false);
  wrapper.unmount();
});

it('点击状态行展开下拉菜单，列出六个状态', async () => {
  const { wrapper } = mountQq09();

  await wrapper.find('[data-test="qq09-status-trigger"]').trigger('click');

  const menu = wrapper.find('[data-test="qq09-status-menu"]');
  expect(menu.exists()).toBe(true);
  const items = wrapper.findAll('.qq09-status-item');
  expect(items.length).toBe(6);
  expect(items.map((i) => i.find('.qq09-status-item-name').text()))
    .toEqual(['我在线上', '忙碌中', '离开', '请勿打扰', 'Q我吧', '隐身']);
  // 六个图标都是纯 CSS 圆点（data-st 驱动配色），无 emoji / 图片
  expect(items.map((i) => i.find('.qq09-st-icon').attributes('data-st')))
    .toEqual(['online', 'busy', 'away', 'dnd', 'qme', 'offline']);
  wrapper.unmount();
});

it('点击「忙碌中」后菜单关闭、状态行与头像状态点同步更新并写入 localStorage', async () => {
  const { wrapper } = mountQq09();

  await wrapper.find('[data-test="qq09-status-trigger"]').trigger('click');
  await wrapper.find('[data-test="qq09-status-item-busy"]').trigger('click');

  expect(wrapper.find('[data-test="qq09-status-menu"]').exists()).toBe(false);
  expect(wrapper.find('.qq09-status-label').text()).toBe('忙碌中');
  expect(wrapper.find('.qq09-st-icon[data-st="busy"]').exists()).toBe(true);
  expect(localStorage.getItem('qq09.sidebarStatus')).toBe('busy');
  expect(wrapper.find('.qq09-status-dot').attributes('data-status')).toBe('busy');
  wrapper.unmount();
});

it('菜单展开时按 Esc 关闭菜单且不改变当前状态', async () => {
  const { wrapper } = mountQq09();

  await wrapper.find('[data-test="qq09-status-trigger"]').trigger('click');
  expect(wrapper.find('[data-test="qq09-status-menu"]').exists()).toBe(true);

  // Esc 监听挂在 document 上（与侧栏既有菜单同一套关闭时机）
  document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
  await nextTick();

  expect(wrapper.find('[data-test="qq09-status-menu"]').exists()).toBe(false);
  expect(wrapper.find('.qq09-status-label').text()).toBe('我在线上');
  expect(localStorage.getItem('qq09.sidebarStatus')).toBeNull();
  wrapper.unmount();
});

it('localStorage 预置状态 qme 时初始渲染「Q我吧」且状态点为蓝色', () => {
  localStorage.setItem('qq09.sidebarStatus', 'qme');
  const { wrapper } = mountQq09();

  expect(wrapper.find('.qq09-status-label').text()).toBe('Q我吧');
  expect(wrapper.find('.qq09-status-dot').attributes('data-status')).toBe('qme');
  wrapper.unmount();
});

// ---- QQ2009 用户卡 QQ 等级展示（装饰性，21 级 = 1 太阳 + 1 月亮 + 1 星星） ----
it('QQ2009 用户卡渲染 QQ 等级：1 太阳 1 月亮 1 星星，末尾数字 21', () => {
  const { wrapper } = mountQq09();

  const level = wrapper.find('[data-test="qq09-user-level"]');
  expect(level.exists()).toBe(true);
  const icons = level.findAll('.qq09-lv-icon');
  expect(icons.length).toBe(3);
  expect(icons.map((i) => i.attributes('data-lv'))).toEqual(['sun', 'moon', 'star']);
  expect(level.find('.qq09-lv-num').text()).toBe('21');
  expect(level.attributes('title')).toContain('21');
  wrapper.unmount();
});

// ---- QQ2009 用户卡个性签名就地编辑（签名存 localStorage key 'qq09.sidebarSign'，
// 文件顶部 beforeEach 已 localStorage.clear()，逐用例隔离） ----
const QQ09_SIGN_DEFAULT = '代码写完了吗，就刷 QQ？';

it('QQ2009 用户卡默认渲染签名「代码写完了吗，就刷 QQ？」与签名编辑按钮，且网名不再带 title', () => {
  const { wrapper } = mountQq09();

  expect(wrapper.find('.qq09-user-sign-text').text()).toBe(QQ09_SIGN_DEFAULT);
  expect(wrapper.find('[data-test="qq09-sign-edit"]').exists()).toBe(true);
  expect(wrapper.find('[data-test="qq09-sign-input"]').exists()).toBe(false);
  // 改为气泡方案后不应残留浏览器默认 tooltip 的 title 绑定
  expect(wrapper.find('.qq09-user-name-text').attributes('title')).toBeUndefined();
  wrapper.unmount();
});

it('点击签名铅笔进入编辑态，回车保存新签名并写入 localStorage，再次编辑可见新签名', async () => {
  const { wrapper } = mountQq09();

  await wrapper.find('[data-test="qq09-sign-edit"]').trigger('click');
  const input = wrapper.find('[data-test="qq09-sign-input"]');
  expect(input.exists()).toBe(true);
  expect((input.element as HTMLInputElement).value).toBe(QQ09_SIGN_DEFAULT);

  await input.setValue('正在写星瑶，勿扰');
  await input.trigger('keyup.enter');

  expect(wrapper.find('.qq09-user-sign-text').text()).toBe('正在写星瑶，勿扰');
  expect(localStorage.getItem('qq09.sidebarSign')).toBe('正在写星瑶，勿扰');
  // 保存后退出编辑态
  expect(wrapper.find('[data-test="qq09-sign-input"]').exists()).toBe(false);

  // 再次进入编辑态：草稿即当前签名
  await wrapper.find('[data-test="qq09-sign-edit"]').trigger('click');
  expect((wrapper.find('[data-test="qq09-sign-input"]').element as HTMLInputElement).value)
    .toBe('正在写星瑶，勿扰');
  wrapper.unmount();
});

it('签名输入纯空格回车时恢复默认签名并写入 localStorage', async () => {
  const { wrapper } = mountQq09();

  await wrapper.find('[data-test="qq09-sign-edit"]').trigger('click');
  const input = wrapper.find('[data-test="qq09-sign-input"]');
  await input.setValue('    ');
  await input.trigger('keyup.enter');

  expect(wrapper.find('.qq09-user-sign-text').text()).toBe(QQ09_SIGN_DEFAULT);
  expect(localStorage.getItem('qq09.sidebarSign')).toBe(QQ09_SIGN_DEFAULT);
  wrapper.unmount();
});

it('签名编辑态按 Esc 退出并放弃改动，签名不变', async () => {
  const { wrapper } = mountQq09();

  await wrapper.find('[data-test="qq09-sign-edit"]').trigger('click');
  const input = wrapper.find('[data-test="qq09-sign-input"]');
  await input.setValue('这条签名不该被保存');
  await input.trigger('keyup.esc');

  expect(wrapper.find('[data-test="qq09-sign-input"]').exists()).toBe(false);
  expect(wrapper.find('.qq09-user-sign-text').text()).toBe(QQ09_SIGN_DEFAULT);
  expect(localStorage.getItem('qq09.sidebarSign')).toBeNull();
  wrapper.unmount();
});

it('localStorage 预置签名时初始渲染预置值', () => {
  localStorage.setItem('qq09.sidebarSign', '签名来自 localStorage');
  const { wrapper } = mountQq09();

  expect(wrapper.find('.qq09-user-sign-text').text()).toBe('签名来自 localStorage');
  wrapper.unmount();
});

// ---- QQ2009 用户卡自定义气泡提示：仅文本被省略号截断时，悬停约 350ms 后显示完整内容 ----
it('悬停未截断的网名不显示气泡；截断时显示完整文本，移出后消失', async () => {
  // 气泡用 setTimeout 延迟触发，用假定时器推进
  vi.useFakeTimers();
  const { wrapper } = mountQq09();
  const nameEl = wrapper.find('.qq09-user-name-text');

  // jsdom 不排版，手动打桩宽高模拟「未截断」（scrollWidth === clientWidth）
  Object.defineProperty(nameEl.element, 'scrollWidth', { configurable: true, value: 40 });
  Object.defineProperty(nameEl.element, 'clientWidth', { configurable: true, value: 40 });
  await nameEl.trigger('mouseenter');
  vi.advanceTimersByTime(400);
  await nextTick();
  expect(wrapper.find('[data-test="qq09-user-tip"]').exists()).toBe(false);

  // 改成「已截断」（scrollWidth > clientWidth）：悬停后出现气泡，内容为完整网名
  Object.defineProperty(nameEl.element, 'scrollWidth', { configurable: true, value: 200 });
  await nameEl.trigger('mouseenter');
  vi.advanceTimersByTime(400);
  await nextTick();
  const tip = wrapper.find('[data-test="qq09-user-tip"]');
  expect(tip.exists()).toBe(true);
  expect(tip.text()).toBe('木偶');

  // 移出后立即消失
  await nameEl.trigger('mouseleave');
  await nextTick();
  expect(wrapper.find('[data-test="qq09-user-tip"]').exists()).toBe(false);

  wrapper.unmount();
  vi.useRealTimers();
});

// ---- QQ2009 用户卡头像点击更换（DataURL 持久化，key 'qq09.sidebarAvatar'；
// 文件顶部 beforeEach 已 localStorage.clear()，逐用例隔离）。
// 测试链路：注入 input.files → 触发 change → 等 FileReader 回调 → 断言 DOM/存储。
// jsdom 自带 FileReader（readAsDataURL 可用），故整条链路能真实跑通，无需打桩。 ----
async function qq09FlushFileReader() {
  // jsdom 的 FileReader 用多段 setImmediate 任务（loadstart → load）投递事件，
  // 单次 await 不足以跑完；这里空转几轮宏任务 + 渲染让回调落定。
  for (let i = 0; i < 5; i++) {
    await new Promise((r) => setTimeout(r, 1));
    await nextTick();
  }
}

/** 模拟用户选中某个文件：input.files 只读，用 defineProperty 注入后触发 change */
async function qq09PickAvatarFile(
  wrapper: ReturnType<typeof mountQq09>['wrapper'],
  file: File,
) {
  const input = wrapper.find('[data-test="qq09-avatar-input"]');
  Object.defineProperty(input.element, 'files', { configurable: true, value: [file] });
  await input.trigger('change');
  await qq09FlushFileReader();
}

it('QQ2009 用户卡默认无自定义头像：不渲染头像图与恢复按钮，头像带「点击更换」提示且隐藏文件框就位', () => {
  const { wrapper } = mountQq09();

  expect(wrapper.find('.qq09-avatar-img').exists()).toBe(false);
  expect(wrapper.find('[data-test="qq09-avatar-reset"]').exists()).toBe(false);
  // 头像区可点击（title 提示 + 隐藏文件框挂上 image/* 过滤）
  expect(wrapper.find('.qq09-avatar').attributes('title')).toBe('点击更换头像');
  const input = wrapper.find('[data-test="qq09-avatar-input"]');
  expect(input.exists()).toBe(true);
  expect(input.attributes('type')).toBe('file');
  expect(input.attributes('accept')).toBe('image/*');
  // 状态点与自定义头像互不影响：默认态照旧渲染
  expect(wrapper.find('.qq09-status-dot').exists()).toBe(true);
  wrapper.unmount();
});

it('点击头像转交隐藏文件框；选中 GIF 后渲染头像 img（src 为 data: URL）、写入 localStorage 并出现恢复按钮', async () => {
  const { wrapper } = mountQq09();

  // 点头像 → 调 input.click()（在 input 实例上打桩，验证转交链路）
  const inputEl = wrapper.find('[data-test="qq09-avatar-input"]').element as HTMLInputElement;
  const clickSpy = vi.spyOn(inputEl, 'click');
  await wrapper.find('.qq09-avatar').trigger('click');
  expect(clickSpy).toHaveBeenCalledTimes(1);
  clickSpy.mockRestore();

  // GIF 头字节（GIF89a），type 为 image/gif：动图与静图走完全同一条路径
  const gif = new File([new Uint8Array([0x47, 0x49, 0x46, 0x38, 0x39, 0x61])], 'a.gif', { type: 'image/gif' });
  await qq09PickAvatarFile(wrapper, gif);

  const img = wrapper.find('.qq09-avatar-img');
  expect(img.exists()).toBe(true);
  const src = img.attributes('src') ?? '';
  expect(src.startsWith('data:')).toBe(true);
  expect(src.startsWith('data:image/gif')).toBe(true);
  expect(img.attributes('alt')).toBe('用户头像');
  // 持久化：localStorage 里就是同一个 DataURL，刷新/重启后仍可显示（GIF 继续播放）
  expect(localStorage.getItem('qq09.sidebarAvatar')).toBe(src);
  expect(wrapper.find('[data-test="qq09-avatar-reset"]').exists()).toBe(true);
  wrapper.unmount();
});

it('点击恢复按钮：头像 img 消失、localStorage 清除，且 @click.stop 不触发文件选择框', async () => {
  localStorage.setItem('qq09.sidebarAvatar', 'data:image/gif;base64,R0lGODlh');
  const { wrapper } = mountQq09();
  expect(wrapper.find('.qq09-avatar-img').exists()).toBe(true);

  const inputEl = wrapper.find('[data-test="qq09-avatar-input"]').element as HTMLInputElement;
  const clickSpy = vi.spyOn(inputEl, 'click');
  await wrapper.find('[data-test="qq09-avatar-reset"]').trigger('click');

  // 恢复钮吞掉冒泡，点头像才有的文件框转交不应发生
  expect(clickSpy).not.toHaveBeenCalled();
  clickSpy.mockRestore();
  expect(wrapper.find('.qq09-avatar-img').exists()).toBe(false);
  expect(wrapper.find('[data-test="qq09-avatar-reset"]').exists()).toBe(false);
  expect(localStorage.getItem('qq09.sidebarAvatar')).toBeNull();
  wrapper.unmount();
});

it('选中非图片文件（text/plain）时忽略：不渲染 img、不写 localStorage', async () => {
  const { wrapper } = mountQq09();

  await qq09PickAvatarFile(wrapper, new File(['hello'], 'note.txt', { type: 'text/plain' }));

  expect(wrapper.find('.qq09-avatar-img').exists()).toBe(false);
  expect(wrapper.find('[data-test="qq09-avatar-reset"]').exists()).toBe(false);
  expect(localStorage.getItem('qq09.sidebarAvatar')).toBeNull();
  wrapper.unmount();
});

it('localStorage 预置头像 DataURL 时初始渲染自定义头像与恢复按钮', () => {
  const preset = 'data:image/png;base64,QUJD';
  localStorage.setItem('qq09.sidebarAvatar', preset);
  const { wrapper } = mountQq09();

  expect(wrapper.find('.qq09-avatar-img').attributes('src')).toBe(preset);
  expect(wrapper.find('[data-test="qq09-avatar-reset"]').exists()).toBe(true);
  wrapper.unmount();
});

it('localStorage 里的头像数据损坏（非 data:image/*）时兜底回默认企鹅头像，不炸用户卡', () => {
  localStorage.setItem('qq09.sidebarAvatar', '{not-a-data-url');
  const { wrapper } = mountQq09();

  expect(wrapper.find('.qq09-avatar-img').exists()).toBe(false);
  // 用户卡其余部分照常渲染
  expect(wrapper.find('.qq09-user-name-text').text()).toBe('木偶');
  wrapper.unmount();
});

it('超过 2MB 的图片被拒绝：alert 提示且不写 localStorage（size 打桩，jsdom 不必真造 2MB 数据）', async () => {
  // jsdom 未实现 alert，打桩以捕获提示文案
  const alertSpy = vi.spyOn(window, 'alert').mockImplementation(() => {});
  const { wrapper } = mountQq09();
  const big = new File([new Uint8Array([1, 2, 3])], 'big.gif', { type: 'image/gif' });
  // 处理逻辑只看 file.size：真造 2MB 二进制数组对断言无增益，直接对只读属性打桩
  Object.defineProperty(big, 'size', { configurable: true, value: 2 * 1024 * 1024 + 1 });

  await qq09PickAvatarFile(wrapper, big);

  expect(alertSpy).toHaveBeenCalledWith('图片不能超过 2MB（localStorage 容量有限，建议压缩后再传）');
  alertSpy.mockRestore();
  expect(wrapper.find('.qq09-avatar-img').exists()).toBe(false);
  expect(localStorage.getItem('qq09.sidebarAvatar')).toBeNull();
  wrapper.unmount();
});

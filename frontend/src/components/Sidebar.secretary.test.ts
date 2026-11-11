import { mount, type VueWrapper } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import { afterEach, expect, it, vi } from 'vitest';
import { nextTick } from 'vue';
import Sidebar from './Sidebar.vue';
import { useAppStore } from '../stores/appStore';

// 挂载到 document.body：起名弹窗的自动聚焦 / 全选需要真实文档焦点环境，
// jsdom 下 detached 节点拿不到 activeElement。
const wrappers: VueWrapper[] = [];
afterEach(() => {
  wrappers.splice(0).forEach((w) => w.unmount());
  delete (window as any).pywebview;
});

// 内置推荐名字池（与 Sidebar.vue 的 SECRETARY_NAME_POOL 同源，仅供断言取用）
const NAME_POOL = ['星河', '小满', '望舒', '清和', '云疏', '拾光'];

// 每个用例自建分组数据（不 mock store，测真实行为）。秘书分组是虚拟分组
// （folder_path='SECRETARY'、is_secretary=true）；组内会话带后端下发的秘书名。
function secretaryGroups(secretaryName = '星河') {
  return [
    {
      folder_path: 'SECRETARY',
      folder_name: '秘书',
      is_secretary: true,
      is_current: false,
      sessions: [
        {
          name: 'session-sec',
          display_name: secretaryName,
          secretary_name: secretaryName,
          count: 2,
          workdir: 'GLOBAL',
          is_global: true,
          updated_at: '',
        },
      ],
    },
    {
      folder_path: 'GLOBAL',
      folder_name: '对话',
      is_global: true,
      is_current: true,
      sessions: [
        { name: 'session-a', display_name: '会话 A', count: 1, workdir: 'GLOBAL', updated_at: '' },
      ],
    },
  ];
}

function setup(api: Record<string, any> = {}) {
  (window as any).pywebview = { api };
  const pinia = createPinia();
  setActivePinia(pinia);
  const store = useAppStore();
  store.sessionGroups = secretaryGroups() as any;
  const wrapper = mount(Sidebar, { global: { plugins: [pinia] }, attachTo: document.body });
  wrappers.push(wrapper);
  return { wrapper, store };
}

// 点秘书分组的 ＋：只应打开起名弹窗，不得直接建会话
async function openNameModal(wrapper: VueWrapper) {
  await wrapper.find('[data-test="secretary-add"]').trigger('click');
  await nextTick();
}

/** 等一轮微任务 + 宏任务：桥调用与随后的 store 收尾（可能多级 await）都落地 */
async function flush() {
  await new Promise((r) => setTimeout(r, 0));
}

it('点秘书分组的 ＋ 弹出起名弹窗：预填推荐名并聚焦全选，且不直接建会话', async () => {
  const create_secretary_session = vi.fn();
  const { wrapper } = setup({ create_secretary_session });

  expect(wrapper.find('[data-test="secretary-name-modal"]').exists()).toBe(false);
  await openNameModal(wrapper);

  expect(wrapper.find('[data-test="secretary-name-modal"]').exists()).toBe(true);
  const input = wrapper.find('[data-test="secretary-name-input"]');
  const el = input.element as HTMLInputElement;
  expect(NAME_POOL).toContain(el.value);
  expect(document.activeElement).toBe(el);
  expect(el.selectionStart).toBe(0);
  expect(el.selectionEnd).toBe(el.value.length);
  // 起名之前绝不建会话：否则会留下一个无名的孤儿会话
  expect(create_secretary_session).not.toHaveBeenCalled();
});

it('名字为空或只有空白时创建按钮 disabled，点它也不发请求', async () => {
  const create_secretary_session = vi.fn();
  const { wrapper } = setup({ create_secretary_session });
  await openNameModal(wrapper);

  const input = wrapper.find('[data-test="secretary-name-input"]');
  const btn = wrapper.find('[data-test="secretary-name-create"]');
  expect((btn.element as HTMLButtonElement).disabled).toBe(false);

  await input.setValue('');
  expect((btn.element as HTMLButtonElement).disabled).toBe(true);

  await input.setValue('   ');
  expect((btn.element as HTMLButtonElement).disabled).toBe(true);
  await btn.trigger('click');
  await flush();

  expect(create_secretary_session).not.toHaveBeenCalled();
  // 弹窗保持打开，用户能就地改完再提交
  expect(wrapper.find('[data-test="secretary-name-modal"]').exists()).toBe(true);
});

it('超过 20 字符时按钮 disabled 且不静默截断，回到 20 字即可创建', async () => {
  const create_secretary_session = vi.fn();
  const { wrapper } = setup({ create_secretary_session });
  await openNameModal(wrapper);

  const input = wrapper.find('[data-test="secretary-name-input"]');
  const btn = wrapper.find('[data-test="secretary-name-create"]');

  await input.setValue('星'.repeat(21));
  expect((btn.element as HTMLButtonElement).disabled).toBe(true);
  // 输入内容保持原样：截断会让显示名与用户输入不符，校验交给按钮禁用与提示
  expect((input.element as HTMLInputElement).value).toBe('星'.repeat(21));
  await btn.trigger('click');
  await flush();
  expect(create_secretary_session).not.toHaveBeenCalled();

  await input.setValue('星'.repeat(20));
  expect((btn.element as HTMLButtonElement).disabled).toBe(false);
});

it('填名按 Enter → 以该名字调用 create_secretary_session 并关闭弹窗', async () => {
  const created = secretaryGroups('清和');
  const create_secretary_session = vi.fn(async () => ({
    ok: true,
    session_name: 'session-sec2',
    title: '新会话',
    display_name: '清和',
    secretary_name: '清和',
    workdir: 'GLOBAL',
  }));
  const get_active_agent_name = vi.fn(async () => ({ agent_name: '清和', is_secretary: true }));
  const { wrapper, store } = setup({
    create_secretary_session,
    get_active_agent_name,
    get_sessions: vi.fn(async () => created),
  });

  await openNameModal(wrapper);
  const input = wrapper.find('[data-test="secretary-name-input"]');
  // 两端空白必须先去掉再提交（后端也会 trim，但前端口径要一致）
  await input.setValue('  清和  ');
  await input.trigger('keydown.enter');
  await flush();

  expect(create_secretary_session).toHaveBeenCalledTimes(1);
  // 第三参数是专属人设：没填就传空串（= 使用内置默认秘书人设）
  expect(create_secretary_session).toHaveBeenCalledWith('新会话', '清和', '');
  expect(wrapper.find('[data-test="secretary-name-modal"]').exists()).toBe(false);
  // 会话以该名字落库并切为当前会话：侧栏条目、欢迎页、头部读的都是它
  expect(store.currentSessionName).toBe('session-sec2');
  expect(store.currentSessionDisplayName).toBe('清和');
  expect(store.agentName).toBe('清和');
});

it('点取消 / 按 Esc 关闭弹窗，不发任何建会话请求', async () => {
  const create_secretary_session = vi.fn();
  const { wrapper } = setup({ create_secretary_session });

  await openNameModal(wrapper);
  await wrapper.find('[data-test="secretary-name-cancel"]').trigger('click');
  await nextTick();
  expect(wrapper.find('[data-test="secretary-name-modal"]').exists()).toBe(false);

  await openNameModal(wrapper);
  const input = wrapper.find('[data-test="secretary-name-input"]');
  await input.setValue('写了一半的名字');
  await input.trigger('keydown.esc');
  await nextTick();
  expect(wrapper.find('[data-test="secretary-name-modal"]').exists()).toBe(false);

  await flush();
  expect(create_secretary_session).not.toHaveBeenCalled();
});

it('秘书分组内的会话条目显示后端下发的秘书名（display_name）', async () => {
  const { wrapper } = setup();
  const first = wrapper.findAll('.session-item')[0];
  expect(first.find('.t').text()).toBe('星河');
});

it('切换到秘书会话后 store.agentName 刷新为桥返回的秘书名，切回普通会话再回落', async () => {
  const switch_session = vi.fn(async () => ({
    ok: true,
    messages: [],
    sessions: secretaryGroups(),
    is_global: true,
    workdir: 'GLOBAL',
    message_queue: [],
  }));
  const get_active_agent_name = vi.fn(async () => ({ agent_name: '望舒', is_secretary: true }));
  const { store } = setup({ switch_session, get_active_agent_name });
  store.agentName = '星瑶';

  await store.switchSession('session-sec', '望舒');
  await flush();

  expect(get_active_agent_name).toHaveBeenCalled();
  expect(store.agentName).toBe('望舒');

  // 切回普通会话：桥回设置页名字，显示名跟着回落（不能被秘书名粘住）
  get_active_agent_name.mockResolvedValue({ agent_name: '星瑶', is_secretary: false });
  await store.switchSession('session-a', '会话 A');
  await flush();

  expect(store.agentName).toBe('星瑶');
});

it('桥取不到生效名时回落设置页名字，不打断切换流程', async () => {
  const switch_session = vi.fn(async () => ({
    ok: true,
    messages: [],
    sessions: secretaryGroups(),
    is_global: true,
    workdir: 'GLOBAL',
    message_queue: [],
  }));
  const get_active_agent_name = vi.fn(async () => {
    throw new Error('桥不可用');
  });
  const { store } = setup({ switch_session, get_active_agent_name });
  store.agentName = '望舒';

  await store.switchSession('session-a', '会话 A');
  await flush();

  expect(store.agentName).toBe('星瑶');
  expect(store.currentSessionName).toBe('session-a');
});

// ---------------------------------------------------------------------------
// 秘书专属人设（每个秘书是一个独立 agent，人设只属于它自己的会话）
// ---------------------------------------------------------------------------

it('新建秘书可填专属人设：与名字一起提交给桥，并去掉首尾空白', async () => {
  const create_secretary_session = vi.fn(async () => ({
    ok: true,
    session_name: 'session-sec2',
    title: '新会话',
    display_name: '清和',
    secretary_name: '清和',
    secretary_persona: '你是说话干脆的行政秘书。',
    workdir: 'GLOBAL',
  }));
  const { wrapper } = setup({
    create_secretary_session,
    get_active_agent_name: vi.fn(async () => ({ agent_name: '清和', is_secretary: true })),
    get_sessions: vi.fn(async () => secretaryGroups('清和')),
  });

  await openNameModal(wrapper);
  await wrapper.find('[data-test="secretary-name-input"]').setValue('清和');
  await wrapper
    .find('[data-test="secretary-persona-input"]')
    .setValue('  你是说话干脆的行政秘书。  ');
  await wrapper.find('[data-test="secretary-name-create"]').trigger('click');
  await flush();

  expect(create_secretary_session).toHaveBeenCalledWith(
    '新会话',
    '清和',
    '你是说话干脆的行政秘书。',
  );
  expect(wrapper.find('[data-test="secretary-name-modal"]').exists()).toBe(false);
});

it('秘书会话右键「编辑人设」：回填已有人设，保存走 set_secretary_persona', async () => {
  const set_secretary_persona = vi.fn(async () => ({
    ok: true,
    session_name: 'session-sec',
    secretary_persona: '新人设',
    sessions: secretaryGroups(),
  }));
  const { wrapper, store } = setup({ set_secretary_persona });
  // 条目带上后端下发的 secretary_persona，弹窗应原样回填（不再打一次后端）
  store.sessionGroups = [
    {
      ...secretaryGroups()[0],
      sessions: [
        {
          name: 'session-sec',
          display_name: '星河',
          secretary_name: '星河',
          secretary_persona: '旧人设：说话简短',
          count: 2,
          workdir: 'GLOBAL',
          updated_at: '',
        },
      ],
    },
    secretaryGroups()[1],
  ] as any;

  const el = wrapper.findAll('.session-item')[0].element as HTMLElement;
  el.dispatchEvent(
    new MouseEvent('contextmenu', { bubbles: true, cancelable: true, clientX: 40, clientY: 60 }),
  );
  await nextTick();
  await wrapper.find('[data-test="ctx-secretary-persona"]').trigger('click');
  await nextTick();

  const textarea = wrapper.find('[data-test="secretary-persona-input"]');
  expect((textarea.element as HTMLTextAreaElement).value).toBe('旧人设：说话简短');

  await textarea.setValue('新人设');
  await wrapper.find('[data-test="secretary-name-create"]').trigger('click');
  await flush();

  expect(set_secretary_persona).toHaveBeenCalledWith('session-sec', '新人设');
});

it('普通会话的右键菜单没有「编辑人设」入口', async () => {
  const { wrapper } = setup();
  // 侧栏渲染顺序里普通会话排在秘书之后，按显示名定位那个条目
  const item = wrapper
    .findAll('.session-item')
    .find((w) => w.text().includes('会话 A'));
  expect(item).toBeTruthy();
  (item!.element as HTMLElement).dispatchEvent(
    new MouseEvent('contextmenu', { bubbles: true, cancelable: true, clientX: 40, clientY: 60 }),
  );
  await nextTick();
  expect(wrapper.find('[data-test="ctx-secretary-persona"]').exists()).toBe(false);
});

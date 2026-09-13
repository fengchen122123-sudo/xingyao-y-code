import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, expect, it, vi } from 'vitest';
import { useAppStore } from './appStore';


beforeEach(() => setActivePinia(createPinia()));

it('notifyChat pushes a notice message into the current conversation', () => {
  const store = useAppStore();

  store.notifyChat('已中断当前回合');

  const last = store.messages[store.messages.length - 1];
  expect(last.role).toBe('notice');
  expect(last.content).toBe('已中断当前回合');
});

it('notifyModal opens a single-button notice modal that stays until confirmed', () => {
  const store = useAppStore();

  store.notifyModal('检查更新', '当前已是最新版本');

  expect(store.confirmModal.show).toBe(true);
  expect(store.confirmModal.notice).toBe(true);
  expect(store.confirmModal.title).toBe('检查更新');
  expect(store.confirmModal.message).toBe('当前已是最新版本');
  expect(store.confirmModal.confirmLabel).toBe('确定');

  // 用户点击确定后才关闭
  store.resolveConfirm(false);
  expect(store.confirmModal.show).toBe(false);
});

it('notice modal resolves a pending confirm as cancel instead of leaving it dangling', async () => {
  const store = useAppStore();
  let answer: boolean | null = null;
  store.askConfirm('删除模型', '删除后需重新添加', '确定删除？').then((ok) => { answer = ok; });

  // 确认框 pending 期间弹 notice：确认框按"取消"安全收尾（promise 不悬置），再显示提示
  store.notifyModal('提示', '后台任务已完成');
  expect(store.confirmModal.notice).toBe(true);
  // promise reaction 需要宏任务 tick 后才落定（单微任务 tick 不可靠）
  await new Promise(r => setTimeout(r, 0));
  expect(answer).toBe(false);

  store.resolveConfirm(true);
  expect(store.confirmModal.show).toBe(false);
});

// 空会话（欢迎页）中切换权限模式/推理强度/沙盒：提示不得插入消息流，
// 否则 notice 会让空状态页消失，表现为"页面跳转"（与 setModel 同款守卫）。
it('setPermissionMode 在空会话静默生效，不产生 notice', async () => {
  (window as any).pywebview = {
    api: { set_permission_mode: vi.fn(async () => ({ ok: true, permission_mode: 'auto' })) },
  };
  const store = useAppStore();

  await store.setPermissionMode('auto' as never);

  expect(store.permissionMode).toBe('auto');
  expect(store.messages.length).toBe(0);
});

it('setPermissionMode 在已有对话的会话中插入 notice', async () => {
  (window as any).pywebview = {
    api: { set_permission_mode: vi.fn(async () => ({ ok: true, permission_mode: 'auto' })) },
  };
  const store = useAppStore();
  store.messages.push({ role: 'user', content: '你好' } as never);

  await store.setPermissionMode('auto' as never);

  const last = store.messages[store.messages.length - 1];
  expect(last.role).toBe('notice');
  expect(last.content).toContain('权限模式已调整');
});

it('setPermissionMode 失败时即使在空会话也保留失败提示', async () => {
  (window as any).pywebview = {
    api: { set_permission_mode: vi.fn(async () => ({ ok: false, msg: '模式非法' })) },
  };
  const store = useAppStore();

  await store.setPermissionMode('auto' as never);

  const last = store.messages[store.messages.length - 1];
  expect(last.role).toBe('notice');
  expect(last.content).toContain('权限模式调整失败');
});

// ---- 双端同步提示（post_notice 广播 + notice 桥事件回环插入）----

it('桥可用时 notifyChat 经 post_notice 广播，本地不直接插入（等事件回环）', () => {
  const postNotice = vi.fn(async () => ({ ok: true }));
  (window as any).pywebview = { api: { post_notice: postNotice } };
  const store = useAppStore();
  store.currentSessionName = 's1';
  store.messages.push({ role: 'user', content: '你好' } as never);

  store.notifyChat('已切换活动模型: k3');

  expect(postNotice).toHaveBeenCalledWith('已切换活动模型: k3', 's1', 'notice');
  // 本地不直插：等 notice 桥事件回环后统一插入，避免发起端重复
  expect(store.messages).toHaveLength(1);
});

it('post_notice 调用失败时降级为本地插入', async () => {
  (window as any).pywebview = {
    api: { post_notice: vi.fn(async () => { throw new Error('桥断开'); }) },
  };
  const store = useAppStore();

  store.notifyChat('提示内容');
  await new Promise(r => setTimeout(r, 0));

  const last = store.messages[store.messages.length - 1];
  expect(last.role).toBe('notice');
  expect(last.content).toBe('提示内容');
});

it('notice 桥事件按 kind=error 插入错误卡片（双端同步的统落点）', () => {
  const store = useAppStore();
  store.currentSessionName = 's1';
  store.messages.push({ role: 'user', content: '你好' } as never);

  store.handleBridgeEvent({
    type: 'notice',
    data: { message: '消息发送失败: x', kind: 'error', session_name: 's1' },
  } as any);

  const last = store.messages[store.messages.length - 1];
  expect(last.role).toBe('error');
  expect(last.content).toBe('消息发送失败: x');
});

it('notice 桥事件指向本端从未加载的会话时直接丢弃（不阻断历史回填）', () => {
  const store = useAppStore();
  store.currentSessionName = 's1';

  store.handleBridgeEvent({
    type: 'notice',
    data: { message: '别的会话的提示', kind: 'notice', session_name: 's2' },
  } as any);

  expect(store.messages).toHaveLength(0);
  // runtime 可能被创建但必须为空：非空会阻断打开 s2 时的数据库历史回填
  expect(store._getSessionRuntime('s2').messages).toHaveLength(0);
});

it('notice 桥事件写入已加载的非当前会话 runtime，切回可见', () => {
  const store = useAppStore();
  store.currentSessionName = 's1';
  store._getSessionRuntime('s2').messages.push({ role: 'user', content: '旧消息' } as never);

  store.handleBridgeEvent({
    type: 'notice',
    data: { message: '切换模型失败: x', kind: 'notice', session_name: 's2' },
  } as any);

  const s2Msgs = store._getSessionRuntime('s2').messages;
  expect(s2Msgs[s2Msgs.length - 1].role).toBe('notice');
  expect(s2Msgs[s2Msgs.length - 1].content).toBe('切换模型失败: x');
});

it('background_task_finished 按归属会话路由，不串到当前查看的会话', () => {
  const store = useAppStore();
  store.currentSessionName = 's1';
  store._getSessionRuntime('s2').messages.push({ role: 'user', content: '旧消息' } as never);

  store.handleBridgeEvent({
    type: 'background_task_finished',
    data: { task_id: 'e0c2c3be', command: 'npm run build', returncode: 129, session_name: 's2' },
  } as any);

  expect(store.messages).toHaveLength(0);
  const s2Msgs = store._getSessionRuntime('s2').messages;
  expect(s2Msgs[s2Msgs.length - 1].role).toBe('notice');
  expect(s2Msgs[s2Msgs.length - 1].content).toBe('后台任务 e0c2c3be 已结束（退出码 129）');
});

it('background_task_finished 缺 session_name（旧后端）回落当前会话', () => {
  const store = useAppStore();
  store.currentSessionName = 's1';
  store.messages.push({ role: 'user', content: '你好' } as never);

  store.handleBridgeEvent({
    type: 'background_task_finished',
    data: { task_id: 't1', command: 'x', returncode: 0 },
  } as any);

  const last = store.messages[store.messages.length - 1];
  expect(last.role).toBe('notice');
  expect(last.content).toBe('后台任务 t1 已完成');
});

it('background_subagent_terminal 按归属会话路由，不串到当前查看的会话', () => {
  const store = useAppStore();
  store.currentSessionName = 's1';
  store._getSessionRuntime('s2').messages.push({ role: 'user', content: '旧消息' } as never);

  store.handleBridgeEvent({
    type: 'background_subagent_terminal',
    data: { task_id: 'sub-1', session_name: 's2', status: 'completed', description: '调研', summary: '' },
  } as any);

  expect(store.messages).toHaveLength(0);
  const s2Msgs = store._getSessionRuntime('s2').messages;
  expect(s2Msgs[s2Msgs.length - 1].role).toBe('notice');
  expect(s2Msgs[s2Msgs.length - 1].content).toBe('后台子代理已完成：调研');
});

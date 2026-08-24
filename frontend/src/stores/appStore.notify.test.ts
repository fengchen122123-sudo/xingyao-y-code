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

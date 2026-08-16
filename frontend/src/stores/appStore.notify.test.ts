import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, expect, it } from 'vitest';
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

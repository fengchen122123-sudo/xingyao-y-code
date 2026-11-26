import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, expect, it } from 'vitest';
import { useAppStore } from './appStore';

beforeEach(() => setActivePinia(createPinia()));

function makeEvent(type: string, sessionName: string) {
  return { type, data: { session_name: sessionName } } as any;
}

it('非当前窗口完成/待确认/报错时打点，进入会话后清除', () => {
  const store = useAppStore();
  store.currentSessionName = 'session-a';

  store.handleBridgeEvent(makeEvent('turn_finish', 'session-b'));
  expect(store.sessionStatusDots['session-b']).toBe('green');

  store.handleBridgeEvent(makeEvent('permission_request', 'session-c'));
  expect(store.sessionStatusDots['session-c']).toBe('yellow');

  store.handleBridgeEvent(makeEvent('turn_error', 'session-d'));
  expect(store.sessionStatusDots['session-d']).toBe('red');
});

it('当前窗口的事件不打点', () => {
  const store = useAppStore();
  store.currentSessionName = 'session-a';

  store.handleBridgeEvent(makeEvent('turn_finish', 'session-a'));
  expect('session-a' in store.sessionStatusDots).toBe(false);

  store.handleBridgeEvent(makeEvent('permission_request', 'session-a'));
  expect('session-a' in store.sessionStatusDots).toBe(false);

  store.handleBridgeEvent(makeEvent('turn_error', 'session-a'));
  expect('session-a' in store.sessionStatusDots).toBe(false);
});

it('turn_cancelled（手动停止）不打点', () => {
  const store = useAppStore();
  store.currentSessionName = 'session-a';

  store.handleBridgeEvent(makeEvent('turn_cancelled', 'session-b'));
  expect('session-b' in store.sessionStatusDots).toBe(false);
});

it('会话重命名时状态点 key 随迁移', () => {
  const store = useAppStore();
  store.currentSessionName = 'session-a';

  store.handleBridgeEvent(makeEvent('turn_finish', 'session-b'));
  expect(store.sessionStatusDots['session-b']).toBe('green');

  store.handleBridgeEvent({
    type: 'session_renamed',
    data: { old: 'session-b', new: 'session-b2' },
  } as any);
  expect(store.sessionStatusDots['session-b2']).toBe('green');
  expect('session-b' in store.sessionStatusDots).toBe(false);
});

it('端到端：用户在 A，B 跑完出绿点，切到 B 点消失，回 A 后 C 报错出红点', () => {
  const store = useAppStore();
  store.currentSessionName = 'session-a';

  store.sessionGroups = [{
    folder_path: 'GLOBAL', folder_name: '对话', is_global: true, is_current: false,
    sessions: [
      { name: 'session-a', display_name: 'A' },
      { name: 'session-b', display_name: 'B' },
      { name: 'session-c', display_name: 'C' },
    ],
  }] as any;

  store.handleBridgeEvent(makeEvent('turn_finish', 'session-b'));
  expect(store.sessionStatusDots['session-b']).toBe('green');

  store.handleBridgeEvent(makeEvent('permission_request', 'session-c'));
  expect(store.sessionStatusDots['session-c']).toBe('yellow');

  store._clearSessionStatusDot('session-b');
  store.currentSessionName = 'session-b';
  expect('session-b' in store.sessionStatusDots).toBe(false);
  expect(store.sessionStatusDots['session-c']).toBe('yellow');

  store.handleBridgeEvent(makeEvent('turn_error', 'session-c'));
  expect(store.sessionStatusDots['session-c']).toBe('red');

  store.handleBridgeEvent(makeEvent('turn_finish', 'session-b'));
  expect('session-b' in store.sessionStatusDots).toBe(false);
});

it('报错后补发的 turn_finish 不覆盖红点，新回合开始才清掉', () => {
  const store = useAppStore();
  store.currentSessionName = 'session-a';

  // 后端异常收尾：先 turn_error，再由 finish_once 补发 turn_finish
  store.handleBridgeEvent({ type: 'turn_error', data: { session_name: 'session-b', message: '429' } } as any);
  expect(store.sessionStatusDots['session-b']).toBe('red');

  store.handleBridgeEvent(makeEvent('turn_finish', 'session-b'));
  expect(store.sessionStatusDots['session-b']).toBe('red');

  store.handleBridgeEvent(makeEvent('turn_idle', 'session-b'));
  expect(store.sessionStatusDots['session-b']).toBe('red');

  // 下一回合开始：上一轮红点作废，正常完成后打绿点
  store.handleBridgeEvent(makeEvent('turn_start', 'session-b'));
  expect('session-b' in store.sessionStatusDots).toBe(false);

  store.handleBridgeEvent(makeEvent('turn_finish', 'session-b'));
  expect(store.sessionStatusDots['session-b']).toBe('green');
});

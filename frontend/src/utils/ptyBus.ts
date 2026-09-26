// PTY 事件总线：底部内嵌终端的原始输出流转通道。
//
// 后端 pty_data / pty_exit 事件经 appStore.handleBridgeEvent 转发到这里，
// TerminalPane 订阅后直接写 xterm 实例——终端输出是高频原始字节流，
// 进 Pinia 响应式状态会白白触发大量组件更新，因此绕开 store。
// appStore 侧只负责事件分流，不缓存终端内容。

type PtyDataHandler = (text: string) => void;
type PtyExitHandler = (code: number) => void;

const dataHandlers = new Set<PtyDataHandler>();
const exitHandlers = new Set<PtyExitHandler>();

export function emitPtyData(text: string): void {
  for (const handler of [...dataHandlers]) handler(text);
}

export function emitPtyExit(code: number): void {
  for (const handler of [...exitHandlers]) handler(code);
}

/** 订阅终端输出，返回取消订阅函数 */
export function onPtyData(handler: PtyDataHandler): () => void {
  dataHandlers.add(handler);
  return () => dataHandlers.delete(handler);
}

/** 订阅终端进程退出，返回取消订阅函数 */
export function onPtyExit(handler: PtyExitHandler): () => void {
  exitHandlers.add(handler);
  return () => exitHandlers.delete(handler);
}

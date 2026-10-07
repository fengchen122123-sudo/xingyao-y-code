// DAP 调试输出总线：调试控制台的原始输出通道。
//
// 与 ptyBus 同一思路（frontend/src/utils/ptyBus.ts 文件头注释）：适配器输出是
// 高频原始文本，进 Pinia 响应式状态会白白触发大量组件更新，因此绕开 store，
// 由 DebugPane 订阅后直接追加到本地行缓冲。appStore 只做事件分流，不缓存内容。

export interface DapOutputPayload {
  session_id: string;
  category: string;
  text: string;
}

type DapOutputHandler = (payload: DapOutputPayload) => void;

const outputHandlers = new Set<DapOutputHandler>();

export function emitDapOutput(payload: DapOutputPayload): void {
  // 广播发生在 appStore.handleBridgeEvent 的事件泵里，而调试输出是"尽力而为"的
  // 原始流：单个订阅者抛错不该打断桥事件分发，更不该让其他订阅者（DebugPane 之外
  // 以后还会加控制台/日志面板）整段收不到输出。因此逐个隔离异常并记录。
  for (const handler of [...outputHandlers]) {
    try {
      handler(payload);
    } catch (err) {
      console.error('[dapBus] 调试输出订阅者处理失败', err);
    }
  }
}

/** 订阅调试输出，返回取消订阅函数 */
export function onDapOutput(handler: DapOutputHandler): () => void {
  outputHandlers.add(handler);
  return () => outputHandlers.delete(handler);
}

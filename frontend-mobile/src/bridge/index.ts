/**
 * 星瑶 bridge 数据层：复用 lan_server 注入的 mobile-shim 协议。
 *
 * - 桌面（pywebview）与手机（shim）都暴露 window.pywebview.api（RPC Proxy）；
 *   RPC 约定 {"method","args","id"} → {"id","result"}，见 core/lan_server.py。
 * - 事件经 window.onBridgeEvent({type, data}) 派发（广播模型，各端独立收）。
 * - shim 每次连接成功会重新注入 api 并 dispatch 'pywebviewready'：
 *   以此作为"连接恢复"信号；RPC 失败/超时则视为离线（shim 无主动断线回调，
 *   由 store 心跳 RPC 兜底探测）。
 */

type BridgeEvent = { type: string; data?: Record<string, any>; seq?: number };
type Listener = (e: BridgeEvent) => void;

const listeners = new Set<Listener>();

function waitForApi(timeoutMs = 15000): Promise<void> {
  if (window.pywebview?.api) return Promise.resolve();
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      window.removeEventListener('pywebviewready', onReady);
      reject(new Error('桥接未就绪：请确认已扫码配对且桌面端在线'));
    }, timeoutMs);
    const onReady = () => {
      clearTimeout(timer);
      resolve();
    };
    window.addEventListener('pywebviewready', onReady);
    // shim 可能在监听注册前已注入：再检查一次防竞态
    if (window.pywebview?.api) {
      clearTimeout(timer);
      resolve();
    }
  });
}

/** 调用 bridge RPC（位置参数数组，与 shim/lan_server ALLOWED_METHODS 约定一致） */
export async function rpc<T = any>(method: string, ...args: any[]): Promise<T> {
  await waitForApi();
  const api = window.pywebview?.api as any;
  if (!api || typeof api[method] !== 'function') {
    throw new Error(`桥方法不可用: ${method}`);
  }
  return (api[method](...args) as Promise<T>);
}

/** 带 RPC 结果回写的连接状态探测：成功即在线 */
export async function probeOnline(): Promise<boolean> {
  try {
    await rpc('get_token_usage');
    return true;
  } catch {
    return false;
  }
}

// ---- 事件分发：单一 window.onBridgeEvent 扇出到多个 listener ----

function dispatch(e: BridgeEvent) {
  for (const l of [...listeners]) {
    try {
      l(e);
    } catch (err) {
      console.error('bridge listener error', err);
    }
  }
}

// 防止模块热替换/重复初始化时覆盖已有 handler 丢失事件
if (window.onBridgeEvent) {
  const prev = window.onBridgeEvent;
  window.onBridgeEvent = (msg: any) => {
    try { prev(msg); } catch { /* 前一个 handler 的错误不阻断后注册的 */ }
    dispatch(msg);
  };
} else {
  window.onBridgeEvent = dispatch as any;
}

/** 订阅 bridge 事件，返回取消函数 */
export function onEvent(listener: Listener): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

// 配对失效：shim 在 WS 收到 4401（token 过期/被轮换）后派发。
// 扇出为普通 bridge 事件，复用 onEvent 通道交给 store 处理。
window.addEventListener('yc-pair-invalid', () => {
  dispatch({ type: 'pair_invalid', data: {} });
});

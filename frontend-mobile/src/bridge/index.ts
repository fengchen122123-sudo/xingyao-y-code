
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

export async function rpc<T = any>(method: string, ...args: any[]): Promise<T> {
  await waitForApi();
  const api = window.pywebview?.api as any;
  if (!api || typeof api[method] !== 'function') {
    throw new Error(`桥方法不可用: ${method}`);
  }
  return (api[method](...args) as Promise<T>);
}

export async function probeOnline(): Promise<boolean> {
  try {
    await rpc('get_token_usage');
    return true;
  } catch {
    return false;
  }
}

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
    try { prev(msg); } catch { }
    dispatch(msg);
  };
} else {
  window.onBridgeEvent = dispatch as any;
}

export function onEvent(listener: Listener): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

window.addEventListener('yc-pair-invalid', () => {
  dispatch({ type: 'pair_invalid', data: {} });
});

window.addEventListener('yc-initial-state', (event: Event) => {
  const detail = (event as CustomEvent).detail;
  if (detail && typeof detail === 'object') {
    dispatch({ type: 'initial_state_sync', data: detail });
  }
});

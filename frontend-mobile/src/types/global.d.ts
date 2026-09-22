/** pywebview / mobile-shim 全局桥接声明（与 core/lan_server.py 注入协议对齐） */
export {};

declare global {
  interface Window {
    /** pywebview 注入的 RPC 入口；shim 环境为 Proxy，方法调用返回 Promise */
    pywebview?: {
      api: Record<string, (...args: any[]) => Promise<any>>;
    };
    /** lan_server 广播事件入口：{"type","data"} 对象 */
    onBridgeEvent?: ((msg: { type: string; data?: Record<string, any>; seq?: number }) => void) | null;
  }
}

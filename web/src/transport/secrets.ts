/**
 * 凭据和服务器地址的落点。
 *
 * - 安卓（Capacitor）：JWT / refresh / 账号 / 角色只经系统密钥库插件（Android Keystore）；
 *   服务器地址是非敏感配置，走 Preferences。
 * - 浏览器：沿用会话存储，关标签页即失效，行为和 P2 一致。
 *
 * 水位不走这里：`(boot_id, last_seq)` 在安卓只存内存变量，见 gateway-shim.ts。
 */

type SecurePlugin = {
  get(options: { key: string }): Promise<{ value?: string } | undefined>;
  set(options: { key: string; value: string }): Promise<unknown>;
  remove(options: { key: string }): Promise<unknown>;
  clear?(): Promise<unknown>;
};

type CapacitorBridge = {
  isNativePlatform?: () => boolean;
  Plugins?: Record<string, SecurePlugin | undefined>;
  nativePromise?: (plugin: string, method: string, options?: unknown) => Promise<unknown>;
};

/** 安卓偏好里的服务器地址键。 */
export const NATIVE_SERVER_KEY = "xingyao.server";
/** 浏览器沿用会话存储里原来的键，行为不变。 */
export const WEB_SERVER_KEY = "yc-session-server";

function bridge(): CapacitorBridge | null {
  try {
    const cap = (globalThis as { Capacitor?: CapacitorBridge }).Capacitor;
    if (!cap || typeof cap.isNativePlatform !== "function") return null;
    return cap.isNativePlatform() ? cap : null;
  } catch {
    return null;
  }
}

export function isNativeApp(): boolean {
  return bridge() !== null;
}

/**
 * 取插件调用口。
 *
 * 优先用已注册的 JS 代理；SPA 没有打包插件 JS 时，退回 bridge 的 nativePromise 直呼原生方法。
 * 两条路都要求原生侧真的装了该插件，没装就返回 null，调用方回退到浏览器行为。
 */
function plugin(name: string): SecurePlugin | null {
  const cap = bridge();
  if (!cap) return null;
  const registered = cap.Plugins?.[name];
  if (registered) return registered;
  const nativePromise = cap.nativePromise;
  if (typeof nativePromise !== "function") return null;
  const call = (method: string, options: unknown) => nativePromise(name, method, options);
  return {
    get: async (options) => (await call("get", options)) as { value?: string } | undefined,
    set: (options) => call("set", options),
    remove: (options) => call("remove", options),
  };
}

function secureStorage(): SecurePlugin | null {
  return plugin("SecureStoragePlugin");
}

function preferences(): SecurePlugin | null {
  return plugin("Preferences");
}

function sessionStore(): Storage | null {
  try {
    return (globalThis as { sessionStorage?: Storage }).sessionStorage ?? null;
  } catch {
    return null;
  }
}

/**
 * 读凭据。Keystore 密钥失效、备份还原、卸载换机之后这里会读到空串，
 * 上层据此要求重新登录，不去猜旧的 refresh 还能不能用。
 */
export async function secretGet(key: string): Promise<string> {
  const secure = secureStorage();
  if (!secure) return sessionStore()?.getItem(key) || "";
  try {
    const result = await secure.get({ key });
    return result?.value || "";
  } catch {
    return "";
  }
}

/** 写凭据。密钥库写失败也不能把人卡在登录页：本次进程内仍可用，重开要求重新登录。 */
export async function secretSet(key: string, value: string): Promise<void> {
  const secure = secureStorage();
  if (!secure) {
    sessionStore()?.setItem(key, value);
    return;
  }
  try {
    await secure.set({ key, value });
  } catch {
    return;
  }
}

export async function secretRemove(key: string): Promise<void> {
  const secure = secureStorage();
  if (!secure) {
    sessionStore()?.removeItem(key);
    return;
  }
  try {
    await secure.remove({ key });
  } catch {
    return;
  }
}

/** 退出、换号、切服务器时清凭据。密钥库支持整体清理时一并清掉，避免旧账号残留。 */
export async function secretClear(keys: readonly string[]): Promise<void> {
  await Promise.all(keys.map((key) => secretRemove(key)));
  const secure = secureStorage();
  if (secure?.clear) {
    try {
      await secure.clear();
    } catch {
      return;
    }
  }
}

/** 服务器地址：安卓存 Preferences，浏览器存会话存储。 */
export async function serverAddressGet(): Promise<string> {
  const prefs = preferences();
  if (prefs) {
    try {
      const result = await prefs.get({ key: NATIVE_SERVER_KEY });
      return result?.value || "";
    } catch {
      return "";
    }
  }
  return sessionStore()?.getItem(WEB_SERVER_KEY) || "";
}

export async function serverAddressSet(value: string): Promise<void> {
  const prefs = preferences();
  if (prefs) {
    try {
      if (value) await prefs.set({ key: NATIVE_SERVER_KEY, value });
      else await prefs.remove({ key: NATIVE_SERVER_KEY });
    } catch {
      return;
    }
    return;
  }
  const store = sessionStore();
  if (!store) return;
  if (value) store.setItem(WEB_SERVER_KEY, value);
  else store.removeItem(WEB_SERVER_KEY);
}

export async function serverAddressRemove(): Promise<void> {
  await serverAddressSet("");
}

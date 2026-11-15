import { useAuthStore } from "../stores/auth";

let refreshing: Promise<boolean> | null = null;

/** 所有需要账号身份的 REST 请求共用一次 401 刷新，不在调用方另做认证流程。 */
export async function apiFetch(path: string, init: RequestInit = {}): Promise<Response> {
  const auth = useAuthStore();
  const request = (credential: string) => {
    // Headers.entries() 需要 DOM.Iterable，本项目 lib 仅含 DOM，改用 forEach 合并成普通对象。
    const headers: Record<string, string> = {};
    new Headers(init.headers).forEach((value, key) => {
      // 认证头统一由下面显式覆盖，避免大小写归一后出现重复键。
      if (key.toLowerCase() !== "authorization") headers[key] = value;
    });
    headers.Authorization = `Bearer ${credential}`;
    return fetch(new URL(path, auth.server), { ...init, headers });
  };
  let response = await request(auth.credential);
  if (response.status !== 401) return response;
  if (!refreshing) {
    refreshing = auth.refresh().finally(() => { refreshing = null; });
  }
  if (!await refreshing) {
    await auth.logout();
    const { default: router } = await import("../router");
    await router.push("/login");
    return response;
  }
  response = await request(auth.credential);
  return response;
}

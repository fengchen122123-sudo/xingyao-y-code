import { defineStore } from "pinia";
import {
  secretClear,
  secretGet,
  secretSet,
  serverAddressGet,
  serverAddressRemove,
  serverAddressSet,
} from "../transport/secrets";

const SESSION_TOKEN = "yc-session-credential";
const SESSION_ACCOUNT = "yc-session-account";
const SESSION_ROLE = "yc-session-role";
const SESSION_REFRESH = "yc-refresh";
const SECRET_KEYS = [SESSION_TOKEN, SESSION_ACCOUNT, SESSION_ROLE, SESSION_REFRESH] as const;

export const useAuthStore = defineStore("auth", {
  state: () => ({
    server: "",
    accountId: "",
    credential: "",
    role: "user",
    mode: "gateway" as const,
  }),
  getters: {
    signedIn: (state) => Boolean(state.credential && state.server),
  },
  actions: {
    async hydrate() {
      // 服务器地址只在偏好/会话里，凭据在密钥库/会话里，两者分开取。
      this.server = await serverAddressGet();
      this.accountId = await secretGet(SESSION_ACCOUNT);
      this.credential = await secretGet(SESSION_TOKEN);
      this.role = (await secretGet(SESSION_ROLE)) || "user";
    },
    async login(server: string, username: string, password: string) {
      const response = await fetch(new URL("/api/auth/login", server), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username, password }),
      });
      if (!response.ok) throw new Error("登录没有通过");
      const body = await response.json();
      // 没有 access_token 就不能进站：否则 signedIn 靠空串糊弄过去，后面每个请求都 401。
      if (!body || !body.access_token) throw new Error("登录没有通过");
      // 换号或换服务器登录：旧凭据、旧缓存、旧水位都不能留。
      await this.dropPreviousIdentity(server, username);
      await this.remember(server, username, body.access_token, body.role || "user");
      await secretSet(SESSION_REFRESH, body.refresh_token);
    },
    async refresh() {
      const refresh = await secretGet(SESSION_REFRESH);
      if (!this.server || !refresh) return false;
      try {
        const response = await fetch(new URL("/api/auth/refresh", this.server), {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ refresh_token: refresh }),
        });
        if (!response.ok) return false;
        const body = await response.json();
        this.credential = body.access_token;
        this.role = body.role || this.role;
        await secretSet(SESSION_TOKEN, this.credential);
        await secretSet(SESSION_ROLE, this.role);
        if (body.refresh_token) await secretSet(SESSION_REFRESH, body.refresh_token);
        return true;
      } catch {
        return false;
      }
    },
    async remember(server: string, accountId: string, credential: string, role: string) {
      this.server = server;
      this.accountId = accountId;
      this.credential = credential;
      this.role = role;
      await serverAddressSet(server);
      await secretSet(SESSION_ACCOUNT, accountId);
      await secretSet(SESSION_TOKEN, credential);
      await secretSet(SESSION_ROLE, role);
    },
    /** 身份变了才清：同一账号重登不必先把刚写好的凭据抹掉。 */
    async dropPreviousIdentity(server: string, accountId: string): Promise<boolean> {
      const hadIdentity = Boolean(this.credential || this.accountId || this.server);
      const changed = this.server !== server || this.accountId !== accountId;
      if (!hadIdentity || !changed) return false;
      await this.clearLocalSession();
      return true;
    },
    /** 关连接、清缓存、删凭据和水位。水位清理由 shim.close() 负责（安卓本来只有内存）。 */
    async clearLocalSession() {
      const { useAppStore } = await import("./app");
      const { useDeviceStore } = await import("./devices");
      useAppStore().disconnect();
      useDeviceStore().disconnect();
      this.credential = "";
      this.accountId = "";
      this.role = "user";
      await secretClear(SECRET_KEYS);
    },
    async logout() {
      const refresh = await secretGet(SESSION_REFRESH);
      const { useAppStore } = await import("./app");
      const { useDeviceStore } = await import("./devices");
      try {
        useAppStore().disconnect();
        useDeviceStore().disconnect();
        if (this.mode === "gateway" && this.credential) {
          await fetch(new URL("/api/auth/logout", this.server), {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
              Authorization: `Bearer ${this.credential}`,
            },
            body: JSON.stringify({ refresh_token: refresh }),
          }).catch(() => undefined);
        }
      } finally {
        this.credential = "";
        this.accountId = "";
        this.server = "";
        this.role = "user";
        await Promise.all([secretClear(SECRET_KEYS), serverAddressRemove()]);
      }
    },
  },
});

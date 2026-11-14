import { defineStore } from "pinia";

const SESSION_TOKEN = "yc-session-credential";
const SESSION_SERVER = "yc-session-server";
const SESSION_ACCOUNT = "yc-session-account";

export const useAuthStore = defineStore("auth", {
  state: () => ({
    server: sessionStorage.getItem(SESSION_SERVER) || "",
    accountId: sessionStorage.getItem(SESSION_ACCOUNT) || "",
    credential: sessionStorage.getItem(SESSION_TOKEN) || "",
    role: "user",
    mode: "gateway" as const,
  }),
  getters: {
    signedIn: (state) => Boolean(state.credential && state.server),
  },
  actions: {
    async login(server: string, username: string, password: string) {
      const response = await fetch(new URL("/api/auth/login", server), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username, password }),
      });
      if (!response.ok) throw new Error("登录没有通过");
      const body = await response.json();
      this.remember(server, username, body.access_token, body.role || "user");
      sessionStorage.setItem("yc-refresh", body.refresh_token);
    },
    remember(server: string, accountId: string, credential: string, role: string) {
      this.server = server;
      this.accountId = accountId;
      this.credential = credential;
      this.role = role;
      sessionStorage.setItem(SESSION_SERVER, server);
      sessionStorage.setItem(SESSION_ACCOUNT, accountId);
      sessionStorage.setItem(SESSION_TOKEN, credential);
    },
    async logout() {
      const refresh = sessionStorage.getItem("yc-refresh") || "";
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
      this.credential = "";
      sessionStorage.removeItem(SESSION_TOKEN);
      sessionStorage.removeItem("yc-refresh");
    },
  },
});

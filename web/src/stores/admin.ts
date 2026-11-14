import { defineStore } from "pinia";
import { useAuthStore } from "./auth";

async function adminFetch(path: string, method = "GET", body?: unknown) {
  const auth = useAuthStore();
  const response = await fetch(new URL(path, auth.server), {
    method,
    headers: {
      Authorization: `Bearer ${auth.credential}`,
      "Content-Type": "application/json",
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) throw new Error("管理接口没有通过");
  return response.json();
}

export const useAdminStore = defineStore("admin", {
  state: () => ({
    accounts: [] as { id: string; username: string; role: string; disabled: number }[],
    devices: [] as { id: string; account_id: string; name: string; revoked: number }[],
    workers: [] as { account_id: string; container_name: string; desired_state: string }[],
    freshToken: "",
    usage: [] as { account_id: string; day: string; model: string; input_tokens: number; output_tokens: number; turns: number }[],
  }),
  actions: {
    async loadAccounts() {
      const body = await adminFetch("/api/admin/accounts");
      this.accounts = body.accounts || [];
    },
    async createAccount(username: string, password: string) {
      await adminFetch("/api/admin/accounts", "POST", { username, password });
      await this.loadAccounts();
    },
    async disableAccount(accountId: string) {
      await adminFetch(`/api/admin/accounts/${accountId}`, "PATCH", { disabled: true });
      await this.loadAccounts();
    },
    async loadDevices() {
      const body = await adminFetch("/api/admin/devices");
      this.devices = body.devices || [];
    },
    async createDevice(accountId: string, name: string) {
      const body = await adminFetch(`/api/admin/devices?account_id=${encodeURIComponent(accountId)}`, "POST", { name });
      this.freshToken = body.token || "";
      await this.loadDevices();
    },
    async revokeDevice(deviceId: string) {
      await adminFetch(`/api/admin/devices/${deviceId}`, "DELETE");
      await this.loadDevices();
    },
    async loadWorkers() {
      const body = await adminFetch("/api/admin/workers");
      this.workers = body.workers || [];
    },
    async controlWorker(accountId: string, action: "start" | "stop") {
      await adminFetch(`/api/admin/workers/${accountId}?action=${action}`, "POST");
      await this.loadWorkers();
    },
    async loadUsage() {
      const body = await adminFetch("/api/admin/usage");
      this.usage = body.usage || [];
    },
  },
});

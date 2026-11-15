import { defineStore } from "pinia";
import { apiFetch } from "../transport/api";

async function adminFetch(path: string, method = "GET", body?: unknown) {
  const response = await apiFetch(path, {
    method,
    headers: { "Content-Type": "application/json" },
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
    models: [] as { name: string; base_url: string; api_key: string; model_id: string; model_name: string; litellm_model: string }[],
    activeModelId: "",
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
    async resetPassword(accountId: string, password: string) {
      await adminFetch(`/api/admin/accounts/${accountId}`, "PATCH", { password });
      await this.loadAccounts();
    },
    async loadModels() {
      const body = await adminFetch("/api/admin/models");
      this.activeModelId = body.active_model_id || "";
      this.models = (body.providers || []).flatMap((provider: { name?: string; base_url?: string; api_key?: string; models?: { id?: string; name?: string; litellm_model?: string }[] }) => {
        const model = provider.models?.[0] || {};
        return [{
          name: provider.name || "",
          base_url: provider.base_url || "",
          api_key: provider.api_key || "",
          model_id: model.id || "",
          model_name: model.name || "",
          litellm_model: model.litellm_model || "",
        }];
      });
    },
    async saveModels(providers: { name: string; base_url: string; api_key: string; model_id: string; model_name: string; litellm_model: string }[], activeModelId: string) {
      await adminFetch("/api/admin/models", "PUT", { providers, active_model_id: activeModelId });
      await this.loadModels();
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
    async rotateDevice(deviceId: string) {
      const body = await adminFetch(`/api/admin/devices/${deviceId}/rotate`, "POST");
      this.freshToken = body.token || "";
      await this.loadDevices();
    },
    async loadWorkers() {
      const body = await adminFetch("/api/admin/workers");
      this.workers = body.workers || [];
    },
    async controlWorker(accountId: string, action: "start" | "stop" | "restart") {
      await adminFetch(`/api/admin/workers/${accountId}?action=${action}`, "POST");
      await this.loadWorkers();
    },
    async loadUsage() {
      const body = await adminFetch("/api/admin/usage");
      this.usage = body.usage || [];
    },
  },
});

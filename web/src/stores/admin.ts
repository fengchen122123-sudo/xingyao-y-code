import { defineStore } from "pinia";
import { useAuthStore } from "./auth";

async function adminGet(path: string) {
  const auth = useAuthStore();
  const response = await fetch(new URL(path, auth.server), {
    headers: { Authorization: `Bearer ${auth.credential}` },
  });
  if (!response.ok) throw new Error("管理接口没有通过");
  return response.json();
}

export const useAdminStore = defineStore("admin", {
  state: () => ({
    accounts: [] as { id: string; username: string; role: string; disabled: number }[],
    usage: [] as { account_id: string; day: string; model: string; input_tokens: number; output_tokens: number; turns: number }[],
  }),
  actions: {
    async loadAccounts() {
      const body = await adminGet("/api/admin/accounts");
      this.accounts = body.accounts || [];
    },
    async loadUsage() {
      const body = await adminGet("/api/admin/usage");
      this.usage = body.usage || [];
    },
  },
});

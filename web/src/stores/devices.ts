import { defineStore } from "pinia";
import { useAuthStore } from "./auth";

export const useDeviceStore = defineStore("devices", {
  state: () => ({
    devices: [] as { id: string; name: string; online: boolean }[],
  }),
  actions: {
    async load() {
      const auth = useAuthStore();
      const response = await fetch(new URL("/api/devices", auth.server), {
        headers: { Authorization: `Bearer ${auth.credential}` },
      });
      if (!response.ok) return;
      const body = await response.json();
      this.devices = body.devices || [];
    },
  },
});

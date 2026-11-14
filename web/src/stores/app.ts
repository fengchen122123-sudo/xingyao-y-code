import { defineStore } from "pinia";
import { GatewayShim } from "../transport/gateway-shim";
import { useAuthStore } from "./auth";

export interface ChatMessage {
  id: string;
  role: "user" | "agent";
  text: string;
}

export const useAppStore = defineStore("app", {
  state: () => ({
    messages: [] as ChatMessage[],
    sessions: [] as { name: string }[],
    approvals: [] as { id: string; title: string; path: string }[],
    previewUrl: "",
    status: "未连接",
    shim: null as GatewayShim | null,
  }),
  actions: {
    connect() {
      const auth = useAuthStore();
      if (!auth.signedIn) return;
      const capacitor = Boolean((window as unknown as { Capacitor?: { isNativePlatform?: () => boolean } }).Capacitor?.isNativePlatform?.());
      const shim = new GatewayShim({
        server: auth.server,
        accountId: auth.accountId,
        profile: "agent",
        credential: auth.credential,
        capacitor,
      });
      shim.onEvent((frame) => this.apply(frame));
      shim.connect();
      this.shim = shim;
      this.status = "正在连接";
    },
    connectDesktop(deviceId: string) {
      const auth = useAuthStore();
      const capacitor = Boolean((window as unknown as { Capacitor?: { isNativePlatform?: () => boolean } }).Capacitor?.isNativePlatform?.());
      const shim = new GatewayShim({
        server: auth.server,
        accountId: auth.accountId,
        profile: "desktop",
        credential: auth.credential,
        deviceId,
        capacitor,
      });
      shim.onEvent((frame) => this.apply(frame));
      shim.connect();
      this.shim = shim;
      this.status = "正在连接这台电脑";
    },
    apply(frame: { type: string; data?: unknown }) {
      const data = (frame.data || {}) as Record<string, unknown>;
      if (frame.type === "hello") return;
      if (frame.type === "token_delta" && typeof data.delta === "string") {
        const last = this.messages[this.messages.length - 1];
        if (last && last.role === "agent") last.text += data.delta;
        return;
      }
      if (frame.type === "permission_request") {
        this.approvals.push({
          id: String(data.id || ""),
          title: String(data.title || "需要确认"),
          path: String(data.path || ""),
        });
      }
      if (frame.type === "computer_observation" && typeof data.preview_url === "string") {
        this.previewUrl = data.preview_url;
      }
      if (frame.type === "yc_snapshot") this.status = "已同步";
    },
    async send(text: string) {
      this.messages.push({ id: String(Date.now()), role: "user", text });
      this.messages.push({ id: String(Date.now() + 1), role: "agent", text: "" });
      if (!this.shim) {
        this.status = "还没有接上星瑶";
        return;
      }
      await this.shim.rpc("send_user_message", { text });
    },
    disconnect() {
      this.shim?.close();
      this.shim = null;
      this.messages = [];
      this.approvals = [];
      this.previewUrl = "";
      this.status = "未连接";
    },
  },
});

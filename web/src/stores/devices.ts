import { defineStore } from "pinia";
import { markRaw } from "vue";
import { apiFetch } from "../transport/api";
import { useAuthStore } from "./auth";

interface DeviceFrame {
  type?: string;
  data?: {
    device_id?: string;
    online?: boolean;
    boot_id?: string;
    connection_generation?: number;
    oldest_seq?: number | null;
    newest_seq?: number | null;
    reason?: string;
  };
}

export interface DeviceRow {
  id: string;
  name: string;
  online: boolean;
  /** 在线/离线/正在整态重来：device_reset 与 resync_required 都要让页面看出来。 */
  state: "online" | "offline" | "resyncing";
  generation?: number;
  reason?: string;
}

export const useDeviceStore = defineStore("devices", {
  state: () => ({
    devices: [] as DeviceRow[],
    socket: null as WebSocket | null,
    /** 断线重连的退避次数，测试用它断言确实发起了重连。 */
    retries: 0,
    timer: null as ReturnType<typeof setTimeout> | null,
  }),
  actions: {
    async load() {
      const response = await apiFetch("/api/devices");
      if (!response.ok) return;
      const body = await response.json();
      const rows = (body.devices || []) as { id: string; name: string; online?: boolean }[];
      this.devices = rows.map((row) => ({
        id: row.id,
        name: row.name,
        online: Boolean(row.online),
        state: row.online ? "online" : "offline",
      }));
      this.watch();
    },
    watch() {
      if (this.socket) return;
      const auth = useAuthStore();
      if (!auth.credential) return;
      const url = new URL("/ws/devices", auth.server);
      url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
      // markRaw：socket 不能进响应式代理，否则 onclose 里的身份比较永远不相等，
      // 旧连接会一直挂在 this.socket 上，退避重连一条也发不出去。
      const socket = markRaw(new WebSocket(url));
      this.socket = socket;
      socket.onopen = () => {
        this.retries = 0;
        socket.send(JSON.stringify({ jwt: auth.credential }));
      };
      socket.onmessage = (event) => this.apply(String(event.data));
      socket.onclose = () => {
        if (this.socket !== socket) return;
        this.socket = null;
        // 手机端常年挂着这一页：断线要自己退避重连，不能等人手动刷新。
        this.scheduleReconnect();
      };
    },
    scheduleReconnect() {
      if (this.timer) return;
      if (!useAuthStore().credential) return;
      const delay = Math.min(1000 * 2 ** this.retries, 30_000);
      this.retries += 1;
      this.timer = setTimeout(() => {
        this.timer = null;
        this.watch();
      }, delay);
    },
    apply(raw: string) {
      let frame: DeviceFrame;
      try {
        frame = JSON.parse(raw) as DeviceFrame;
      } catch {
        return;
      }
      const id = frame.data?.device_id;
      if (!id) return;
      const device = this.devices.find((item) => item.id === id);
      if (frame.type === "device_status") {
        if (!device) return;
        if (typeof frame.data?.online === "boolean") {
          device.online = frame.data.online;
          device.state = frame.data.online ? "online" : "offline";
        }
        return;
      }
      if (frame.type === "device_online") {
        if (!device) return;
        // 网关重开连接：DESKTOP 重新上线，窗口内补发随后由 replay 送来。
        device.online = true;
        device.state = "online";
        device.reason = "";
        if (typeof frame.data?.connection_generation === "number") {
          device.generation = frame.data.connection_generation;
        }
        return;
      }
      if (frame.type === "device_reset" || frame.type === "resync_required") {
        if (!device) return;
        // GUI 重启换了 boot_id，或窗口断裂：App 要整态重来，页面据此提示「正在同步」。
        device.state = "resyncing";
        device.reason = String(frame.data?.reason || (frame.type === "device_reset" ? "boot" : "gap"));
        return;
      }
    },
    disconnect() {
      if (this.timer) {
        clearTimeout(this.timer);
        this.timer = null;
      }
      this.retries = 0;
      this.socket?.close();
      this.socket = null;
    },
  },
});

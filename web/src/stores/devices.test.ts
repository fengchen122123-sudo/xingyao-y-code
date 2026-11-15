import { beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { useDeviceStore } from "./devices";
import { useAuthStore } from "../stores/auth";

class Socket {
  static OPEN = 1;
  close = vi.fn();
  onopen = null as null | (() => void);
  onmessage = null as null | ((event: { data: string }) => void);
  onclose = null as null | (() => void);
  sent: string[] = [];
  constructor(_url: string) {}
  send(raw: string) { this.sent.push(raw); }
  message(frame: unknown) { this.onmessage?.({ data: JSON.stringify(frame) }); }
}

function setup() {
  setActivePinia(createPinia());
  vi.stubGlobal("WebSocket", Socket);
}

describe("device store", () => {
  beforeEach(() => { setup(); });

  it("retries REST through the shared API helper and disconnects its socket", async () => {
    const auth = useAuthStore();
    auth.server = "https://example.test";
    auth.credential = "jwt";
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, status: 200, json: async () => ({ devices: [{ id: "d", online: true }] }) }));
    const devices = useDeviceStore();
    await devices.load();
    expect(devices.socket).not.toBeNull();
    const socket = devices.socket!;
    devices.disconnect();
    expect(socket.close).toHaveBeenCalledOnce();
    expect(devices.socket).toBeNull();
    vi.unstubAllGlobals();
  });

  it("tracks device_status, device_online and device_reset without guessing online", async () => {
    const auth = useAuthStore();
    auth.server = "https://example.test";
    auth.credential = "jwt";
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ devices: [{ id: "d", name: "台式机", online: true }] }),
    }));
    const devices = useDeviceStore();
    await devices.load();
    const socket = devices.socket as unknown as Socket;

    socket.message({ type: "device_status", data: { device_id: "d", online: false } });
    expect(devices.devices[0].online).toBe(false);
    expect(devices.devices[0].state).toBe("offline");

    socket.message({ type: "device_online", data: { device_id: "d", connection_generation: 3 } });
    expect(devices.devices[0].online).toBe(true);
    expect(devices.devices[0].generation).toBe(3);

    socket.message({ type: "device_reset", data: { device_id: "d", boot_id: "b2" } });
    expect(devices.devices[0].state).toBe("resyncing");
    expect(devices.devices[0].reason).toBe("boot");

    socket.message({ type: "resync_required", data: { device_id: "d", reason: "overflow" } });
    expect(devices.devices[0].reason).toBe("overflow");

    // 未在列表里的设备帧不能凭空造出一条记录。
    socket.message({ type: "device_status", data: { device_id: "ghost", online: true } });
    expect(devices.devices).toHaveLength(1);
    vi.unstubAllGlobals();
  });

  it("reconnects with backoff after the socket drops", async () => {
    vi.useFakeTimers();
    const auth = useAuthStore();
    auth.server = "https://example.test";
    auth.credential = "jwt";
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, status: 200, json: async () => ({ devices: [] }) }));
    const devices = useDeviceStore();
    await devices.load();
    const socket = devices.socket as unknown as Socket;
    socket.onclose?.();
    expect(devices.socket).toBeNull();
    expect(devices.retries).toBe(1);
    vi.advanceTimersByTime(1000);
    expect(devices.socket).not.toBeNull();
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });
});

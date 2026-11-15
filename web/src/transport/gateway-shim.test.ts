import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { GatewayShim } from "./gateway-shim";

class FakeSocket {
  static instances: FakeSocket[] = [];
  static OPEN = 1;
  readyState = FakeSocket.OPEN;
  sent: string[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  onclose: ((event: { code: number }) => void) | null = null;
  constructor(_url: string) { FakeSocket.instances.push(this); }
  send(raw: string) { this.sent.push(raw); }
  close() { this.onclose?.({ code: 1000 }); }
  message(frame: unknown) { this.onmessage?.({ data: JSON.stringify(frame) }); }
}

function makeShim() {
  const shim = new GatewayShim({ server: "https://example.test", accountId: "a", profile: "agent", credential: "jwt", capacitor: true });
  const events: { type: string; data?: unknown }[] = [];
  shim.onEvent((frame) => events.push(frame));
  shim.connect();
  const socket = FakeSocket.instances.at(-1)!;
  return { shim, events, socket };
}

function respondInitial(socket: FakeSocket, state: Record<string, unknown> = { messages: [] }) {
  const request = socket.sent.map((raw) => JSON.parse(raw)).find((frame) => frame.method === "get_initial_state");
  if (!request) throw new Error("missing get_initial_state request");
  socket.message({ id: request.id, result: state });
}

describe("GatewayShim protocol", () => {
  beforeEach(() => {
    FakeSocket.instances = [];
    vi.stubGlobal("WebSocket", FakeSocket);
    vi.stubGlobal("window", { setTimeout, localStorage: { getItem: vi.fn(), setItem: vi.fn(), removeItem: vi.fn() } });
  });
  afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); });

  it("deduplicates frames by boot and sequence, including replay wrappers", () => {
    const { shim, events, socket } = makeShim();
    socket.message({ type: "hello", data: { boot_id: "b1" } });
    const first = { type: "token_delta", seq: 2, data: { boot_id: "b1", text: "a" } };
    socket.message({ type: "replay", frames: [JSON.stringify(first), first, { type: "token_delta", seq: 1, data: { boot_id: "b0", text: "old" } }] });
    socket.message(first);
    socket.message({ type: "token_delta", seq: 3, data: { boot_id: "b2", text: "wrong boot" } });
    expect(events.map((event) => event.type)).toEqual(["token_delta"]);
    expect(shim.lastSeq).toBe(2);
    shim.close();
  });

  it("waits for hello before reporting transport readiness", async () => {
    const { shim, socket } = makeShim();
    let ready = false;
    const waiting = shim.whenReady().then(() => { ready = true; });
    await Promise.resolve();
    expect(ready).toBe(false);
    socket.message({ type: "hello", data: { boot_id: "b1" } });
    await waiting;
    expect(ready).toBe(true);
    shim.close();
  });

  it("fetches initial state for a legacy service before releasing business frames and readiness", async () => {
    const { shim, events, socket } = makeShim();
    let ready = false;
    const waiting = shim.whenReady().then(() => { ready = true; });
    socket.message({ type: "worker_starting", data: {} });
    expect(socket.sent.some((raw) => raw.includes("get_initial_state"))).toBe(true);
    expect(events).toEqual([]);
    await Promise.resolve();
    expect(ready).toBe(false);
    respondInitial(socket, { current_session: "s" });
    await waiting;
    expect(events.map((event) => event.type)).toEqual(["yc_snapshot", "worker_starting"]);
    expect(ready).toBe(true);
    shim.close();
  });

  it("handles resync controls and deduplicates held frames when the snapshot lands", async () => {
    const { shim, events, socket } = makeShim();
    socket.message({ type: "hello", data: { boot_id: "b1" } });
    socket.message({ type: "token_delta", seq: 4, data: { boot_id: "b1", text: "old" } });
    socket.message({ type: "resync_required", data: { boot_id: "b1", resync_required: true } });
    const live = { type: "token_delta", seq: 5, data: { boot_id: "b1", text: "new" } };
    socket.message(live);
    socket.message({ type: "token_delta", seq: 4, data: { boot_id: "b1", text: "duplicate" } });
    respondInitial(socket);
    await Promise.resolve();
    socket.message(live);
    expect(events.filter((event) => event.type === "token_delta")).toHaveLength(2);
    expect(events.filter((event) => event.type === "yc_snapshot")).toHaveLength(1);
    shim.close();
  });

  it("rejects timed out and closed RPCs", async () => {
    vi.useFakeTimers();
    const { shim, socket } = makeShim();
    const timeout = shim.rpc("send_user_message", {});
    const rejection = expect(timeout).rejects.toThrow("超时");
    await vi.advanceTimersByTimeAsync(30_000);
    await rejection;
    const closed = shim.rpc("send_user_message", {});
    shim.close();
    await expect(closed).rejects.toThrow("关闭");
  });

  it("resumes from the stored watermark and drops frames left over from the previous boot", () => {
    const store = new Map<string, string>([
      ["yc-watermark:https://example.test|a|agent", JSON.stringify({ bootId: "boot-1", lastSeq: 7 })],
    ]);
    vi.stubGlobal("window", {
      setTimeout,
      localStorage: {
        getItem: (key: string) => store.get(key) ?? null,
        setItem: (key: string, value: string) => void store.set(key, value),
        removeItem: (key: string) => void store.delete(key),
      },
    });
    const shim = new GatewayShim({ server: "https://example.test", accountId: "a", profile: "agent", credential: "jwt", capacitor: false });
    const events: { type: string; data?: unknown }[] = [];
    shim.onEvent((frame) => events.push(frame));
    shim.connect();
    const socket = FakeSocket.instances.at(-1)!;
    socket.onopen?.();
    expect(JSON.parse(socket.sent[0])).toEqual({ jwt: "jwt", last_boot_id: "boot-1", last_seq: 7 });

    socket.message({ type: "hello", data: { boot_id: "boot-2" } });
    socket.message({ type: "token_delta", seq: 8, data: { boot_id: "boot-1", text: "restart" } });
    socket.message({ type: "token_delta", seq: 9, data: { boot_id: "boot-2", text: "fresh" } });
    expect(events.map((event) => event.type)).toEqual(["token_delta"]);
    expect(shim.bootId).toBe("boot-2");
    expect(JSON.parse(store.get("yc-watermark:https://example.test|a|agent") || "{}")).toEqual({ bootId: "boot-2", lastSeq: 9 });
    shim.close();
  });

  it("drops frames at or below the pre-resync watermark when the snapshot lands", async () => {
    const { shim, events, socket } = makeShim();
    socket.message({ type: "hello", data: { boot_id: "b1" } });
    socket.message({ type: "token_delta", seq: 4, data: { boot_id: "b1", text: "seen" } });
    socket.message({ type: "resync_required", data: { boot_id: "b1", resync_required: true } });
    socket.message({ type: "token_delta", seq: 4, data: { boot_id: "b1", text: "duplicate" } });
    socket.message({ type: "token_delta", seq: 5, data: { boot_id: "b1", text: "live" } });
    respondInitial(socket);
    await new Promise((resolve) => setTimeout(resolve, 0));
    const texts = events.filter((event) => event.type === "token_delta").map((event) => (event.data as { text: string }).text);
    expect(texts).toEqual(["seen", "live"]);
    shim.close();
  });

  it("treats relay device_reset as a whole-state restart with a cleared watermark", async () => {
    const { shim, events, socket } = makeShim();
    socket.message({ type: "hello", data: { boot_id: "b1" } });
    socket.message({ type: "token_delta", seq: 7, data: { boot_id: "b1", text: "old" } });
    socket.message({ type: "device_reset", data: { device_id: "d", boot_id: "b2" } });
    expect(socket.sent.some((raw) => raw.includes("get_initial_state"))).toBe(true);
    expect(shim.bootId).toBe("b2");
    expect(shim.lastSeq).toBe(0);
    // 新 boot 的 seq 从 1 重新开始：旧 boot 的 7 不能把新帧当重复帧压掉。
    socket.message({ type: "token_delta", seq: 1, data: { boot_id: "b2", text: "fresh" } });
    respondInitial(socket);
    await new Promise((resolve) => setTimeout(resolve, 0));
    // 旧 boot 的迟到帧仍然要被丢掉。
    socket.message({ type: "token_delta", seq: 8, data: { boot_id: "b1", text: "stale" } });
    const texts = events.filter((event) => event.type === "token_delta").map((event) => (event.data as { text: string }).text);
    expect(texts).toEqual(["old", "fresh"]);
    expect(shim.lastSeq).toBe(1);
    shim.close();
  });

  it("refreshes once on unauthorized closes and reconnects with the rotated credential", async () => {
    vi.useFakeTimers();
    vi.stubGlobal("window", {
      setTimeout,
      localStorage: { getItem: vi.fn(), setItem: vi.fn(), removeItem: vi.fn() },
    });
    const onUnauthorized = vi.fn().mockResolvedValue(true);
    let credential = "jwt-old";
    const shim = new GatewayShim({
      server: "https://example.test",
      accountId: "a",
      profile: "agent",
      credential,
      refreshCredential: () => credential,
      capacitor: true,
      onUnauthorized,
    });
    shim.connect();
    const first = FakeSocket.instances.at(-1)!;
    first.onclose?.({ code: 4401 });
    await vi.advanceTimersByTimeAsync(2000);
    expect(onUnauthorized).toHaveBeenCalledTimes(1);
    const second = FakeSocket.instances.at(-1)!;
    expect(second).not.toBe(first);
    credential = "jwt-new";
    second.onopen?.();
    expect(JSON.parse(second.sent[0]).jwt).toBe("jwt-new");

    onUnauthorized.mockResolvedValue(false);
    second.onclose?.({ code: 4401 });
    await vi.advanceTimersByTimeAsync(2000);
    expect(FakeSocket.instances).toHaveLength(2);
    shim.close();
  });
});

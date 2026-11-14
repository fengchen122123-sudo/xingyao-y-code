import { decideFrame, keepLiveFrame, shouldPersistWatermark, watermarkKey, type Watermark } from "./epoch";
import { endpointFor, type AgentProfile } from "./profile";

export interface ShimOptions {
  server: string;
  accountId: string;
  profile: AgentProfile;
  credential: string;
  deviceId?: string;
  capacitor: boolean;
}

/** 首帧只带 JWT。工人令牌由网关在服务器侧送出，不进入浏览器。 */
export class GatewayShim {
  bootId = "";
  lastSeq = 0;
  private socket: WebSocket | null = null;
  private pending = new Map<number, { resolve: (value: unknown) => void; reject: (error: Error) => void }>();
  private seq = 0;
  private sawHello = false;
  private holding = false;
  private pausedSeq = 0;
  private held: { type?: string; seq?: number; data?: { boot_id?: string; turn_id?: string } }[] = [];
  private listeners = new Set<(frame: { type: string; data?: unknown }) => void>();

  constructor(private options: ShimOptions) {
    const saved = this.readWatermark();
    if (saved) {
      this.bootId = saved.bootId;
      this.lastSeq = saved.lastSeq;
    }
  }

  onEvent(listener: (frame: { type: string; data?: unknown }) => void): void {
    this.listeners.add(listener);
  }

  connect(): void {
    const url = endpointFor(this.options.server, this.options.profile);
    this.socket = new WebSocket(url);
    this.socket.onopen = () => {
      const frame: Record<string, unknown> = {};
      frame.jwt = this.options.credential;
      if (this.options.profile === "desktop") frame.device_id = this.options.deviceId || "";
      if (this.bootId) {
        frame.last_boot_id = this.bootId;
        frame.last_seq = this.lastSeq;
      }
      this.socket?.send(JSON.stringify(frame));
    };
    this.socket.onmessage = (event) => this.receive(String(event.data));
  }

  close(): void {
    this.pending.forEach((item) => item.reject(new Error("连接已关闭")));
    this.pending.clear();
    this.socket?.close();
    this.socket = null;
    this.bootId = "";
    this.lastSeq = 0;
    this.forgetWatermark();
  }

  rpc(method: string, args: unknown): Promise<unknown> {
    return new Promise((resolve, reject) => {
      if (!this.socket || this.socket.readyState !== WebSocket.OPEN) {
        reject(new Error("连接未就绪"));
        return;
      }
      const id = ++this.seq;
      this.pending.set(id, { resolve, reject });
      this.socket.send(JSON.stringify({ method, args, id }));
    });
  }

  private receive(raw: string): void {
    const msg = JSON.parse(raw) as { id?: number; result?: unknown; error?: string; type?: string; data?: { boot_id?: string; resync_required?: boolean }; seq?: number };
    if (typeof msg.id !== "undefined") {
      const pending = this.pending.get(msg.id);
      if (!pending) return;
      this.pending.delete(msg.id);
      if (msg.error) pending.reject(new Error(msg.error));
      else pending.resolve(msg.result);
      return;
    }
    const decision = decideFrame(msg, this.sawHello);
    if (decision.kind === "hello" || decision.kind === "reset") {
      this.sawHello = true;
      this.bootId = decision.bootId || this.bootId;
      if (decision.kind === "reset") {
        this.pausedSeq = this.lastSeq;
        this.lastSeq = 0;
        this.holding = decision.resync;
        this.held = [];
      }
      this.writeWatermark();
      if (decision.resync) void this.rpc("get_initial_state", []).then((state) => {
        this.emit({ type: "yc_snapshot", data: state });
        this.releaseHeld();
      });
      return;
    }
    if (this.holding && msg.type) {
      this.held.push(msg);
      return;
    }
    if (decision.kind === "legacy") this.sawHello = false;
    if (typeof msg.seq === "number" && msg.seq > this.lastSeq) {
      this.lastSeq = msg.seq;
      this.writeWatermark();
    }
    if (msg.type) this.emit({ type: msg.type, data: msg.data });
  }

  private releaseHeld(): void {
    const finished = new Set<string>();
    const pending = this.held;
    this.held = [];
    this.holding = false;
    pending.forEach((frame) => {
      if (keepLiveFrame(frame, this.pausedSeq, this.bootId, finished) && frame.type) {
        this.emit({ type: frame.type, data: frame.data });
      }
    });
  }

  private emit(frame: { type: string; data?: unknown }): void {
    this.listeners.forEach((listener) => listener(frame));
  }

  private storageKey(): string {
    return watermarkKey(this.options.server, this.options.accountId, this.options.profile);
  }

  private readWatermark(): Watermark | null {
    if (!shouldPersistWatermark(this.options.capacitor)) return null;
    const raw = window.localStorage.getItem(this.storageKey());
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Watermark;
    return { bootId: parsed.bootId || "", lastSeq: Number(parsed.lastSeq) || 0 };
  }

  private writeWatermark(): void {
    if (!shouldPersistWatermark(this.options.capacitor) || !this.bootId) return;
    window.localStorage.setItem(this.storageKey(), JSON.stringify({ bootId: this.bootId, lastSeq: this.lastSeq }));
  }

  private forgetWatermark(): void {
    if (!shouldPersistWatermark(this.options.capacitor)) return;
    window.localStorage.removeItem(this.storageKey());
  }
}

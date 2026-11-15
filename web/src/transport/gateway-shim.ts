import { decideFrame, keepLiveFrame, shouldPersistWatermark, watermarkKey, type Watermark } from "./epoch";
import { endpointFor, type AgentProfile } from "./profile";

export interface ShimOptions {
  server: string;
  accountId: string;
  profile: AgentProfile;
  credential: string;
  refreshCredential?: () => string;
  deviceId?: string;
  capacitor: boolean;
  onUnauthorized?: () => Promise<boolean>;
}

type Frame = { id?: number; result?: unknown; error?: string; type?: string; data?: { boot_id?: string; turn_id?: string; resync_required?: boolean; [key: string]: unknown }; seq?: number; frames?: unknown[] };
type Pending = { resolve: (value: unknown) => void; reject: (error: Error) => void; timer: ReturnType<typeof setTimeout> };
type ReadyWaiter = { resolve: () => void; reject: (error: Error) => void };

const RECONNECT_DELAY = 2000;
const RPC_TIMEOUT = 30_000;
const PROBE_TIMEOUT = 5000;

/** 首帧只带 JWT。工人令牌由网关在服务器侧送出，不进入浏览器。 */
export class GatewayShim {
  bootId = "";
  lastSeq = 0;
  private socket: WebSocket | null = null;
  private pending = new Map<number, Pending>();
  private seq = 0;
  private sawHello = false;
  private holding = false;
  private pausedSeq = 0;
  private held: Frame[] = [];
  private listeners = new Set<(frame: { type: string; data?: unknown }) => void>();
  private readyWaiters = new Set<ReadyWaiter>();
  private connectionReady = false;
  private generation = 0;
  private userClosed = false;
  /** 安卓水位只在内存：进程一死就没了，重开必须走 get_initial_state（设计 §10.7）。 */
  private readonly persistWatermark: boolean;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private probing = false;
  private suspended = false;
  private detachLifecycle: (() => void) | null = null;

  constructor(private options: ShimOptions) {
    this.persistWatermark = shouldPersistWatermark(this.options.capacitor);
    if (!this.persistWatermark) this.dropStoredWatermark();
    const saved = this.readWatermark();
    if (saved) {
      this.bootId = saved.bootId;
      this.lastSeq = saved.lastSeq;
    }
    this.attachLifecycle();
  }

  onEvent(listener: (frame: { type: string; data?: unknown }) => void): void {
    this.listeners.add(listener);
  }

  whenReady(): Promise<void> {
    if (this.connectionReady) return Promise.resolve();
    if (this.userClosed) return Promise.reject(new Error("连接已关闭"));
    return new Promise((resolve, reject) => {
      this.readyWaiters.add({ resolve, reject });
    });
  }

  connect(): void {
    const generation = ++this.generation;
    this.cancelReconnect();
    this.userClosed = false;
    this.connectionReady = false;
    this.rejectReady(new Error("连接已断开"));
    this.sawHello = false;
    this.holding = false;
    this.held = [];
    const previous = this.socket;
    this.socket = null;
    previous?.close();
    const url = endpointFor(this.options.server, this.options.profile);
    const socket = new WebSocket(url);
    this.socket = socket;
    socket.onopen = () => {
      const frame: Record<string, unknown> = {};
      frame.jwt = this.options.refreshCredential?.() || this.options.credential;
      if (this.options.profile === "desktop") frame.device_id = this.options.deviceId || "";
      if (this.bootId) {
        frame.last_boot_id = this.bootId;
        frame.last_seq = this.lastSeq;
      }
      socket.send(JSON.stringify(frame));
    };
    socket.onmessage = (event) => {
      if (generation === this.generation && socket === this.socket) this.receive(String(event.data));
    };
    socket.onclose = (event) => {
      if (this.userClosed || this.generation !== generation) return;
      this.connectionReady = false;
      this.rejectReady(new Error("连接已断开"));
      this.rejectPending(new Error("连接已断开"));
      // 后台里不重试：回到前台由 resume() 立刻补一次。
      if (this.suspended) return;
      const unauthorized = event.code === 4401;
      this.reconnectTimer = setTimeout(() => {
        this.reconnectTimer = null;
        void this.recover(unauthorized);
      }, RECONNECT_DELAY);
    };
  }

  /** 切到后台：不断言连接还在，也不主动关闭。 */
  suspend(): void {
    this.suspended = true;
    this.cancelReconnect();
  }

  /**
   * 回到前台：先确认连接再决定。
   * socket 已经断了就立刻重连（带内存水位补发）；看起来还开着就用一次整态 RPC 探测，
   * 探测失败按普通重连处理——静默断链拿不到 onclose。
   */
  resume(): void {
    if (this.userClosed) return;
    this.suspended = false;
    if (!this.socket || this.socket.readyState !== WebSocket.OPEN) {
      this.cancelReconnect();
      this.connect();
      return;
    }
    void this.probe();
  }

  private async probe(): Promise<void> {
    if (this.probing || this.userClosed) return;
    this.probing = true;
    try {
      const state = await this.rpc("get_initial_state", [], PROBE_TIMEOUT);
      if (this.userClosed) return;
      // 后台期间可能漏帧：取一份整态，之后照常按 (boot_id, seq) 去重。
      this.emit({ type: "yc_snapshot", data: state });
      this.markReady();
    } catch {
      if (this.userClosed) return;
      this.cancelReconnect();
      this.connect();
    } finally {
      this.probing = false;
    }
  }

  private cancelReconnect(): void {
    const timer = this.reconnectTimer;
    if (timer === null) return;
    this.reconnectTimer = null;
    clearTimeout(timer);
  }

  private attachLifecycle(): void {
    if (this.detachLifecycle) return;
    if (typeof document === "undefined" || typeof document.addEventListener !== "function") return;
    const doc = document;
    const onChange = () => {
      if (doc.visibilityState === "hidden") this.suspend();
      else if (doc.visibilityState === "visible") this.resume();
    };
    doc.addEventListener("visibilitychange", onChange);
    this.detachLifecycle = () => doc.removeEventListener("visibilitychange", onChange);
  }

  private async recover(unauthorized: boolean): Promise<void> {
    if (this.userClosed || this.suspended) return;
    if (unauthorized && this.options.onUnauthorized) {
      const ok = await this.options.onUnauthorized();
      if (!ok || this.userClosed || this.suspended) return;
    }
    this.connect();
  }

  close(): void {
    this.userClosed = true;
    this.generation += 1;
    this.cancelReconnect();
    this.detachLifecycle?.();
    this.detachLifecycle = null;
    this.connectionReady = false;
    this.rejectReady(new Error("连接已关闭"));
    this.rejectPending(new Error("连接已关闭"));
    this.socket?.close();
    this.socket = null;
    this.holding = false;
    this.held = [];
    this.bootId = "";
    this.lastSeq = 0;
    this.forgetWatermark();
  }

  rpc(method: string, args: unknown, timeout = RPC_TIMEOUT): Promise<unknown> {
    return new Promise((resolve, reject) => {
      if (!this.socket || this.socket.readyState !== WebSocket.OPEN) {
        reject(new Error("连接未就绪"));
        return;
      }
      const id = ++this.seq;
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error("请求超时"));
      }, timeout);
      this.pending.set(id, { resolve, reject, timer });
      this.socket.send(JSON.stringify({ method, args, id }));
    });
  }

  private receive(raw: string): void {
    let msg: Frame;
    try {
      msg = JSON.parse(raw) as Frame;
    } catch {
      return;
    }
    if (typeof msg.id !== "undefined") {
      const pending = this.pending.get(msg.id);
      if (!pending) return;
      this.pending.delete(msg.id);
      clearTimeout(pending.timer);
      if (msg.error) pending.reject(new Error(msg.error));
      else pending.resolve(msg.result);
      return;
    }
    if (msg.type === "replay" && Array.isArray(msg.frames)) {
      msg.frames.forEach((item) => this.receive(typeof item === "string" ? item : JSON.stringify(item)));
      return;
    }
    // relay 的 device_reset 是 GUI 换了 boot_id：旧 boot 的 seq 与新进程毫无关系，
    // 同样必须整态重来；但它不是同 boot 的洞，水位要清零（新 boot 从 1 重新计数）。
    if (msg.type === "device_reset") {
      this.applyDeviceReset(msg.data?.boot_id || "");
      return;
    }
    // relay 用 resync_required 表示需要整态，语义与 seq_reset 相同。
    if (msg.type === "resync_required") msg = { ...msg, type: "seq_reset", data: { ...msg.data, resync_required: true } };
    const decision = decideFrame(msg, this.sawHello);
    if (decision.kind === "hello" || decision.kind === "reset") {
      this.sawHello = true;
      this.bootId = decision.bootId || this.bootId;
      if (decision.kind === "reset") {
        // 同 boot 空洞：旧水位必须保留给 releaseHeld 去重，否则补发帧会被当成新帧再播一遍。
        const pausedSeq = this.lastSeq;
        this.lastSeq = 0;
        this.holding = false;
        this.held = [];
        if (decision.resync) this.beginResync(pausedSeq);
      }
      if (!decision.resync) this.markReady();
      this.writeWatermark();
      return;
    }
    if (decision.kind === "legacy") this.beginResync();
    if (this.holding && msg.type) {
      if (this.acceptSequence(msg, false)) this.held.push(msg);
      return;
    }
    this.accept(msg, true);
  }

  /**
   * 设备换了 boot_id：整态重来，且水位清零。
   * 旧 boot 的 lastSeq 不能拿来当 pausedSeq——新进程的 seq 从 1 重新开始，
   * 沿用它会把所有新帧当重复帧丢掉；水位本身仍照常落盘（新 boot + 0）。
   */
  private applyDeviceReset(bootId: string): void {
    this.sawHello = true;
    if (bootId) this.bootId = bootId;
    this.lastSeq = 0;
    this.holding = false;
    this.held = [];
    this.pausedSeq = 0;
    this.writeWatermark();
    this.beginResync(0);
  }

  private beginResync(pausedSeq = this.lastSeq): void {
    if (this.holding) return;
    this.connectionReady = false;
    this.holding = true;
    this.pausedSeq = pausedSeq;
    this.held = [];
    if (!this.socket || this.socket.readyState !== WebSocket.OPEN) {
      this.holding = false;
      this.rejectReady(new Error("连接未就绪"));
      return;
    }
    void this.rpc("get_initial_state", []).then((state) => {
      this.emit({ type: "yc_snapshot", data: state });
      this.releaseHeld(state);
      this.markReady();
    }).catch(() => {
      // 失败不能无限压住新事件；调用方可在传输恢复后重试整态 RPC。
      this.held = [];
      this.holding = false;
      this.markReady();
    });
  }

  private markReady(): void {
    this.connectionReady = true;
    const waiters = Array.from(this.readyWaiters);
    this.readyWaiters.clear();
    waiters.forEach((waiter) => waiter.resolve());
  }

  private rejectReady(error: Error): void {
    const waiters = Array.from(this.readyWaiters);
    this.readyWaiters.clear();
    waiters.forEach((waiter) => waiter.reject(error));
  }

  private accept(msg: Frame, advance: boolean): boolean {
    if (!this.acceptSequence(msg, advance)) return false;
    if (msg.type) this.emit({ type: msg.type, data: msg.data });
    return true;
  }

  private acceptSequence(msg: Frame, advance: boolean): boolean {
    const frameBoot = msg.data?.boot_id || "";
    if (frameBoot && this.bootId && frameBoot !== this.bootId) return false;
    if (typeof msg.seq !== "number") return true;
    if (msg.seq <= this.lastSeq) return false;
    if (advance) {
      this.lastSeq = msg.seq;
      this.writeWatermark();
    }
    return true;
  }

  private releaseHeld(state: unknown): void {
    const snapshot = (state || {}) as { active_turn_snapshots?: Record<string, { turn_id?: string; status?: string; state?: string; finished?: boolean }> };
    const finished = new Set<string>();
    Object.values(snapshot.active_turn_snapshots || {}).forEach((turn) => {
      if (turn.turn_id && (turn.finished || ["done", "finished", "error", "cancelled"].includes(String(turn.status || turn.state)))) finished.add(turn.turn_id);
    });
    const pending = this.held;
    this.held = [];
    this.holding = false;
    pending.forEach((frame) => {
      if (keepLiveFrame(frame, this.pausedSeq, this.bootId, finished)) this.accept(frame, true);
    });
  }

  private rejectPending(error: Error): void {
    this.pending.forEach((item) => {
      clearTimeout(item.timer);
      item.reject(error);
    });
    this.pending.clear();
  }

  private emit(frame: { type: string; data?: unknown }): void {
    this.listeners.forEach((listener) => listener(frame));
  }

  private storageKey(): string {
    return watermarkKey(this.options.server, this.options.accountId, this.options.profile);
  }

  private storage(): Storage | null {
    try {
      return (globalThis as { window?: { localStorage?: Storage } }).window?.localStorage ?? null;
    } catch {
      return null;
    }
  }

  /** 只清不读：升级前写进 WebView localStorage 的旧水位不能被当成本次进程的水位。 */
  private dropStoredWatermark(): void {
    try {
      this.storage()?.removeItem(this.storageKey());
    } catch {
      return;
    }
  }

  private readWatermark(): Watermark | null {
    if (!this.persistWatermark) return null;
    try {
      const raw = this.storage()?.getItem(this.storageKey());
      if (!raw) return null;
      const parsed = JSON.parse(raw) as Watermark;
      return { bootId: parsed.bootId || "", lastSeq: Number(parsed.lastSeq) || 0 };
    } catch {
      return null;
    }
  }

  private writeWatermark(): void {
    if (!this.persistWatermark || !this.bootId) return;
    try {
      this.storage()?.setItem(this.storageKey(), JSON.stringify({ bootId: this.bootId, lastSeq: this.lastSeq }));
    } catch {
      return;
    }
  }

  private forgetWatermark(): void {
    if (!this.persistWatermark) return;
    try {
      this.storage()?.removeItem(this.storageKey());
    } catch {
      return;
    }
  }
}

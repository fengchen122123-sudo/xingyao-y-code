/** 客户端对 hello / seq_reset 的裁决。hello 和 seq_reset 不进入业务事件。 */

export interface Watermark {
  bootId: string;
  lastSeq: number;
}

export interface ControlDecision {
  kind: "hello" | "reset" | "legacy" | "event";
  bootId: string;
  resync: boolean;
}

export function decideFrame(
  frame: { type?: string; data?: { boot_id?: string; resync_required?: boolean } },
  sawHello: boolean,
): ControlDecision {
  if (frame.type === "hello") {
    return { kind: "hello", bootId: frame.data?.boot_id || "", resync: false };
  }
  if (frame.type === "seq_reset") {
    return {
      kind: "reset",
      bootId: frame.data?.boot_id || "",
      resync: Boolean(frame.data?.resync_required),
    };
  }
  if (!sawHello) {
    return { kind: "legacy", bootId: "", resync: true };
  }
  return { kind: "event", bootId: "", resync: false };
}

export function watermarkKey(server: string, accountId: string, profile: string): string {
  return `yc-watermark:${server}|${accountId}|${profile}`;
}

export function shouldPersistWatermark(isCapacitor: boolean): boolean {
  return !isCapacitor;
}

export interface LiveFrame {
  type?: string;
  seq?: number;
  data?: { boot_id?: string; turn_id?: string };
}

/** 快照落地前先压住。同一 boot 里，序号不超过暂停点的帧丢掉；已结束回合的增量也丢掉。 */
export function keepLiveFrame(
  frame: LiveFrame,
  pausedSeq: number,
  bootId: string,
  finishedTurns: ReadonlySet<string>,
): boolean {
  const frameBoot = frame.data?.boot_id || "";
  if (frameBoot && bootId && frameBoot !== bootId) return false;
  if (typeof frame.seq === "number" && frame.seq <= pausedSeq) return false;
  const turnId = frame.data?.turn_id || "";
  if (turnId && finishedTurns.has(turnId) && (frame.type === "token_delta" || frame.type === "thinking_delta")) {
    return false;
  }
  return true;
}

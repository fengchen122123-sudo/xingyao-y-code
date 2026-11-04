// M3-T05：画布撤销/重做历史栈。
//
// 分层：这里只管「快照怎么存、怎么取」，不认识 store、不认识桥。
// store 在每次落盘级变更【之前】push 一份 { nodes, connections }（即变更前的状态），
// undo 时把这份状态原样还回来；调用方顺手把「当前状态」作为 current 传进来，
// 栈再把它压进 redo 侧——这样 redo 才能回到撤销前的样子。
//
// 合并窗口：拖拽/连续输入会在一瞬间打出几十次 push，300ms 内的连续 push 合成一条记录：
// 撤销目标是 burst 之前的首个快照，时间戳滚动到最后一次 push（窗口随之后延），
// 于是「一次拖拽 = 一步撤销」，而不是几十步。
import type { CanvasConnection, CanvasNodeData } from '../types';

/** 历史快照：只存结构（节点 + 连线），视口/选中态不进历史（参考仓同口径） */
export type CanvasHistorySnapshot = { nodes: CanvasNodeData[]; connections: CanvasConnection[] };

/** 栈容量：超出丢最旧的（计划口径 50） */
export const HISTORY_CAPACITY = 50;
/** 合并窗口：这段时间内的连续 push 只算一步 */
export const HISTORY_MERGE_WINDOW_MS = 300;

export interface HistoryStackOptions {
  capacity?: number;
  mergeWindowMs?: number;
  /** 时间源（测试注入假时钟；默认 Date.now） */
  now?: () => number;
}

interface HistoryEntry {
  snapshot: CanvasHistorySnapshot;
  /** 该条记录最近一次 push 的时间（合并窗口判定用） */
  at: number;
}

/**
 * 深拷贝快照。
 * structuredClone 能正确处理 Map/Date 等，但遇到函数/不可克隆值会抛；
 * 画布快照理论上只有纯 JSON，抛了就退回 JSON 往返，保证「绝不让复制炸掉撤销」。
 */
function deepClone<T>(value: T): T {
  if (typeof structuredClone === 'function') {
    try {
      return structuredClone(value);
    } catch {
      /* 落到 JSON 往返 */
    }
  }
  return JSON.parse(JSON.stringify(value)) as T;
}

function copySnapshot(snapshot: CanvasHistorySnapshot): CanvasHistorySnapshot {
  return {
    nodes: deepClone(Array.isArray(snapshot?.nodes) ? snapshot.nodes : []),
    connections: deepClone(Array.isArray(snapshot?.connections) ? snapshot.connections : []),
  };
}

export class HistoryStack {
  private readonly capacity: number;
  private readonly mergeWindowMs: number;
  private readonly now: () => number;
  private undoEntries: HistoryEntry[] = [];
  private redoEntries: HistoryEntry[] = [];

  constructor(options: HistoryStackOptions = {}) {
    const capacity = Math.floor(options.capacity ?? HISTORY_CAPACITY);
    this.capacity = Number.isFinite(capacity) && capacity > 0 ? capacity : HISTORY_CAPACITY;
    const window = options.mergeWindowMs ?? HISTORY_MERGE_WINDOW_MS;
    this.mergeWindowMs = Number.isFinite(window) && window >= 0 ? window : HISTORY_MERGE_WINDOW_MS;
    this.now = options.now ?? (() => Date.now());
  }

  get canUndo(): boolean {
    return this.undoEntries.length > 0;
  }

  get canRedo(): boolean {
    return this.redoEntries.length > 0;
  }

  /** 可撤销步数（测试与调试用） */
  get undoSize(): number {
    return this.undoEntries.length;
  }

  /** 可重做步数 */
  get redoSize(): number {
    return this.redoEntries.length;
  }

  /**
   * 记录一份「变更前」的快照。
   * - 快照一律深拷贝：调用方后续怎么改 project 都污染不到栈；
   * - 新变更会让 redo 分支失效（和编辑器通用语义一致）；
   * - 合并窗口内：保留首个快照（＝撤销目标），只把时间戳滚到本次。
   */
  push(snapshot: CanvasHistorySnapshot): void {
    if (!snapshot) return;
    const at = this.now();
    const last = this.undoEntries[this.undoEntries.length - 1];
    if (last && at - last.at <= this.mergeWindowMs) {
      last.at = at;
      this.redoEntries = [];
      return;
    }
    this.undoEntries.push({ snapshot: copySnapshot(snapshot), at });
    if (this.undoEntries.length > this.capacity) {
      this.undoEntries.splice(0, this.undoEntries.length - this.capacity);
    }
    this.redoEntries = [];
  }

  /**
   * 撤销：返回要恢复的快照（调用方负责把 project 换成它）。
   * current 传「撤销前的当前状态」，会被压进 redo 栈；不传则只做单向回退（redo 空转）。
   * 栈空返回 null。
   */
  undo(current?: CanvasHistorySnapshot): CanvasHistorySnapshot | null {
    const entry = this.undoEntries.pop();
    if (!entry) return null;
    if (current) {
      this.redoEntries.push({ snapshot: copySnapshot(current), at: this.now() });
      if (this.redoEntries.length > this.capacity) {
        this.redoEntries.splice(0, this.redoEntries.length - this.capacity);
      }
    }
    return copySnapshot(entry.snapshot);
  }

  /**
   * 重做：返回要恢复的快照。
   * current 传「重做前的当前状态」，压回 undo 栈（不参与合并窗口——重做是一步明确操作）。
   * 栈空返回 null。
   */
  redo(current?: CanvasHistorySnapshot): CanvasHistorySnapshot | null {
    const entry = this.redoEntries.pop();
    if (!entry) return null;
    if (current) {
      this.undoEntries.push({ snapshot: copySnapshot(current), at: this.now() });
      if (this.undoEntries.length > this.capacity) {
        this.undoEntries.splice(0, this.undoEntries.length - this.capacity);
      }
    }
    return copySnapshot(entry.snapshot);
  }

  /** 清空（换项目/新建项目时调用：跨项目撤销没有意义） */
  clear(): void {
    this.undoEntries = [];
    this.redoEntries = [];
  }
}

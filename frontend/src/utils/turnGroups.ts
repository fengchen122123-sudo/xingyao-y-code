import type { ChatMessage } from '../stores/appStore';
import { diffCounts, type LegacyDiff } from './toolTrace';

export { countDiffChanges } from './toolTrace';

const msgKeyMap = new WeakMap<object, number>();
let msgKeySeq = 0;

export function msgKey(msg: object): number {
  let k = msgKeyMap.get(msg);
  if (k === undefined) {
    k = ++msgKeySeq;
    msgKeyMap.set(msg, k);
  }
  return k;
}

export interface TurnGroupInfo {
  userMsg: ChatMessage | null;
  middle: ChatMessage[];
  tail: ChatMessage[];
  running: boolean;
  state: 'completed' | 'cancelled' | 'error';
  turnId: string;
}

export function buildMessageGroups(messages: ChatMessage[]): TurnGroupInfo[] {
  const groups: TurnGroupInfo[] = [];
  let current: TurnGroupInfo | null = null;

  for (const msg of messages) {
    if (msg.role === 'user') {
      current = {
        userMsg: msg, middle: [], tail: [], running: false, state: 'completed',
        turnId: msg.turnId || '',
      };
      groups.push(current);
      continue;
    }
    if (!current) {
      current = { userMsg: null, middle: [], tail: [], running: false, state: 'completed', turnId: '' };
      groups.push(current);
    }
    if (msg.role === 'assistant') {
      // 新轮次开始：原 tail 里的 assistant 降级为中间过程
      current.middle.push(...current.tail);
      current.tail.length = 0;
      current.tail.push(msg);
    } else if (current.tail.length > 0) {
      current.tail.push(msg);
    } else {
      current.middle.push(msg);
    }
  }

  for (const g of groups) {
    // 无任何 assistant 回复的纯提示/报错回合（如 `/笔记本目录` 等直接走本地或 HTTP 的斜杠命令）：
    // 不能留在 middle，否则 TurnGroup 因 hasAssistant=false 且 tail 为空会整段隐藏。
    if (g.tail.length === 0 && g.middle.length > 0) {
      g.tail.push(...g.middle);
      g.middle.length = 0;
    }
    const assistants = [...g.middle, ...g.tail].filter((m) => m.role === 'assistant');
    g.running = assistants.some((m) => m.execution_state === 'running');
    const states = assistants.map((m) => m.execution_state);
    if (states.includes('cancelled') || assistants.some((m) => m.cancelled)) {
      g.state = 'cancelled';
    } else if (states.includes('error')) {
      g.state = 'error';
    } else {
      g.state = 'completed';
    }
  }
  return groups;
}

export function groupHasProcess(group: TurnGroupInfo): boolean {
  return group.middle.length > 0;
}

export function groupStats(group: TurnGroupInfo): { rounds: number; toolCount: number } {
  let rounds = 0;
  let toolCount = 0;
  for (const m of [...group.middle, ...group.tail]) {
    if (m.role !== 'assistant') continue;
    rounds += 1;
    toolCount += (m.tool_calls?.length ?? 0) + (m.legacy_diffs?.length ?? 0);
  }
  return { rounds, toolCount };
}

export interface FileChangeEntry {
  name: string;
  path: string;
  added: number;
  removed: number;
  diffText: string;
}

function baseName(p: string): string {
  const norm = p.replace(/\\/g, '/');
  const i = norm.lastIndexOf('/');
  return i >= 0 ? norm.slice(i + 1) : norm;
}

export function collectFileChanges(group: TurnGroupInfo): FileChangeEntry[] {
  const byPath = new Map<string, FileChangeEntry>();
  const push = (diff: LegacyDiff) => {
    const filePath = diff.file_path;
    const diffText = diff.diff_text;
    const p = String(filePath || '').trim();
    if (!p) return;
    const { added, removed } = diffCounts(diff);
    const entry = byPath.get(p);
    if (entry) {
      entry.added += added;
      entry.removed += removed;
      if (diffText) entry.diffText += (entry.diffText ? '\n' : '') + diffText;
    } else {
      byPath.set(p, { name: baseName(p), path: p, added, removed, diffText: diffText || '' });
    }
  };
  for (const m of [...group.middle, ...group.tail]) {
    if (m.role !== 'assistant') continue;
    for (const c of m.tool_calls ?? []) {
      if (c.diff && c.diff.file_path) push(c.diff);
    }
    for (const d of m.legacy_diffs ?? []) {
      push(d);
    }
  }
  return [...byPath.values()];
}

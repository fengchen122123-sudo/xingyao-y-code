import type { ChatMessage } from '../stores/appStore';

// ---------------------------------------------------------------------------
// 稳定消息 key：对象身份 -> 递增数字。
// App.vue 消息流与 TurnGroup 折叠体共用，保证分组重排（running -> 折叠形态
// 切换）时同一条消息的组件实例按身份复用，而不是按数组下标错位重挂载。
// ---------------------------------------------------------------------------
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

// ---------------------------------------------------------------------------
// 按回合分组消息流
//
// 一个回合 = 一条用户消息 + 其后到下一条用户消息前的全部回复消息。
// 回合结束后中间过程（多个 LLM 轮次的星瑶消息、提示、中途错误）折叠成
// 一块，组尾只保留最后一条 assistant（最终总结）与其后的 error 消息。
// 回合运行中不折叠，保持逐条实时显示的现状。
// ---------------------------------------------------------------------------

export interface TurnGroupInfo {
  /** 触发本轮的用户消息；会话开头孤儿段可为 null */
  userMsg: ChatMessage | null;
  /** 中间过程消息（已完成的轮次 / 提示 / 中途错误），回合结束后折叠展示 */
  middle: ChatMessage[];
  /** 组尾：最后一条 assistant（总结轮）+ 其后的 error 消息，始终可见 */
  tail: ChatMessage[];
  /** 组内任一 assistant 仍 running：实时模式，不折叠 */
  running: boolean;
  /** 回合终态（running 时不定），用于折叠头文案 */
  state: 'completed' | 'cancelled' | 'error';
}

export function buildMessageGroups(messages: ChatMessage[]): TurnGroupInfo[] {
  const groups: TurnGroupInfo[] = [];
  let current: TurnGroupInfo | null = null;

  for (const msg of messages) {
    if (msg.role === 'user') {
      current = { userMsg: msg, middle: [], tail: [], running: false, state: 'completed' };
      groups.push(current);
      continue;
    }
    if (!current) {
      // 会话开头没有用户消息的孤儿段（异常数据 / 旧会话残留），同样聚合
      current = { userMsg: null, middle: [], tail: [], running: false, state: 'completed' };
      groups.push(current);
    }
    if (msg.role === 'assistant') {
      // 新轮次开始：原 tail 里的 assistant 降级为中间过程
      current.middle.push(...current.tail);
      current.tail.length = 0;
      current.tail.push(msg);
    } else if (current.tail.length > 0) {
      // 总结轮之后的 error 消息跟在组尾（用户需要立即看到）
      current.tail.push(msg);
    } else {
      // 回合中段的 notice / error 收进折叠区
      current.middle.push(msg);
    }
  }

  for (const g of groups) {
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

/** 回合内是否值得显示折叠头：有中间过程消息才有东西可折 */
export function groupHasProcess(group: TurnGroupInfo): boolean {
  return group.middle.length > 0;
}

/** 折叠头摘要统计：轮次数与工具调用数（含未绑定的历史 diff） */
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

// ---------------------------------------------------------------------------
// 回合文件更改摘要（"N 个文件已更改" 折叠面板）
//
// 数据来源：本回合所有 assistant 消息里，mutation 工具调用绑定的 diff 与
// 未绑定的历史 legacy_diffs。行数从 diff 文本启发式统计（与后端
// core/turn_renderer._count_diff_changes 同一套口径：剥离 ANSI/行号后按
// + / - 前缀计数，跳过 +++ / --- 文件头）。同一文件多次修改去重，行数累加。
// ---------------------------------------------------------------------------

export interface FileChangeEntry {
  /** 文件名（basename），如 tools.py */
  name: string;
  /** 相对/原始路径，如 .\core\tools.py */
  path: string;
  added: number;
  removed: number;
  /** 该文件本回合全部修改的 unified diff 文本（多次修改拼接），用于展开查看 */
  diffText: string;
}

const DIFF_ANSI_RE = /\x1b\[[0-9;]*m/g;
const DIFF_MARKER_RE = /^[\d\s]*([+-])/;

/** 与后端 _count_diff_changes 对齐的 +/− 行数统计 */
export function countDiffChanges(diffText: string): { added: number; removed: number } {
  let added = 0;
  let removed = 0;
  for (const rawLine of diffText.split('\n')) {
    const line = rawLine.replace(DIFF_ANSI_RE, '').replace(/│/g, '').trim();
    if (!line || line.startsWith('+++') || line.startsWith('---')) continue;
    const m = line.match(DIFF_MARKER_RE);
    if (!m) continue;
    if (m[1] === '+') added += 1;
    else removed += 1;
  }
  return { added, removed };
}

function baseName(p: string): string {
  const norm = p.replace(/\\/g, '/');
  const i = norm.lastIndexOf('/');
  return i >= 0 ? norm.slice(i + 1) : norm;
}

/** 收集回合内的文件更改（去重 + 行数累加）。无更改返回空数组（面板不显示） */
export function collectFileChanges(group: TurnGroupInfo): FileChangeEntry[] {
  const byPath = new Map<string, FileChangeEntry>();
  const push = (filePath: string, diffText: string) => {
    const p = String(filePath || '').trim();
    if (!p) return;
    const { added, removed } = countDiffChanges(diffText || '');
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
      if (c.diff && c.diff.file_path) push(c.diff.file_path, c.diff.diff_text);
    }
    for (const d of m.legacy_diffs ?? []) {
      push(d.file_path, d.diff_text);
    }
  }
  return [...byPath.values()];
}

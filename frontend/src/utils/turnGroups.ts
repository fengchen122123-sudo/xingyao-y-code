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

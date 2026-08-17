import type { ChatMessage } from './appStore';
import type { PlanData, TokenUsage } from '../types/pywebview';
import type { SubagentTaskSnapshot } from '../utils/toolTrace';

export interface SessionRuntimeState {
  messages: ChatMessage[];
  streamingText: string;
  thinkingText: string;
  running: boolean;
  subagentTasks: SubagentTaskSnapshot[];
  planData?: PlanData;
  // 该会话最近一次收到的 Token 用量（窗口口径）：切回会话时先恢复此值，
  // 避免底部摘要短暂停留在上一个会话的数字，随后由后端 token_usage 事件校正
  tokenUsage?: TokenUsage;
  // 本轮回合开始时间戳（毫秒）：用于"已工作 xx秒"计时，切回会话时恢复，
  // 避免切换窗口后计时被重置为 0 重新开始
  turnStartedAt?: number;
}

export function createSessionRuntime(): SessionRuntimeState {
  return {
    messages: [],
    streamingText: '',
    thinkingText: '',
    running: false,
    subagentTasks: [],
    planData: { title: '', items: [], progress: 0 },
  };
}

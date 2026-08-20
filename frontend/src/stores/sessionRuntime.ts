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
  // 该会话绑定的模型选择（含未落库的 pending 草稿会话）：切换会话时即时恢复，
  // A/B 会话各自用各自的模型，互不覆盖；未设置（undefined）时保持当前值，
  // 由后端 switch_session 响应校正为该会话落库的绑定模型
  modelId?: string;
  modelName?: string;
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

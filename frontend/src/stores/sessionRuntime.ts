import type { ChatMessage } from './appStore';
import type { PlanData } from '../types/pywebview';
import type { SubagentTaskSnapshot } from '../utils/toolTrace';

export interface SessionRuntimeState {
  messages: ChatMessage[];
  streamingText: string;
  thinkingText: string;
  running: boolean;
  subagentTasks: SubagentTaskSnapshot[];
  planData?: PlanData;
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

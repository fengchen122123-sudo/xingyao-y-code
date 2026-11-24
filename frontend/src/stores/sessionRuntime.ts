import type { ChatMessage } from './appStore';
import type { PlanData, TokenUsage } from '../types/pywebview';
import type { SubagentTaskSnapshot } from '../utils/toolTrace';
import type { AttachmentItem } from '../types/composer';

export interface SessionRuntimeState {
  messages: ChatMessage[];
  // 尚未发送的输入文字属于会话运行态，组件卸载或切换会话时不能丢失/串用。
  composerText?: string;
  composerAttachments?: AttachmentItem[];
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
  modelId?: string;
  modelName?: string;
  permMode?: string;
  usedSkills?: string[];
  streamingSpeed?: number;
  historyHasMore?: boolean;
  historyOldestId?: number | null;
  // 红点导航全量索引：随会话保存/恢复，切会话时绝不残留上一个会话的红点
  navItems?: Array<{ id: number; preview: string }>;
  rightPanelTabs?: string[];
  rightPanelActiveTab?: string;
  rightPanelExpanded?: boolean;
  rightPanelUserCollapsed?: boolean;
  automationViewport?: [number, number] | null;
  embeddedBrowserUrl?: string;
  embeddedBrowserTitle?: string;
  bgTaskDetailId?: string;
  // 该会话最近查看的子代理详情任务 id：与 bgTaskDetailId 完全同口径随会话
  // 保存/恢复——切会话不残留上一个会话的查看对象，否则 SubagentPane 会用
  // 新会话名去查上一会话的任务 id，详情串场
  subagentDetailId?: string;
  // 后台回合先产生事件时，若尚未拉过全量历史，切回时补拉一次，避免覆盖旧消息
  needsHistoryHydration?: boolean;
  historyHydrated?: boolean;
}

export function createSessionRuntime(): SessionRuntimeState {
  return {
    messages: [],
    composerText: '',
    composerAttachments: [],
    streamingText: '',
    thinkingText: '',
    running: false,
    subagentTasks: [],
    planData: { title: '', items: [], progress: 0 },
  };
}

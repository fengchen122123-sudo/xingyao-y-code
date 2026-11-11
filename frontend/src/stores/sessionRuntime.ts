import type { ChatMessage } from './appStore';
import type { PlanData, TokenUsage } from '../types/pywebview';
import type { SubagentTaskSnapshot } from '../utils/toolTrace';
import type { AttachmentItem } from '../types/composer';

export interface SessionRuntimeState {
  messages: ChatMessage[];
  // 尚未发送的输入文字属于会话运行态，组件卸载或切换会话时不能丢失/串用。
  composerText?: string;
  // 与文字草稿同生命周期；数组引用在 pending 会话迁移为真实会话时保持不变，
  // 让已开始的异步上传仍能写回发起操作的会话。
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
  // 该会话绑定的模型选择（含未落库的 pending 草稿会话）：切换会话时即时恢复，
  // A/B 会话各自用各自的模型，互不覆盖；未设置（undefined）时保持当前值，
  // 由后端 switch_session 响应校正为该会话落库的绑定模型
  modelId?: string;
  modelName?: string;
  // 该会话的权限档位（含未落库的 pending 草稿会话）：权限选择只影响当前会话，
  // 切换会话时即时恢复；后端 switch_session 响应按会话落库绑定校正
  permMode?: string;
  // 本回合实际用到的技能名（从 is_skill 工具调用收集）：随会话保存/恢复，
  // 状态栏"技能"项据此显示；回合开始时清空，回合结束后固化为本回合结果
  usedSkills?: string[];
  // 当前流式生成瞬时速度（t/s）：切回会话时恢复
  streamingSpeed?: number;
  // 会话历史分页游标（懒加载）：随会话保存/恢复；loading 为瞬态不入 runtime
  historyHasMore?: boolean;
  historyOldestId?: number | null;
  // 红点导航全量索引：随会话保存/恢复，切会话时绝不残留上一个会话的红点
  navItems?: Array<{ id: number; preview: string }>;
  // 右面板多页签（会话级隔离）：该会话打开的页签、激活页签与面板展开态，
  // 随会话保存/恢复——A 会话打开的浏览器页签不串到 B 会话。
  // undefined = 该会话从未展开过右面板（切回时默认收起、无页签）
  rightPanelTabs?: string[];
  rightPanelActiveTab?: string;
  rightPanelExpanded?: boolean;
  // 该会话最近查看的后台任务详情 id：随会话保存/恢复，切会话时任务详情
  // 不残留上一个会话的查看对象（与 rightPanelTabs 的会话隔离同口径）
  bgTaskDetailId?: string;
  // 该会话最近查看的子代理详情任务 id：与 bgTaskDetailId 完全同口径随会话
  // 保存/恢复——切会话不残留上一个会话的查看对象，否则 SubagentPane 会用
  // 新会话名去查上一会话的任务 id，详情串场
  subagentDetailId?: string;
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

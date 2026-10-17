// Python <-> pywebview 桥接层类型定义
// 本文件按后端桥接 API 契约编写（而非当前 gui_bridge.py 的实现），
// 后端会单独重写以匹配此契约。

import type {
  GuiTheme,
  GuiThemeResult,
  GuiThemeSummary,
  NativeTitlebarColorCapability,
  NativeTitlebarTheme,
  ThemeRegionId,
} from '../themes/types';

export type PermissionMode = 'confirm' | 'accept_edits' | 'plan' | 'full';
export type ReasoningChoice = 'default' | 'off' | 'on' | 'low' | 'medium' | 'high' | 'xhigh' | 'max';
export type ComputerProtocolChoice = '' | 'none' | 'generic' | 'native_openai';

export interface ReasoningOption {
  value: ReasoningChoice;
  label: string;
}

export interface ReasoningSnapshot {
  kind: 'effort' | 'toggle' | 'fixed' | 'hidden';
  visible: boolean;
  disabled: boolean;
  disabled_reason: string;
  choice: ReasoningChoice;
  options: ReasoningOption[];
}

export interface ModelInfo {
  id: string;
  name: string;
  provider_id: string;
  provider_name: string;
  context_window: number;
  supports_vision: boolean;
  // 电脑操控能力契约（设计 9.1）：旧模型缺省 false / 'none'，不自动获得资格
  supports_function_calling: boolean;
  computer_protocol: ComputerProtocolChoice;
  // 最大输出：null = 未设置（请求时走内置默认表）
  max_tokens?: number | null;
  // 思考设置：choice 为当前持久化选择（未设置时 "default"）；
  // options 为该模型真实支持的档位（后端按模型能力表给出，不猜测）；
  // visible=false 的模型不展示思考设置，kind=fixed 的模型不可调（只读提示）。
  reasoning_choice?: string;
  reasoning_options?: { value: string; label: string }[];
  reasoning_visible?: boolean;
  reasoning_kind?: string;
  // 内置官方默认档位（无依据的模型为空串）：打开弹窗且没有用户选择时自动预选
  reasoning_default_choice?: string;
  reasoning_disabled_reason?: string;
}

export interface ProviderInfo {
  id: string;
  name: string;
  base_url?: string;
  protocol?: string;
  preset_id?: string;
  has_api_key?: boolean;
  models?: ModelInfo[];
}

export interface ApiProtocolInfo {
  id: 'anthropic' | 'openai' | 'responses';
  label: string;
  path: string;
}

export interface PresetModelInfo {
  name: string;
  context_window: number;
  supports_vision: boolean;
}

export interface PresetProviderInfo {
  id: string;
  name: string;
  base_url: string;
  protocol: ApiProtocolInfo['id'];
  models: PresetModelInfo[];
}

export interface ModelSettingsSnapshot {
  protocols: ApiProtocolInfo[];
  presets: PresetProviderInfo[];
  providers: ProviderInfo[];
  models: ModelInfo[];
  active_model_id: string;
  secondary_model_id: string;
  secondary_model_roles: {
    vision: boolean;
    compression: boolean;
    subagent: boolean;
  };
}

export interface CommandShellOption {
  id: string;
  label: string;
  description: string;
  available: boolean;
  executable: string;
  unavailable_reason: string;
}

// core.computer.settings.BrowserOption.to_dict()：单个受支持浏览器的独立探测结果
export interface ComputerBrowserOption {
  browser: string;
  label: string;
  available: boolean;
  executable: string;
  unavailable_reason: string;
}

// core.computer.models.ComputerRunSnapshot.to_public_dict()：脱敏后的公开字段
export interface ComputerRunSnapshot {
  run_id: string;
  owner_session_id: string;
  owner_turn_id: string;
  status: string;
  target: string;
  browser: string;
  model_name: string;
  protocol: string;
  started_at: number;
  action_count: number;
  current_action: string;
  current_surface: string;
  preview_url: string;
  pause_reason: string;
  stop_reason: string;
  error_code: string;
  hotkey_available: boolean;
}

export interface FeishuSettingsSnapshot {
  enabled: boolean;
  app_id: string;
  /** 仅表示是否已配置密钥；明文不经快照下发 */
  has_secret: boolean;
  approvers: string[];
  notify_chat_id: string;
  status: {
    state: string;
    running: boolean;
    last_error: string;
    received_events: number;
    duplicate_events: number;
    uptime_seconds: number;
  };
}

// JEV 决策模型服务（Task 3 设置页）：字段与 core.decision_service.DEFAULT_CONFIG 对齐。
// 默认全关（enabled=false 时项目行为零变化）；损坏配置由后端回落默认值。
export interface DecisionSettingsSnapshot {
  version: number;
  enabled: boolean;
  /** "auto"=自动匹配基元律动通道；或指定 provider id/name */
  provider_ref: string;
  /** 空串 = 用 provider 的 base_url；非空为手动覆盖 */
  base_url: string;
  model_id: string;
  endpoint_style: 'decision' | 'systemone';
  /** "auto"=跨权限轮换并记忆可用 Key；"index:N"=固定第 N 把（N 为 0 基） */
  key_mode: string;
  shadow_mode: boolean;
  timeout_ms: number;
  scenes: Record<string, { enabled: boolean; shadow: boolean; threshold: number }>;
  audit_enabled: boolean;
}

// probe_decision() 单条逐 Key 结果；status 分类见 core.decision_service.probe_decision
export interface DecisionProbeKeyResult {
  /** 第几把 Key（0 基；UI 展示时 +1） */
  key_index: number;
  status: 'available' | 'no_permission' | 'invalid_key' | 'timeout' | 'network_error' | 'rate_limited' | 'error';
  latency_ms: number;
  error: string;
}

// get_decision_settings() 的运行时状态（Key 序号与最近一次探针结果；不落盘）
export interface DecisionRuntimeState {
  key_count: number;
  active_key_index: number | null;
  denied_key_indexes: number[];
  cooldown_key_indexes: number[];
  last_probe: {
    ok: boolean;
    results: DecisionProbeKeyResult[];
    model_id: string;
    msg: string;
  } | null;
}

// list_decision_models() 条目：上游 type=="decision" 的模型
export interface DecisionModelOption {
  id: string;
  name: string;
  context: number;
  price_text: string;
}

// list_decision_providers() 条目：可选调用通道（不含 Key 内容，仅报数量）
export interface DecisionProviderOption {
  /** provider 的 id（优先）或 name */
  ref: string;
  name: string;
  base_url: string;
  key_count: number;
  /** 是否为 provider_ref="auto" 的解析目标（base_url 含 tokenrhythm 的第一个通道） */
  auto_match: boolean;
}

// get_decision_audit_summary() 条目：单条审计记录的精简视图（不含正文）
export interface DecisionAuditRecentItem {
  ts: string;
  scene: string;
  shadow: boolean;
  adopted: boolean;
  latency_ms: number | null;
  fallback_reason: string;
  model_id: string;
  answers_digest: string;
}

// get_decision_audit_summary() 汇总：今日计数 + 最近记录（设置页「决策记录」面板）
export interface DecisionAuditSummary {
  ok: boolean;
  today_count: number;
  today_shadow_count: number;
  today_adopted_count: number;
  today_fallback_count: number;
  recent: DecisionAuditRecentItem[];
  msg?: string;
}

export interface SystemSettingsSnapshot {
  command_shell: string;
  command_shell_options: CommandShellOption[];
  command_shell_available: boolean;
  command_shell_error: string;
  remote_token_ttl: number;
  agent_turn_step_limit: number;
  agent_turn_step_limit_min: number;
  agent_turn_step_limit_max: number;
  // 上下文水位基准：null 表示未设置（直接跟随模型真实窗口），
  // 取值范围由 min/max 给出，供设置页输入框与校验使用
  context_watermark_base: number | null;
  context_watermark_base_min: number;
  context_watermark_base_max: number;
  // AI 生成内容目录（Task 5）：ai_output_dir 为当前生效的绝对路径；
  // ai_output_dir_setting 为原始配置值（未配置为空串，供设置页回显草稿）；
  // ai_output_dir_default 为默认目录（占位符展示用）
  ai_output_dir: string;
  ai_output_dir_setting: string;
  ai_output_dir_default: string;
  // 电脑操控设置（设计 8.1/8.2）：两个开关默认关闭，浏览器选项独立探测
  browser_control_enabled: boolean;
  computer_control_enabled: boolean;
  computer_browser: string;
  computer_browser_options: ComputerBrowserOption[];
  computer_profile_path: string;
  computer_platform_supported: boolean;
  computer_hotkey_available: boolean;
  computer_hotkey: string;
  computer_run: ComputerRunSnapshot;
  // 当前活动模型的电脑操控资格（与 Turn 起点同一判定函数的只读展示）
  computer_active_model_capability: {
    model: string;
    eligible: boolean;
    protocol?: string;
    error_code?: string;
    reason?: string;
  };
}

// 电脑操控三个严格 setter 的统一返回形状（Task 12）：
// 失败时携带稳定 error_code + 后端真实 settings 快照供前端回滚
export interface SystemSettingsMutationResult {
  ok: boolean;
  error_code?: string;
  msg?: string;
  settings: SystemSettingsSnapshot;
}

// 电脑操控控制 API 统一返回（Task 13a）：快照字段平铺 + ok/msg，
// 失败时 error_code/msg 覆盖快照同名字段（与 gui_bridge._run_computer_mutation 一致），
// 前端用完整快照校准 Store（设计 16.4）
export interface ComputerControlResult extends ComputerRunSnapshot {
  ok: boolean;
  msg?: string;
}

// 内嵌浏览器（右侧面板浏览器页签）：DOM 占位区的 CSS px 矩形，上报给宿主，
// 由宿主把叠加的原生 WebView2 控件摆到该矩形上
export interface EmbeddedBrowserRect {
  x: number;
  y: number;
  width: number;
  height: number;
}

// core.embedded_browser.EmbeddedBrowserController._snapshot()：宿主回推的状态快照
export interface EmbeddedBrowserState {
  url: string;
  title: string;
  can_go_back: boolean;
  can_go_forward: boolean;
  loading: boolean;
  ready: boolean;
  visible: boolean;
}

// browser_pane_* 系列统一返回：成功 {ok:true,state?}；失败 {ok:false,error}
// error ∈ 'unsupported_platform' | 'invalid_rect' | 'form_unavailable' | 'not_open'
//       | 'invoke_failed' | 'not_ready' | 'invalid_url'（前端只特判 unsupported_platform）
export interface EmbeddedBrowserPaneResult {
  ok: boolean;
  state?: EmbeddedBrowserState | null;
  error?: string;
}

// /doctor 聚合自检（T3b）：单项结果来自 core.doctor.CheckResult，字段名与
// CLI `/doctor --json` schema 完全一致（name/status/detail/hint）；status 四态固定
export type DoctorCheckStatus = 'ok' | 'warn' | 'fail' | 'skip';

export interface DoctorCheck {
  name: string;
  status: DoctorCheckStatus;
  detail: string;
  hint: string;
}

// run_doctor 桥接返回：ok=false 时 checks 为 []（防御性兜底），
// summary 为各状态计数（键固定四态），供设置页顶部汇总展示
export interface DoctorReport {
  ok: boolean;
  msg?: string;
  checks: DoctorCheck[];
  summary: Record<DoctorCheckStatus, number>;
}

// 电脑操控四类事件（Task 13a）：平铺信封——run_id 与严格递增的 event_seq 都在
// 顶层，与既有 {"type","data"} 信封不同；事件体只含脱敏小 JSON，绝不携带截图
// base64 或输入正文。载荷字段按防御性解析声明为可选并容忍未知扩展字段
export type ComputerEventType =
  | 'computer_state'
  | 'computer_action_started'
  | 'computer_action_finished'
  | 'computer_observation';

export interface ComputerEventBase {
  run_id: string;
  event_seq: number;
  type: string;
}

export interface ComputerStateEvent extends ComputerEventBase {
  type: 'computer_state';
  status?: string;
  target?: string;
  reason?: string;
  [key: string]: unknown;
}

export interface ComputerActionStartedEvent extends ComputerEventBase {
  type: 'computer_action_started';
  action_ordinal?: number;
  action_kind?: string;
  target?: string;
  [key: string]: unknown;
}

export interface ComputerActionFinishedEvent extends ComputerEventBase {
  type: 'computer_action_finished';
  action_ordinal?: number;
  action_kind?: string;
  ok?: boolean;
  error_code?: string;
  // 契约扩展位：后端可能补发脱敏摘要/耗时（brief ComputerActionTrace 字段）
  summary?: string;
  duration_ms?: number;
  // R9 生产实测分段（仅本地事件数据）：queue/input/wait/observe 缺省表示
  // 该阶段未发生（“未测量”），绝不以 0ms 伪装瞬间完成
  queue_ms?: number;
  input_ms?: number;
  wait_ms?: number;
  observe_ms?: number;
  [key: string]: unknown;
}

export interface ComputerObservationEvent extends ComputerEventBase {
  type: 'computer_observation';
  action_ordinal?: number;
  target?: string;
  screen_sha256?: string;
  width?: number;
  height?: number;
  origin_or_window?: string;
  // 只带 "/computer-preview/" 开头的 loopback token URL，无截图内容
  preview_url?: string;
  // R5 受控画面本地身份：browser 为 "page-N"，desktop 为 "hwnd:<句柄>"；
  // generation 在受控页/前台窗口/几何变化时递增（仅本地展示用）
  surface_id?: string;
  surface_generation?: number;
  [key: string]: unknown;
}

export type ComputerControlEvent =
  | ComputerStateEvent
  | ComputerActionStartedEvent
  | ComputerActionFinishedEvent
  | ComputerObservationEvent;

// 前端内存操作轨迹行（脱敏）：只含五个契约字段；type_text 只显示长度，
// 绝不存输入正文（设计 16.2 / 22）
export interface ComputerActionTrace {
  action_ordinal: number;
  summary: string;
  status: string;
  duration_ms: number;
  error_code: string;
}

export interface SkillInfo {
  id: string;
  short_id?: string;
  name: string;
  description: string;
  enabled: boolean;
  source?: string;     // user | builtin（旧版本后端可能不返回）
  // 该技能曾由内置释放、且内置源里仍有同名技能——用于判断能否「恢复内置版本」。
  // 旧后端不返回时按 undefined（不显示恢复入口）处理，避免误导用户
  from_builtin?: boolean;
}

export interface McpInfo {
  name: string;
  enabled: boolean;
  connected: boolean;
  tools_count: number;
  // 连接失败原因（正常为 null / 旧后端不返回该字段时为 undefined）
  last_error?: string | null;
}

export interface PluginInfo {
  name: string;
  version: string;
  description: string;
  source: string;      // user | builtin
  status: string;      // loaded | disabled | failed | overridden | pending_enable | pending_disable | untrusted
  enabled: boolean;
  tools: string[];
  // 插件是否提供设置页（入口模块声明了 plugin_settings 钩子；未启用/未信任一律 false）
  has_settings?: boolean;
  error?: string;
}

// C11：Hook 条目（Hook 信任卡片数据源；status 由后端按配置内容摘要派生）
export interface HookInfo {
  event: string;
  matcher: string;
  command: string;
  timeout_ms: number;
  status_message: string;
  source_path: string;
  scope: 'user' | 'project';
  digest: string;
  status: 'pending' | 'trusted' | 'stale' | 'blocked' | 'revoked';
  // 是否已禁用；后端总返回，可选以兼容旧桥接
  disabled?: boolean;
}

// 星轨自动化任务（待开发/星轨自动化任务/01-完整设计规格.md 11.3）
export interface AutomationTaskInfo {
  id: string;
  name: string;
  enabled: boolean;
  trigger_type: 'interval' | 'daily' | 'weekly' | 'startup';
  trigger_config: Record<string, any>;
  target_session: string;
  // 任务指定的工作目录：'' = 跟随目标会话归属；'GLOBAL' = 新会话挂自由对话；
  // 其余为懒创建新会话时绑定的项目目录路径
  workdir?: string;
  // 任务指定的执行模型 id：'' = 跟随会话绑定/全局默认
  model_id?: string;
  prompt: string;
  permission_mode: 'confirm' | 'accept_edits' | 'plan' | 'full';
  missed_run_policy: 'skip' | 'run_once';
  notify_on_success: boolean;
  created_at: string;
  updated_at: string;
  last_run_at?: string | null;
  // unix 秒；startup 类型恒 null
  next_run_at?: number | null;
  // 后端生成的面向用户触发器摘要（如「每天 09:30」）
  trigger_summary?: string;
}

export interface AutomationRunInfo {
  id: string;
  task_id: string;
  task_name: string;
  trigger_kind: 'scheduled' | 'startup' | 'manual' | 'catchup';
  status: 'queued' | 'running' | 'completed' | 'cancelled' | 'error' | 'skipped' | 'interrupted';
  session_name: string;
  turn_id?: string | null;
  error?: string | null;
  queued_at: string;
  started_at?: string | null;
  finished_at?: string | null;
}

export interface SessionInfo {
  name: string;
  // 不可变主键 sessions.id（按回合撤销的稳定归属标识；旧后端缺省为 undefined）
  session_id?: number;
  display_name: string;
  count: number;
  workdir: string;
  is_global?: boolean;
  // 秘书会话专属：该会话的秘书名（= 显示名，未命名的历史会话回落「秘书」）。
  // 普通会话没有这个字段；前端据此 + 分组 is_secretary 判定改名走哪条路
  secretary_name?: string;
  updated_at?: string;
}

// 会话消息排队：回合运行期间发送的消息（快照展示文本，不含 @路径拼接）
export interface QueuedMessage {
  id: string;
  text: string;
  attachments: string[];
}

export interface SessionGroup {
  folder_path: string;
  folder_name: string;
  is_current?: boolean;
  is_global?: boolean;
  // 秘书归属分组：folder_path='SECRETARY' 只是分组标识，**不是磁盘目录**，
  // 因此不能拿它拼路径 / 打开资源管理器 / 删除分组；组内会话与「对话」同口径
  // （workdir='GLOBAL'、is_global=true）。后端固定把它排在 GLOBAL 之前。
  is_secretary?: boolean;
  sessions: SessionInfo[];
}

// get_archived_sessions() 的扁平列表条目：字段与分组树里的会话条目同源，
// 额外带 archived_at（归档时刻，ISO8601 文本）。归档区没有分组语义。
export interface ArchivedSessionInfo extends SessionInfo {
  archived_at?: string;
}

// archive_session / unarchive_session 的返回契约：成功时至少回 name 与 archived_at；
// sessions / archived_sessions 是事件负载里的最新快照，桥接方法可选回传（缺省时前端主动刷新）。
export interface ArchiveSessionResult {
  ok: boolean;
  name?: string;
  archived_at?: string | null;
  sessions?: SessionGroup[];
  archived_sessions?: ArchivedSessionInfo[];
  msg?: string;
}

// unarchive_sessions / delete_archived_sessions 的返回契约：names 是本次真正处理成功
// 的会话名，failed 是逐项失败原因（一项失败不阻断其它项）。故 ok 表示「是否至少成功
// 一项」而非「是否全部成功」；全失败时 names 为空、msg 给出整体原因。
export interface ArchiveBatchResult {
  ok: boolean;
  success?: boolean;
  count?: number;
  names?: string[];
  failed?: { name: string; msg?: string }[];
  sessions?: SessionGroup[];
  archived_sessions?: ArchivedSessionInfo[];
  current_session?: string;
  msg?: string;
}

export interface FileNode {
  n: string;
  d: number;
  children?: FileNode[];
}

export interface PlanTaskItem {
  id: string;
  text: string;
  status: 'todo' | 'in_progress' | 'done';
  // 子 Agent 超时接管可见性；plan_updated 快照是可恢复事实源
  execution_mode?: string;
  owner?: string;
  takeover_required?: boolean;
  handoff_reason?: string;
  takeover_sequence?: number;
  handoff_note?: string;
}

// get_changelog() 的单条版本条目
export interface ChangelogEntry {
  version: string;
  date: string;
  notes: string[];
}

export interface PlanData {
  title: string;
  items: PlanTaskItem[];
  progress: number;
}

export interface BackgroundTask {
  task_id: string;
  command: string;
  status: string;
  running_time?: string;
  // 发起该任务的会话名（后端任务记录持久化）：执行条按当前会话过滤，
  // 其他会话启动的任务不在本会话串显；旧记录无归属时不属于任何会话
  session_id?: string;
  // 输出心跳（list_active_tasks 提供）：日志字节数 / 距最后写入秒数 /
  // 是否尚无实际命令输出（仅启动标记）。面板据此在运行期区分
  // "仍在运行只是暂无输出"与"面板失联"，旧后端缺省 undefined
  pid?: number;
  turn_id?: string;
  output_size?: number;
  last_output_age?: number | null;
  no_output_yet?: boolean;
}

// ---------------------------------------------------------------------------
// 子代理执行过程（右面板"子代理"工作台 + 聊天卡片"查看"入口）
// ---------------------------------------------------------------------------

export interface SubagentStagePayload {
  name: string;
  at: number;
  detail: string;
}

/** get_subagent_task_detail 返回的完整任务详情（含阶段历史与结论） */
export interface SubagentTaskDetail {
  id: string;
  description: string;
  type: string;
  status: string;
  stage: string;
  stage_detail?: string;
  elapsed: number;
  background: boolean;
  plan_item_id?: string;
  started_at?: number | null;
  finished_at?: number | null;
  stages: SubagentStagePayload[];
  result: string;
  error: string;
  partial: string;
}

/** 任务切换条用的轻量清单项 */
export interface SubagentTaskListItem {
  id: string;
  description: string;
  type: string;
  status: string;
  stage: string;
  elapsed: number;
  background: boolean;
}

// 上下文来源（字符数估算，仅用于悬停面板展示占比，不进 token 账本）
export type ContextBreakdownSource =
  | 'messages'
  | 'system_prompt'
  | 'skills'
  | 'runtime_context'
  | 'system_tools'
  | 'mcp_tools';

export interface ContextBreakdownItem {
  source: ContextBreakdownSource;
  chars: number;
}

export interface TokenUsage {
  used: number;
  window: number;
  cached_pct: number;
  cumulative: number;
  cache_latest_pct: number | null;
  cache_hot_pct: number | null;
  cache_session_pct: number | null;
  cache_available: boolean;
  cache_hot_turns: number;
  cache_invalidation_reason: string;
  prompt_tokens: number;
  cached_tokens: number;
  /** 当前车道身份（模型 + 接口）：命中率按车道独立，跨渠道切换时用来确认没串数 */
  cache_lane_model: string;
  cache_lane_api_base: string;
  /** 保活（cache_scope=keepalive）单独命中率；未报告为 null，界面按"隐藏"处理而非 0% */
  cache_keepalive_hot_pct: number | null;
  cache_keepalive_session_pct: number | null;
  /** CLI 状态栏口径：会话计费累计输入/输出/缓存命中 tokens */
  total_prompt: number;
  total_completion: number;
  total_cached: number;
  /** 本会话口径（token_usage 落库聚合，重启不清零）；状态行默认显示这组数字 */
  session_totals: SessionTokenTotals;
  /** 上下文来源字符占比快照（输入框悬停面板用）；无数据为 [] */
  context_breakdown: ContextBreakdownItem[];
}

/** 本会话 token 用量聚合：对话口径（main/legacy），保活单列不混入 */
export interface SessionTokenTotals {
  session_id: string;
  prompt: number;
  completion: number;
  cached: number;
  /** 命中率分母：只含已报告缓存明细的轮次 prompt（未报告 ≠ 0） */
  cache_prompt: number;
  total: number;
  requests: number;
  last_ts: number | null;
  keepalive_prompt: number;
  keepalive_cached: number;
  keepalive_requests: number;
}

/** 会话弹层的单会话行（按合计 token 降序） */
export interface TokenUsageOverviewSession {
  session_id: string;
  prompt: number;
  completion: number;
  cached: number;
  cache_prompt: number;
  total: number;
  requests: number;
  last_ts: number | null;
}

/** 会话 token 弹层数据：全部会话合计（不分 scope，含保活/压缩）+ 会话列表 */
export interface TokenUsageOverview {
  ok: boolean;
  msg?: string;
  current: string;
  totals: {
    prompt: number;
    completion: number;
    cached: number;
    cache_prompt: number;
    total: number;
    requests: number;
  };
  sessions: TokenUsageOverviewSession[];
}

export interface TokenStatisticsMetrics {
  prompt: number;
  completion: number;
  cached: number;
  cache_prompt: number;
  total: number;
  requests: number;
  cache_hit_rate: number | null;
}

export interface TokenStatisticsModel extends TokenStatisticsMetrics {
  key: string;
  provider: string;
  model: string;
}

export interface TokenStatisticsDay extends TokenStatisticsMetrics {
  date: string;
  models: TokenStatisticsModel[];
}

export interface TokenStatisticsSnapshot {
  ok: true;
  range: {
    days: 1 | 7 | 30 | 365;
    start_date: string;
    end_date: string;
  };
  summary: TokenStatisticsMetrics;
  models: TokenStatisticsModel[];
  days: TokenStatisticsDay[];
}

// get_initial_state() 的返回结构
export interface InitialState {
  models: ModelInfo[];
  providers?: ProviderInfo[];
  active_model_id: string;
  active_model_name: string;
  // 副模型（用于识图 / 压缩 / 子 Agent 等后台任务）
  secondary_model_id?: string;
  secondary_model_roles?: { vision: boolean; compression: boolean; subagent: boolean };
  permission_mode: PermissionMode;
  // 沙盒开关（true=允许访问工作目录外文件），与 CLI /sandbox 共享配置
  allow_outside_workspace?: boolean;
  system_settings?: SystemSettingsSnapshot;
  reasoning?: ReasoningSnapshot;
  sessions: SessionGroup[];
  skills: SkillInfo[];
  mcps: McpInfo[];
  workdir: string;
  file_tree?: FileNode[];
  token_usage: TokenUsage;
  version: string;
  // 仍有活跃回合的会话名（窗口重载时恢复运行态）
  active_sessions?: string[];
  // 进行中回合快照（供中途扫码或切换会话时还原正在运行的消息）
  active_turn_snapshots?: Record<string, ActiveTurnSnapshot>;
  active_turn_snapshot?: ActiveTurnSnapshot | null;
  // 各会话排队中的消息（窗口重载时恢复排队条目）
  message_queues?: Record<string, QueuedMessage[]>;
  theme_light?: boolean;
  gui_theme: GuiTheme | null;
  gui_themes: GuiThemeSummary[];
  gui_theme_error: import('../themes/types').GuiThemeError | null;
  gui_theme_list_error: import('../themes/types').GuiThemeError | null;
  native_titlebar_color_capability: NativeTitlebarColorCapability;
}

export interface ActiveTurnSnapshot {
  session_name: string;
  user_text: string;
  display_text: string;
  started_at_ms: number;
  thinking_text: string;
  streaming_text: string;
  tool_calls: Array<{
    id: string;
    name: string;
    label: string;
    display?: ToolDisplayPayload;
    started_at_ms?: number;
    ok?: boolean;
    summary?: string;
    result?: string;
    error?: string;
    duration_ms?: number;
    diff?: LegacyDiff;
  }>;
  internal_kind?: string;
}

// 工具事件 display dict：后端原样透传，字段可缺省
export interface ToolDisplayPayload {
  category?: string;
  action?: string;
  target?: string;
  command?: string;
  metrics?: Record<string, number>;
  error_summary?: string;
  is_skill?: boolean;
  background?: boolean;
}

export interface TurnStartEventData {
  user_text: string;
  display_text?: string;
  session_name?: string;
  internal?: string;
  // 按回合撤销：权威回合身份（新协议为 32 位随机 UUID），与 session_id 一起
  // 构成撤销定位键；旧后端/内部回合可能缺省
  turn_id?: string;
  session_id?: number;
}

export interface TurnUndoUpdatedEventData {
  session_id: number;
  session_name: string;
  turn_id: string;
  operation_id: string;
  status: string;
  file_count: number;
  reverted_at: number;
  owner_kind?: string;
  message: string;
}

export interface UserAttachmentNotesEventData {
  session_name: string;
  notes: string[];
}

export interface ToolStartEventData {
  id: string;
  name: string;
  label: string;
  display?: ToolDisplayPayload;
  started_at_ms?: number;
}

export interface ToolFinishEventData {
  id: string;
  name: string;
  label: string;
  ok: boolean;
  summary: string;
  result: string;
  error?: string;
  duration_ms?: number | null;
  display?: ToolDisplayPayload;
}

export interface DiffReadyEventData {
  tool_call_id: string;
  file_path: string;
  diff_text: string;
  added?: number;
  removed?: number;
}

export interface TurnErrorEventData {
  message: string;
}

export interface SubagentProgressEventData {
  tasks: Array<{
    id: string;
    description: string;
    type?: string;
    status: string;
    stage: string;
    stage_detail?: string;
    elapsed: number;
    background?: boolean;
    stages?: SubagentStagePayload[];
  }>;
}

export interface BridgeEvent {
  // 已知事件名的联合（供类型提示与检索用，不是闭集：末尾的 (string & {})
  // 兜底允许后端先推送、前端后适配的新事件；桥梁事件唯一赋值点在 appStore）
  type: BridgeEventType;
  // 既有事件信封为 {"type","data"}；电脑操控事件（Task 13a）为平铺信封，
  // data 缺省、run_id/event_seq 直接在顶层，识别规则见 appStore
  data?: any;
  run_id?: string;
  event_seq?: number;
}

/** 秘书调度台 open_session 事件的载荷（后端 gui_bridge 秘书调度服务推送） */
export interface OpenSessionBridgeData {
  /** 目标会话名（后端已用它切换当前会话；缺失时前端忽略本次跳转） */
  session_name: string;
  /** 目标会话生效显示名（秘书会话为秘书名，普通会话为标题/会话名） */
  display_name?: string;
  /** 目标会话归属目录，仅供调试/展示，前端切换不由它决定 */
  workdir?: string;
}

/** JEV 决策记录实时事件的载荷（后端 gui_bridge 决策审计监听器推送，形状同审计精简条目） */
export interface DecisionRecordedBridgeData {
  ts: string;
  scene: string;
  shadow: boolean;
  adopted: boolean;
  latency_ms: number | null;
  fallback_reason: string;
  model_id: string;
  answers_digest: string;
}

/**
 * 桥事件名联合。只登记前端 `handleBridgeEvent` 里确有处理分支的事件，
 * `(string & {})` 兜底保证既有 `BridgeEvent.type` 的宽松口径不收紧
 * （未知事件仍可传入，不会因类型收窄而编译失败）。
 */
export type BridgeEventType =
  // 秘书调度台：系统层跳转（载荷见 OpenSessionBridgeData）
  | 'open_session'
  // JEV 决策记录实时推送（载荷见 DecisionRecordedBridgeData）
  | 'decision_recorded'
  | (string & {});

// ---------------------------------------------------------------------------
// 按回合撤销 DTO（规格 19；与后端 core/turn_undo 状态值严格对应）
// ---------------------------------------------------------------------------

export type TurnUndoStatusValue =
  | 'recording' | 'ready' | 'no_changes' | 'unavailable' | 'conflicted'
  | 'reverting' | 'reverted' | 'interrupted' | 'recovering' | 'failed';

export type SnapshotAvailabilityValue =
  | 'available' | 'expired' | 'missing' | 'corrupt' | 'not_required';

export interface TurnUndoFileSummary {
  file_id: string;
  display_path: string;
  before_kind: string;
  after_kind: string | null;
  net_added: number;
  net_removed: number;
  diff_kind: string | null;
}

export interface TurnUndoStateSummary {
  checkpoint_id: string;
  turn_id: string;
  status: TurnUndoStatusValue;
  snapshot_availability: SnapshotAvailabilityValue;
  record_version: number;
  owner_kind: string;
  parent_checkpoint_id: string | null;
  description: string;
  started_at_ms: number;
  completed_at_ms: number | null;
  reverted_at_ms: number | null;
  has_uncovered_command_effects: number | boolean;
  unavailable_code: string | null;
  file_count: number;
  net_added: number;
  net_removed: number;
}

export interface TurnUndoNodeState {
  kind: string;
  exists: boolean;
  size: number | null;
  sha256: string | null;
  identity: string | null;
  link_count: number | null;
  symlink_target: string | null;
}

export interface TurnUndoFileConflict {
  file_id: string;
  display_path: string;
  expected: TurnUndoNodeState;
  actual: TurnUndoNodeState;
  reasons: string[];
}

export interface TurnUndoPreviewPayload {
  checkpoint_id: string;
  turn_id: string;
  status: TurnUndoStatusValue;
  snapshot_availability: SnapshotAvailabilityValue;
  record_version: number;
  file_count: number;
  restore_content_count: number;
  delete_created_count: number;
  restore_deleted_count: number;
  restore_path_state_count: number;
  created_files: string[];
  conflicts: TurnUndoFileConflict[];
  has_uncovered_command_effects: boolean;
  subsequent_completed_turns: number;
  workspace_root: string;
}

export interface TurnUndoExecutePayload {
  checkpoint_id: string;
  turn_id: string;
  status: string;
  operation_id: string;
  reverted_at_ms: number;
  restored_files: string[];
  deleted_created_files: string[];
  restored_deleted_files: string[];
  idempotent: boolean;
  original_reverted_at_ms: number | null;
}

export interface TurnUndoRecoveryPreviewPayload {
  operation_id: string;
  checkpoint_id: string;
  action: 'continue_revert' | 'cancel_revert';
  processed_files: string[];
  remaining_files: string[];
  unknown_files: string[];
  recovery_required: boolean;
  child_operation_id?: string | null;
}

export interface TurnUndoRecoveryExecutePayload {
  operation_id: string;
  parent_operation_id: string | null;
  action: 'continue_revert' | 'cancel_revert';
  status: string;
  checkpoint_status: string;
  affected_files: string[];
}

export interface TurnUndoErrorResponse {
  ok: false;
  code: string;
  msg: string;
  detail?: Record<string, any>;
}

export type TurnUndoStatesResponse =
  | { ok: true; states: TurnUndoStateSummary[] }
  | TurnUndoErrorResponse;

export type TurnUndoStatusResponse =
  | { ok: true; status: (TurnUndoStateSummary & { files: TurnUndoFileSummary[]; last_operation_id?: string | null }) | null }
  | TurnUndoErrorResponse;

export type TurnUndoFileDiffResponse =
  | {
      ok: true;
      diff: {
        file_id: string;
        display_path: string;
        diff_kind: string;
        diff_text: string | null;
        net_added: number;
        net_removed: number;
        before_size: number | null;
        after_size: number | null;
        truncated: boolean;
        undo_result: string | null;
      };
    }
  | TurnUndoErrorResponse;

export type TurnUndoPreviewResponse =
  | { ok: true; preview: TurnUndoPreviewPayload; preview_token: string }
  | TurnUndoErrorResponse;

export type TurnUndoExecuteResponse =
  | { ok: true; result: TurnUndoExecutePayload }
  | TurnUndoErrorResponse;

export type TurnUndoRecoveryPreviewResponse =
  | { ok: true; preview: TurnUndoRecoveryPreviewPayload; preview_token: string }
  | TurnUndoErrorResponse;

export type TurnUndoRecoveryExecuteResponse =
  | { ok: true; result: TurnUndoRecoveryExecutePayload }
  | TurnUndoErrorResponse;

// check_update() 的返回结构。cached_installer 为后端探测到的本地可复用
// 安装包绝对路径（缓存命中且通过官方 SHA256 / 大小校验），空串表示需下载；
// 旧后端不返回该字段时视为空串（向后兼容）。
export interface UpdateCheckResult {
  ok: boolean;
  has_update: boolean;
  cached_installer?: string;
  latest_version?: string;
  current_version?: string;
  release_title?: string;
  release_notes?: string;
  download_url?: string;
  asset_name?: string;
  asset_size?: number;
  // 分卷下载（Gitee 单附件 100MB 限制）：非空时 download_url 不再是直链
  part_urls?: string[];
  part_count?: number;
  is_split?: boolean;
  // 官方 SHA256（来自 Release 描述），下载完成后后端强制校验
  sha256?: string;
  msg?: string;
}

// 后端所有方法均返回 Promise<纯 JSON 对象>；大多数写操作的返回值形状
// 为 { ok: boolean, msg?: string, ...其余按方法而定 }，此处以 any 承接，
// 具体字段读取处自行按契约文档取用，避免过度约束导致类型摩擦。
export interface PyWebViewAPI {
  get_initial_state(): Promise<InitialState>;
  get_changelog(): Promise<{ ok: boolean; entries: ChangelogEntry[]; msg?: string }>;
  select_workspace_folder(): Promise<any>;
  // 纯选目录：弹原生文件夹对话框只返回路径，不切换工作区（自动化任务表单用）
  pick_folder_dialog(): Promise<{ ok: boolean; path?: string; msg?: string }>;
  get_workspace_state(workdir: string): Promise<any>;
  get_sessions(): Promise<SessionGroup[]>;
  create_session(title?: string, workdir?: string): Promise<any>;
  // 秘书会话：与 create_session 的"首条消息才懒落库"不同，它立即落库并切为当前
  // 会话（source=SECRETARY、workdir=GLOBAL）。name 是这个秘书的身份标识，必填
  // 且去首尾空白后 1-20 字符；非法名后端拒绝且不产生会话记录，返回
  // { ok:false, msg }。成功返回 { ok, session_name, title, display_name,
  // secretary_name, workdir }（display_name 即秘书名，会话名不参与命名）。
  create_secretary_session(title: string, name: string): Promise<any>;
  // 当前会话生效名：秘书会话 → 该会话的秘书名（未命名回落「秘书」），
  // 普通会话 → 设置页智能体名。前端切会话 / 建会话 / 启动后据此刷新显示名
  get_active_agent_name(): Promise<{ agent_name: string; is_secretary: boolean; msg?: string }>;
  // 秘书会话改名：只改 secretary_name，不动 session_name（消息 / 摘要 / 快照键
  // 都不迁移）；成功回带 { ok, session_name, secretary_name, display_name, sessions }
  rename_secretary_session(sessionName: string, newName: string): Promise<any>;
  switch_session(sessionName: string, targetWorkdir?: string): Promise<any>;
  load_older_messages(sessionName: string, beforeId: number): Promise<any>;
  get_session_nav_index(sessionName: string): Promise<any>;
  delete_session(sessionName: string): Promise<any>;
  delete_folder_group(workdir: string): Promise<any>;
  rename_session(oldName: string, newName: string): Promise<any>;
  // 归档会话：归档只写标记、可随时恢复，故默认列表不含归档会话（include_archived=false）
  get_archived_sessions(): Promise<ArchivedSessionInfo[]>;
  archive_session(sessionName: string): Promise<ArchiveSessionResult>;
  unarchive_session(sessionName: string): Promise<ArchiveSessionResult>;
  // 归档区多选批量操作：一次桥接处理多个会话，逐项容错（见 ArchiveBatchResult）
  unarchive_sessions(sessionNames: string[]): Promise<ArchiveBatchResult>;
  delete_archived_sessions(sessionNames: string[]): Promise<ArchiveBatchResult>;

  set_model(modelId: string, sessionName?: string): Promise<any>;
  get_reasoning_snapshot(modelId: string): Promise<any>;
  get_reasoning_snapshot_by_name(modelName: string): Promise<any>;
  set_reasoning_choice(modelId: string, choice: ReasoningChoice): Promise<any>;
  // 权限档位是会话级的：传 sessionName 只绑定该会话；不传则改全局默认
  set_permission_mode(mode: PermissionMode, sessionName?: string): Promise<any>;
  set_sandbox_enabled(enabled: boolean): Promise<any>;
  get_system_settings(): Promise<SystemSettingsSnapshot>;
  set_command_shell(shellId: string): Promise<{ ok: boolean; settings: SystemSettingsSnapshot; msg?: string }>;
  // AI 生成内容目录：不可用路径显式拒绝（ok=false + msg），不做静默降级
  set_ai_output_dir(path: string): Promise<{ ok: boolean; settings: SystemSettingsSnapshot; msg?: string }>;
  set_remote_token_ttl(seconds: number): Promise<{ ok: boolean; settings: SystemSettingsSnapshot; msg?: string }>;
  // 飞书集成：快照不含 app_secret 明文（只回 has_secret），明文按需 get_feishu_secret
  get_feishu_settings(): Promise<{ ok: boolean; settings?: FeishuSettingsSnapshot; msg?: string }>;
  set_feishu_settings(payload: {
    enabled: boolean;
    app_id: string;
    app_secret?: string;
    approvers: string[];
    notify_chat_id: string;
  }): Promise<{ ok: boolean; settings?: FeishuSettingsSnapshot; msg?: string }>;
  get_feishu_secret(): Promise<{ ok: boolean; app_secret?: string; msg?: string }>;
  test_feishu_connection(appId?: string, appSecret?: string): Promise<{ ok: boolean; msg?: string; endpoint_ready?: boolean }>;
  // JEV 决策模型（Task 3）：配置快照 + 运行时状态；后端未就绪/异常时 ok=false
  get_decision_settings(): Promise<{ ok: boolean; settings?: DecisionSettingsSnapshot; runtime?: DecisionRuntimeState; msg?: string }>;
  // 保存决策配置 patch（深合并 + 校验，非法值拒绝并回中文原因；settings 为保存后快照）
  set_decision_settings(patch: Partial<DecisionSettingsSnapshot>): Promise<{ ok: boolean; msg?: string; settings?: DecisionSettingsSnapshot }>;
  // 逐 Key 各跑 1 次无害探针（每把 Key 至多 1 次，不做压测；status 分类见 DecisionProbeKeyResult）
  probe_decision(): Promise<{ ok: boolean; results: DecisionProbeKeyResult[]; model_id: string; msg?: string }>;
  // 从上游拉取 type=="decision" 的模型列表（设置页「从上游拉取」下拉）
  list_decision_models(): Promise<{ ok: boolean; models: DecisionModelOption[]; msg?: string }>;
  // 可选调用通道列表（不含 Key 内容，仅报数量；设置页通道下拉）
  list_decision_providers(): Promise<{ ok: boolean; providers?: DecisionProviderOption[]; msg?: string }>;
  // 决策审计汇总（今日计数 + 最近记录；设置页「决策记录」面板）
  get_decision_audit_summary(limit?: number): Promise<DecisionAuditSummary>;
  // 在系统文件管理器中打开决策审计目录（缺失时先创建）
  open_decision_audit_dir(): Promise<{ ok: boolean; path?: string; msg?: string }>;
  // 决策审计尾部：~/.ycode/decision_audit 最新文件的最后 limit 行
  get_decision_audit_tail(limit?: number): Promise<{ ok: boolean; lines: string[]; msg?: string }>;
  set_agent_turn_step_limit(value: number): Promise<{ ok: boolean; settings: SystemSettingsSnapshot; msg?: string }>;
  // 上下文水位基准：0 表示不设基准（跟随模型真实窗口）
  set_context_watermark_base(value: number): Promise<{ ok: boolean; settings: SystemSettingsSnapshot; msg?: string }>;
  // /doctor 聚合自检（T3b）：设置页「运行自检」入口，复用 CLI 同一套检查
  // （core.doctor.run_checks）；skipNetwork 默认 false（含模型端点联网探测）
  run_doctor(skipNetwork?: boolean): Promise<DoctorReport>;
  // 电脑操控三个严格 setter（Task 12）：失败返回 error_code + 后端真实快照
  set_browser_control_enabled(value: boolean): Promise<SystemSettingsMutationResult>;
  set_computer_control_enabled(value: boolean): Promise<SystemSettingsMutationResult>;
  set_computer_browser(value: string): Promise<SystemSettingsMutationResult>;
  // 电脑操控运行控制（Task 13a）：快照字段平铺返回（含失败），前端校准 Store
  get_computer_control_status(): Promise<ComputerControlResult>;
  pause_computer_control(runId: string): Promise<ComputerControlResult>;
  resume_computer_control(runId: string): Promise<ComputerControlResult>;
  stop_computer_control(runId: string): Promise<ComputerControlResult>;
  // Provider 强制安全检查确认：官方契约验证完成前固定不可用（Task 11b 条件）
  acknowledge_computer_safety(runId: string, checkIds?: string[]): Promise<ComputerControlResult>;
  get_permission_rules(): Promise<any>;
  add_permission_rule(ruleType: 'command' | 'write' | 'edit', value: string): Promise<any>;
  remove_permission_rule(ruleType: 'command' | 'write' | 'edit', value: string): Promise<any>;
  clear_permission_rules(): Promise<any>;
  respond_permission(requestId: string, allow: boolean, always: boolean): Promise<any>;
  respond_permission_option(requestId: string, optionKey: string): Promise<any>;
  respond_permission_choice(requestId: string, choiceIndex: number, customText?: string): Promise<any>;

  send_user_message(text: string, sessionName?: string, attachments?: string[], workdir?: string, modelId?: string, permissionMode?: string): Promise<any>;
  // 客户端本地提示（notice/error 卡片）经桥广播到全部客户端（桌面 + 手机端）；
  // 各端收到 notice 桥事件后统一回环插入，提示为瞬态、不落历史
  post_notice(message: string, sessionName?: string, kind?: 'notice' | 'error'): Promise<any>;
  stop_current_turn(sessionName?: string): Promise<any>;
  // 会话消息排队：查询 / 取消排队 / 插队发送（停止当前回合优先补发该消息）
  get_message_queue(sessionName?: string): Promise<{ ok: boolean; session_name: string; queue: QueuedMessage[] }>;
  cancel_queued_message(sessionName: string, messageId: string): Promise<any>;
  jump_queue(sessionName: string, messageId: string): Promise<any>;

  get_token_usage(): Promise<TokenUsage>;
  get_token_usage_overview(limit?: number): Promise<TokenUsageOverview>;
  get_token_usage_statistics(days: 1 | 7 | 30 | 365): Promise<TokenStatisticsSnapshot | { ok: false; msg: string }>;

  toggle_skill(skillId: string, enabled: boolean): Promise<any>;
  refresh_skills(): Promise<{ ok: boolean; skills?: SkillInfo[]; msg?: string }>;
  // 用内置源覆盖用户改动后的技能副本；成功时回传最新技能列表
  restore_skill(skill_id: string): Promise<{ ok: boolean; msg?: string; skills?: SkillInfo[] }>;
  get_skills(): Promise<SkillInfo[]>;
  // 星轨自动化任务
  get_automation_tasks(): Promise<{ ok: boolean; tasks?: AutomationTaskInfo[]; msg?: string }>;
  save_automation_task(task: Record<string, any>): Promise<{ ok: boolean; id?: string; errors?: string[]; msg?: string }>;
  delete_automation_task(taskId: string): Promise<{ ok: boolean; deleted?: boolean; msg?: string }>;
  set_automation_task_enabled(taskId: string, enabled: boolean): Promise<{ ok: boolean; msg?: string }>;
  run_automation_task_now(taskId: string): Promise<{ ok: boolean; run_id?: string; msg?: string }>;
  get_automation_runs(taskId?: string, limit?: number): Promise<{ ok: boolean; runs?: AutomationRunInfo[]; msg?: string }>;
  clear_automation_runs(taskId: string): Promise<{ ok: boolean; msg?: string }>;
  get_plugins(): Promise<any>;
  toggle_plugin(name: string, enabled: boolean): Promise<any>;
  trust_plugin(name: string): Promise<any>;
  // 插件设置页（声明式渲染）：页面快照读取 / 动作执行 / 打开外部授权链接
  get_plugin_settings(name: string): Promise<any>;
  run_plugin_settings_action(name: string, action: string, params?: Record<string, unknown>): Promise<any>;
  open_external_url(url: string): Promise<{ ok: boolean; msg?: string }>;
  // C11：项目级 Hook 信任（信任卡片数据源 + 信任/拒绝/撤销/重置动作）
  get_hooks(): Promise<{ ok: boolean; hooks?: HookInfo[]; workdir?: string; msg?: string }>;
  grant_hook_trust(digest: string): Promise<any>;
  deny_hook_trust(digest: string): Promise<any>;
  revoke_hook_trust(digest: string): Promise<any>;
  reset_hook_trust(digest: string): Promise<any>;
  // C13：Hook 手动配置（新建/编辑/删除/启停；成功后回传刷新列表）
  save_hook(payload: Record<string, any>): Promise<{ ok: boolean; msg?: string; hooks?: HookInfo[]; workdir?: string }>;
  delete_hook(sourcePath: string, digest: string): Promise<{ ok: boolean; msg?: string; hooks?: HookInfo[]; workdir?: string }>;
  set_hook_disabled(sourcePath: string, digest: string, disabled: boolean): Promise<{ ok: boolean; msg?: string; hooks?: HookInfo[]; workdir?: string }>;
  get_mcp_servers(): Promise<McpInfo[]>;
  toggle_mcp_server(name: string, enabled: boolean): Promise<any>;
  add_mcp_server(name: string, command: string, args: string[], env?: Record<string, string>, enabled?: boolean): Promise<any>;
  remove_mcp_server(name: string): Promise<any>;
  reconnect_mcp(): Promise<any>;

  export_session(fmt: 'md' | 'html' | 'json'): Promise<any>;
  clear_session_history(sessionName: string): Promise<any>;

  // ------------------------------------------------------------------
  // 按回合撤销（规格 19.1 窄桥接）：会话名由后端解析为 sessions.id
  // ------------------------------------------------------------------
  list_turn_undo_states(sessionName: string): Promise<TurnUndoStatesResponse>;
  get_turn_undo_status(sessionName: string, turnId: string): Promise<TurnUndoStatusResponse>;
  get_turn_undo_file_diff(sessionName: string, turnId: string, fileId: string): Promise<TurnUndoFileDiffResponse>;
  preview_turn_undo(sessionName: string, turnId: string): Promise<TurnUndoPreviewResponse>;
  execute_turn_undo(sessionName: string, turnId: string, previewToken: string): Promise<TurnUndoExecuteResponse>;
  preview_turn_undo_recovery(sessionName: string, operationId: string, action: 'continue_revert' | 'cancel_revert'): Promise<TurnUndoRecoveryPreviewResponse>;
  execute_turn_undo_recovery(sessionName: string, operationId: string, action: 'continue_revert' | 'cancel_revert', previewToken: string): Promise<TurnUndoRecoveryExecuteResponse>;

  read_file_preview(path: string): Promise<any>;
  check_paths_kind(paths: string[]): Promise<Array<{ path: string; kind: 'file' | 'dir' | 'missing' }>>;
  open_path(path: string): Promise<any>;
  open_folder_in_explorer(path: string): Promise<any>;
  // 侧栏排序快照：权威副本在数据目录 ui_layout.json，localStorage 只是缓存
  // （WebView2 的 localStorage 在"拖完立刻重启"时会被 leveldb 丢弃）
  get_ui_layout(): Promise<{ ok: boolean; group_order?: string[]; session_order?: Record<string, string[]>; msg?: string }>;
  save_ui_layout(layout: { group_order: string[]; session_order: Record<string, string[]> }): Promise<{ ok: boolean; msg?: string }>;
  pick_files(): Promise<any>;
  get_files_info(paths: string[]): Promise<any>;   save_clipboard_image(dataUrl: string): Promise<any>;
  get_attachment_thumbnails(paths: string[]): Promise<any>;
   // 手机/平板端文件上传（WS RPC）
   upload_file(name: string, dataB64: string): Promise<any>;// 拖拽 drop 桥接：取回 Python 侧已存储的拖拽文件完整路径
  report_dropped_files(event: any): Promise<{ ok: boolean; paths?: string[] }>;
  // @ 引用自动补全：工作区文件子串匹配
  search_workspace_files(query: string, limit?: number): Promise<any>;
  // @ 提及对话自动补全：历史会话子串匹配（排除当前会话）
  // currentSession：用户实际所在的会话名；草稿新会话传空串，表示不排除任何会话
  search_sessions(query: string, limit?: number, currentSession?: string): Promise<{
    ok: boolean;
    sessions?: { name: string; count: number; updated_at: string; workdir: string }[];
    msg?: string;
  }>;
  // 会话多维度全局检索（标题模糊匹配 + 对话内容全文检索）
  search_sessions_full(
    query: string,
    scope?: 'all' | 'title' | 'content',
    workdir?: string,
    limit?: number,
    // 是否把已归档会话也纳入检索范围（默认 false：与归档前行为一致）
    includeArchived?: boolean,
  ): Promise<{
    ok: boolean;
    total: number;
    results: {
      type: 'title' | 'content';
      session_name: string;
      display_name: string;
      workdir: string;
      workdir_label: string;
      updated_at: string;
      msg_count: number;
      role?: 'user' | 'assistant' | null;
      snippet?: string | null;
    }[];
    msg?: string;
  }>;
  get_dir_children(path: string): Promise<any>;
  get_file_tree(workdir: string): Promise<any>;
  set_titlebar_theme(dark: boolean): Promise<any>;
  // 自绘标题栏窗口控制（frameless 模式）
  window_minimize(): Promise<{ ok: boolean; error?: string }>;
  window_toggle_maximize(): Promise<{ ok: boolean; maximized?: boolean; error?: string }>;
  // 原生最大化/还原（SC_MAXIMIZE/SC_RESTORE），等价标题栏 HTCAPTION 双击；可缺省
  window_native_maximize?(): Promise<{ ok: boolean; maximized?: boolean; error?: string }>;
  window_close(): Promise<{ ok: boolean; error?: string }>;
  window_is_maximized(): Promise<{ ok: boolean; maximized?: boolean }>;
  window_begin_drag(): Promise<{ ok: boolean; error?: string }>;
  window_begin_resize(edge: 'left' | 'right' | 'top' | 'bottom' | 'top_left' | 'top_right' | 'bottom_left' | 'bottom_right'): Promise<{ ok: boolean; error?: string | null }>;
  window_confirm_close(choice: 'exit' | 'background' | 'cancel'): Promise<{ ok: boolean; error?: string }>;
  set_gui_theme(theme_light: boolean): Promise<GuiThemeResult<{
    theme: GuiTheme;
    native_titlebar_color_capability: NativeTitlebarColorCapability;
  }>>;
  list_gui_themes(): Promise<GuiThemeResult<{ themes: GuiThemeSummary[] }>>;
  get_gui_theme(themeId: string): Promise<GuiThemeResult<{ theme: GuiTheme }>>;
  get_active_gui_theme(): Promise<GuiThemeResult<{
    theme: GuiTheme;
    native_titlebar_color_capability: NativeTitlebarColorCapability;
  }>>;
  save_gui_theme(theme: GuiTheme): Promise<GuiThemeResult<{ theme: GuiTheme }>>;
  set_active_gui_theme(themeId: string): Promise<GuiThemeResult<{
    theme: GuiTheme;
    native_titlebar_color_capability: NativeTitlebarColorCapability;
  }>>;
  copy_gui_theme(sourceId: string, newId: string, newName: string): Promise<GuiThemeResult<{ theme: GuiTheme }>>;
  rename_gui_theme(themeId: string, newId: string, newName: string): Promise<GuiThemeResult<{ theme: GuiTheme }>>;
  delete_gui_theme(themeId: string): Promise<GuiThemeResult>;
  import_gui_theme(): Promise<GuiThemeResult<{ theme: GuiTheme }>>;
  export_gui_theme(themeId: string): Promise<GuiThemeResult<{ path: string }>>;
  // 图片另存为：url 必须是本进程媒体服务签发的地址；cancelled 表示用户取消
  save_media_image(url: string, suggestedName?: string): Promise<{
    ok: boolean;
    path?: string;
    msg?: string;
    cancelled?: boolean;
  }>;
  import_gui_theme_image(themeId: string, region: ThemeRegionId): Promise<GuiThemeResult<{ asset_path: string }>>;
  remove_gui_theme_image(themeId: string, region: ThemeRegionId): Promise<GuiThemeResult<{ theme: GuiTheme }>>;
  read_gui_theme_asset(themeId: string, assetPath: string): Promise<GuiThemeResult<{
    mime: string;
    base64: string;
  }>>;
  get_gui_theme_asset(themeId: string, assetPath: string): Promise<GuiThemeResult<{
    mime: string;
    base64: string;
  }>>;
  preview_native_titlebar(nativeTitlebar: NativeTitlebarTheme): Promise<GuiThemeResult<{
    native_titlebar_color_capability: NativeTitlebarColorCapability;
  }>>;
  restore_native_titlebar(): Promise<GuiThemeResult<{
    theme: GuiTheme;
    native_titlebar_color_capability: NativeTitlebarColorCapability;
  }>>;

  get_background_tasks(): Promise<BackgroundTask[]>;
  get_background_task_output(taskId: string, tailLines?: number): Promise<{ ok: boolean; output: string; msg?: string }>;
  kill_task(taskId: string): Promise<any>;

  // 子代理执行过程详情（右面板"子代理"工作台轮询）：task 为目标任务完整详情，
  // tasks 为会话内全部子代理轻量清单；任务被容量清理时 ok=false 保留 msg
  get_subagent_task_detail(taskId: string, sessionName?: string): Promise<{
    ok: boolean;
    task?: SubagentTaskDetail | null;
    tasks: SubagentTaskListItem[];
    msg?: string;
  }>;

  // ---------------------------------------------------------------------------
  // 内嵌浏览器（右侧面板浏览器页签；宿主 gui_browser_mixin.BrowserBridgeMixin）
  // rect 为 DOM 占位区的 CSS px 矩形；url 为空串表示只创建/定位、不导航。
  // 非 Windows 或缺桥时统一返回 {ok:false,error:'unsupported_platform'}。
  // ---------------------------------------------------------------------------
  browser_pane_open(rect: EmbeddedBrowserRect, url?: string): Promise<EmbeddedBrowserPaneResult>;
  browser_pane_set_bounds(rect: EmbeddedBrowserRect): Promise<EmbeddedBrowserPaneResult>;
  browser_pane_navigate(url: string): Promise<EmbeddedBrowserPaneResult>;
  browser_pane_back(): Promise<EmbeddedBrowserPaneResult>;
  browser_pane_forward(): Promise<EmbeddedBrowserPaneResult>;
  browser_pane_reload(): Promise<EmbeddedBrowserPaneResult>;
  browser_pane_stop(): Promise<EmbeddedBrowserPaneResult>;
  browser_pane_hide(): Promise<EmbeddedBrowserPaneResult>;
  browser_pane_get_state(): Promise<EmbeddedBrowserPaneResult>;
  // 把前端渲染好的预览 HTML 落盘（宿主 gui_browser_mixin.BrowserBridgeMixin，
  // 实现 core.web_preview）：返回本地路径后由 browser_pane_navigate 打开。
  // 与平台无关；内容为空/过大时返回 {ok:false,error}。
  write_preview_page(
    htmlText: string,
    sourcePath?: string,
  ): Promise<{ ok: boolean; path?: string; error?: string }>;

  // ---------------------------------------------------------------------------
  // 右面板画笔截图（宿主 gui_snip_mixin.SnipBridgeMixin）。rect 为右面板的
  // CSS px 矩形（与 browser_pane_open 同一坐标系），冻结帧必须在其原生表面
  // 仍可见时捕获（快照要包含浏览器内容）。所有失败归一 {ok:false,msg}。
  // ---------------------------------------------------------------------------
  capture_right_panel_region(rect: EmbeddedBrowserRect): Promise<{
    ok: boolean;
    data_url?: string;
    width?: number;
    height?: number;
    msg?: string;
  }>;
  snip_copy_clipboard(dataUrl: string): Promise<{ ok: boolean; msg?: string }>;
  snip_save_image(dataUrl: string, suggestedName?: string): Promise<{
    ok: boolean;
    path?: string;
    msg?: string;
  }>;
  // 屏幕截图（宿主 gui_snip_mixin.SnipBridgeMixin）：全屏任意位置截图，
  // 打开遮罩窗口；「发到输入框」完成后的结果经桥事件
  // {type:'snip_attachment_ready', data:{path}} 回传
  screen_snip_start(): Promise<{ ok: boolean; msg?: string }>;

  // 旧版右侧面板单命令终端已下线；底部交互式终端见 pty_terminal_*
  pty_terminal_ensure(cols: number, rows: number): Promise<{
    ok: boolean; pid?: number; cwd?: string; reused?: boolean; msg?: string;
  }>;
  pty_terminal_write(data: string): Promise<{ ok: boolean; msg?: string }>;
  pty_terminal_resize(cols: number, rows: number): Promise<{ ok: boolean; msg?: string }>;
  pty_terminal_kill(): Promise<{ ok: boolean; msg?: string }>;

  add_provider(name: string, baseUrl: string, apiKey: string, protocol: string): Promise<any>;
  get_model_settings(): Promise<ModelSettingsSnapshot>;
  add_preset_provider(presetId: string, apiKey: string): Promise<any>;
  update_provider(providerId: string, name: string, baseUrl: string, apiKey: string, protocol: string): Promise<any>;
  get_provider_api_key(providerId: string): Promise<{ ok: boolean; api_key?: string; msg?: string }>;
  delete_provider(providerId: string): Promise<any>;
  add_model(providerId: string, modelName: string, contextWindow: number, supportsVision: boolean, supportsFunctionCalling?: boolean, computerProtocol?: ComputerProtocolChoice, maxTokens?: number | null, reasoningChoice?: string | null): Promise<any>;
  fetch_provider_models(providerId: string): Promise<{ ok: boolean; msg?: string; models?: { id: string; model_type?: string; context_window?: number | null; supports_vision?: boolean | null; max_tokens?: number | null }[]; count?: number }>;
  update_model(providerId: string, modelId: string, modelName: string, contextWindow: number, supportsVision: boolean, supportsFunctionCalling?: boolean, computerProtocol?: ComputerProtocolChoice, maxTokens?: number | null, reasoningChoice?: string | null): Promise<any>;
  delete_model(modelId: string): Promise<any>;
  test_model(modelId: string): Promise<any>;
  set_secondary_model(modelId: string): Promise<any>;
  set_secondary_roles(roles: { vision: boolean; compression: boolean; subagent: boolean }): Promise<any>;

  check_update(manual?: boolean): Promise<UpdateCheckResult>;
  // 分卷模式：partUrls 为 .part1..partN 直链序列（Gitee 单附件 100MB 限制），
  // 下载器顺序下载拼接还原 exe；sha256 为 Release 描述里发布的官方校验和，
  // 非空时强制校验，不匹配即拒绝安装；totalSize 为各分卷字节数之和（进度分母）
  download_update(downloadUrl: string, targetVersion: string, partUrls?: string[], sha256?: string, totalSize?: number): Promise<any>;
  cancel_download_update(): Promise<any>;
  has_active_tasks(): Promise<any>;
  launch_installer(filePath: string): Promise<any>;

  // ---------------------------------------------------------------------------
  // Git 工作台（规格 §11/§13；桥接层 gui_git_mixin.GitBridgeMixin）
  // ---------------------------------------------------------------------------
  git_get_status(workdir: string): Promise<GitBridgeResult>;
  git_stage(workdir: string, paths: string[]): Promise<GitBridgeResult>;
  git_stage_all(workdir: string): Promise<GitBridgeResult>;
  git_unstage(workdir: string, paths: string[]): Promise<GitBridgeResult>;
  git_commit(workdir: string, title: string, body?: string, amend?: boolean): Promise<GitBridgeResult>;
  git_list_branches(workdir: string): Promise<GitBridgeResult>;
  git_switch_branch(workdir: string, target: string): Promise<GitBridgeResult>;
  git_create_branch(workdir: string, name: string, startPoint?: string, switchAfter?: boolean): Promise<GitBridgeResult>;
  git_fetch(workdir: string, remote?: string, prune?: boolean): Promise<GitBridgeResult>;
  git_pull(workdir: string): Promise<GitBridgeResult>;
  git_push(workdir: string, remote?: string, refspec?: string, forceWithLease?: boolean, token?: string): Promise<GitBridgeResult>;
  git_load_history(workdir: string, skip?: number, limit?: number): Promise<GitBridgeResult>;
  git_load_diff(workdir: string, path?: string, staged?: boolean, left?: string, right?: string): Promise<GitBridgeResult>;
  git_load_reflog(workdir: string, skip?: number, limit?: number): Promise<GitBridgeResult>;
  git_stash_create(workdir: string, message: string, includeUntracked: boolean): Promise<GitBridgeResult>;
  git_preview_risk(workdir: string, operation: string, params: Record<string, any>): Promise<GitBridgeResult>;
  git_confirm_operation(workdir: string, operation: string, params: Record<string, any>, token: string): Promise<GitBridgeResult>;
  git_get_trust(workdir: string): Promise<GitBridgeResult>;
  git_allow_once(workdir: string): Promise<GitBridgeResult>;
  git_trust_repository(workdir: string): Promise<GitBridgeResult>;
  git_load_conflict(workdir: string, path: string): Promise<GitBridgeResult>;
  git_resolve_conflict(workdir: string, path: string, resultText?: string, side?: string): Promise<GitBridgeResult>;
  git_recover_branch(workdir: string, oid: string, name: string): Promise<GitBridgeResult>;
  git_ai_assist(workdir: string, kind: string): Promise<GitBridgeResult>;
}

// ---------------------------------------------------------------------------
// Git 工作台 DTO（与 core/git 桥接层序列化结构对应；字段只含界面所需）
// ---------------------------------------------------------------------------

export interface GitBridgeResult {
  ok: boolean;
  snapshot?: GitSnapshotDto;
  error?: GitErrorDto;
  branches?: GitBranchDto[];
  commits?: GitCommitDto[];
  has_more?: boolean;
  diff?: GitDiffDto;
  entries?: GitReflogDto[];
  tags?: GitTagDto[];
  remotes?: GitRemoteDto[];
  stashes?: GitStashDto[];
  lines?: [string, number, string][];
  preview?: GitRiskPreviewDto;
  worktrees?: GitWorktreeDto[];
  conflict?: GitConflictContentDto;
  plan?: GitRebasePlanLineDto[];
  level?: 'trusted' | 'allowed_once' | 'untrusted';
  fingerprint?: string;
}

export interface GitErrorDto {
  code: string;
  message: string;
  retryable: boolean;
  command: string;
}

export interface GitFileChangeDto {
  path: string;
  index_status: string;
  worktree_status: string;
  orig_path: string | null;
  is_binary: boolean;
}

export interface GitConflictDto {
  path: string;
  kind: string;
}

export interface GitSnapshotDto {
  repository: { worktree_root: string; common_git_dir: string; git_version: string; is_linked_worktree: boolean };
  head_oid: string;
  branch_name: string | null;
  head_state: 'normal' | 'detached' | 'unborn';
  upstream: string | null;
  ahead: number;
  behind: number;
  staged: GitFileChangeDto[];
  unstaged: GitFileChangeDto[];
  untracked: string[];
  ignored_count: number;
  conflicts: GitConflictDto[];
  stash_count: number;
  operation_kind: string;
  operation_phase: string;
  identity_state: string;
  worktree_version: string;
  refs_version: string;
}

export interface GitBranchDto {
  name: string;
  is_remote: boolean;
  is_current: boolean;
  upstream: string | null;
  ahead: number;
  behind: number;
  upstream_gone: boolean;
  oid: string;
}

export interface GitCommitDto {
  oid: string;
  short: string;
  parents: string[];
  author_name: string;
  author_email: string;
  author_time: number;
  committer_time: number;
  summary: string;
  refs: string[];
}

export interface GitDiffHunkDto {
  old_start: number;
  old_lines: number;
  new_start: number;
  new_lines: number;
  header: string;
  lines: string[];
}

export interface GitDiffDto {
  raw_text: string;
  is_binary: boolean;
  hunks: GitDiffHunkDto[];
}

export interface GitReflogDto {
  oid: string;
  selector: string;
  message: string;
}

export interface GitTagDto {
  name: string;
  oid: string;
  is_annotated: boolean;
  message: string;
}

export interface GitRemoteDto {
  name: string;
  fetch_url: string;
  push_url: string;
}

export interface GitStashDto {
  index: number;
  message: string;
  oid: string;
}

export interface GitRiskPreviewDto {
  operation: string;
  risk_level: string;
  confirm_required: boolean;
  typed_confirm_required: boolean;
  confirm_text: string;
  versions: { worktree_version: string | null; refs_version: string | null };
  token: string | null;
}

export interface GitWorktreeDto {
  path: string;
  head_oid: string;
  branch: string | null;
  is_bare: boolean;
}

export interface GitConflictContentDto {
  path: string;
  base: string | null;
  ours: string | null;
  theirs: string | null;
  worktree: string | null;
}

export interface GitRebasePlanLineDto {
  action: string;
  oid: string;
  summary: string;
  plan?: GitRebasePlanLineDto[];
  conflict?: GitConflictContentDto;
}

declare global {
  interface Window {
    pywebview?: {
      api: PyWebViewAPI;
    };
    onBridgeEvent?: (event: BridgeEvent) => void;
  }
}

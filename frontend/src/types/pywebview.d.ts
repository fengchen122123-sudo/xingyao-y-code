// Python <-> pywebview 桥接层类型定义
// 本文件按后端桥接 API 契约编写（而非当前 gui_bridge.py 的实现），
// 后端会单独重写以匹配此契约。

export type PermissionMode = 'confirm' | 'auto' | 'plan' | 'full';

export interface ModelInfo {
  id: string;
  name: string;
  provider_name: string;
}

export interface ProviderInfo {
  id: string;
  name: string;
  base_url?: string;
  protocol?: string;
}

export interface SkillInfo {
  id: string;
  short_id?: string;
  name: string;
  description: string;
  enabled: boolean;
}

export interface McpInfo {
  name: string;
  enabled: boolean;
  connected: boolean;
  tools_count: number;
}

export interface SessionInfo {
  name: string;
  display_name: string;
  count: number;
  workdir: string;
  is_global?: boolean;
}

export interface SessionGroup {
  folder_path: string;
  folder_name: string;
  is_current?: boolean;
  is_global?: boolean;
  sessions: SessionInfo[];
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
}

export interface TokenUsage {
  used: number;
  window: number;
  cached_pct: number;
  cumulative: number;
}

// get_initial_state() 的返回结构
export interface InitialState {
  models: ModelInfo[];
  active_model_id: string;
  active_model_name: string;
  permission_mode: PermissionMode;
  sessions: SessionGroup[];
  skills: SkillInfo[];
  mcps: McpInfo[];
  workdir: string;
  file_tree?: FileNode[];
  token_usage: TokenUsage;
  version: string;
}

// window.onBridgeEvent 收到的事件信封；具体 data 形状随 type 变化，
// 详见 appStore.ts 的 handleBridgeEvent 分支注释。
export interface BridgeEvent {
  type: string;
  data: any;
}

// 后端所有方法均返回 Promise<纯 JSON 对象>；大多数写操作的返回值形状
// 为 { ok: boolean, msg?: string, ...其余按方法而定 }，此处以 any 承接，
// 具体字段读取处自行按契约文档取用，避免过度约束导致类型摩擦。
export interface PyWebViewAPI {
  get_initial_state(): Promise<InitialState>;
  select_workspace_folder(): Promise<any>;
  get_sessions(): Promise<SessionGroup[]>;
  create_session(title?: string, workdir?: string): Promise<any>;
  create_global_session(title?: string): Promise<any>;
  switch_session(sessionName: string, targetWorkdir?: string): Promise<any>;
  delete_session(sessionName: string): Promise<any>;
  delete_folder_group(workdir: string): Promise<any>;
  rename_session(oldName: string, newName: string): Promise<any>;

  set_model(modelId: string): Promise<any>;
  set_permission_mode(mode: PermissionMode): Promise<any>;
  respond_permission(requestId: string, allow: boolean, always: boolean): Promise<any>;

  send_user_message(text: string, sessionName?: string): Promise<any>;
  stop_current_turn(): Promise<any>;

  get_token_usage(): Promise<TokenUsage>;

  toggle_skill(skillId: string, enabled: boolean): Promise<any>;
  get_mcp_servers(): Promise<McpInfo[]>;
  toggle_mcp_server(name: string, enabled: boolean): Promise<any>;
  reconnect_mcp(): Promise<any>;

  export_session(fmt: 'md' | 'html' | 'json'): Promise<any>;
  clear_session_history(sessionName: string): Promise<any>;
  undo_last_edit(): Promise<any>;

  read_file_preview(path: string): Promise<any>;
  open_path(path: string): Promise<any>;
  pick_files(): Promise<any>;

  get_background_tasks(): Promise<BackgroundTask[]>;
  kill_task(taskId: string): Promise<any>;

  run_terminal_command(command: string): Promise<any>;
  kill_terminal_command(): Promise<any>;

  add_provider(name: string, baseUrl: string, apiKey: string, protocol: string): Promise<any>;
  add_model(providerId: string, modelName: string, contextWindow: number): Promise<any>;
  delete_model(modelId: string): Promise<any>;
  test_model(modelId: string): Promise<any>;
}

declare global {
  interface Window {
    pywebview?: {
      api: PyWebViewAPI;
    };
    onBridgeEvent?: (event: BridgeEvent) => void;
  }
}

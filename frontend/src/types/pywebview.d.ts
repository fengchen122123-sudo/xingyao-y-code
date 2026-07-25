export interface ModelInfo {
  id: string;
  name: string;
  litellm_model?: string;
  provider?: string;
}

export interface SkillInfo {
  id: string;
  name: string;
  description: string;
}

export interface McpInfo {
  name: string;
  description: string;
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

export interface ToolCallItem {
  id: string;
  function: {
    name: string;
    arguments: any;
  };
  summary?: string;
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

export interface InitialState {
  workdir: string;
  current_session: string;
  sessions: SessionGroup[];
  current_model: string;
  models: ModelInfo[];
  permission_mode: string;
  skills: SkillInfo[];
  mcps: McpInfo[];
  file_tree: FileNode[];
}

export interface BridgeEvent {
  type: string;
  data: any;
}

export interface PyWebViewAPI {
  get_initial_state(): Promise<InitialState>;
  get_sessions(): Promise<SessionGroup[]>;
  select_workspace_folder(): Promise<any>;
  create_session(title?: string, workdir?: string): Promise<any>;
  create_global_session(title?: string): Promise<any>;
  switch_session(sessionName: string, targetWorkdir?: string): Promise<any>;
  rename_session(oldName: string, newName: string): Promise<boolean>;
  delete_session(sessionName: string): Promise<any>;
  delete_folder_group(workdir: string): Promise<any>;
  set_model(modelName: string): Promise<boolean>;
  set_permission_mode(name: string, val: string): Promise<boolean>;
  respond_permission(reqId: string, allowed: boolean, always: boolean): Promise<boolean>;
  send_user_message(text: string, sessionName?: string): Promise<any>;
  stop_current_turn(): Promise<boolean>;
  execute_terminal_cmd(cmd: str): Promise<{ exit_code: number; stdout: string; stderr: string }>;
  get_background_tasks(): Promise<BackgroundTask[]>;
  kill_task(taskId: string): Promise<boolean>;
}

declare global {
  interface Window {
    pywebview?: {
      api: PyWebViewAPI;
    };
    onBridgeEvent?: (event: BridgeEvent) => void;
  }
}

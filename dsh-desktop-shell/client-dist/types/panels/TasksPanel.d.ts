/**
 * TasksPanel —— 任务面板（接 DSH 后台任务 JobView[]）。
 *
 * 数据接入方式（已确认）：
 * 后台任务列表经全局 slot 标准 hook `useSessions` 读取：
 *   useSessions(s => (s.current ? s.jobsBySession[s.current] : undefined) ?? [])
 * SessionListState 含 current: SessionId|undefined 与
 * jobsBySession: Record<SessionId, readonly JobView[]>（缺失 key 视为空）。
 * useSessions 由父/slot 注入到 props；缺省时容错为空数组（渲染空态）。
 *
 * JobView 类型（@deepseek-ai/dsh-host-apiproxy/api/jobs）：
 *   { id: string; kind: string; label: string;
 *     status: 'running'|'stopping'|'completed'|'killed'|'failed';
 *     detail?: string; startedAt: number; finishedAt?: number }
 *
 * 注意：终止 job 的 client 接口尚未确认公开，"终止"按钮只做 UI + 预留
 * onKill 回调（props.onKill?(id)），不强制连真实接口。
 *
 * 视觉对齐 ycode RightPanel.vue 的 tasks 区：顶部标题行 + 刷新按钮，
 * 下方任务卡片列表（label + 状态 + 耗时/detail + 终止按钮）。
 * CSS 变量一律 var(--yc-*)。
 */
/** DSH JobView（与 @deepseek-ai/dsh-host-apiproxy/api/jobs 对齐） */
export interface JobView {
    id: string;
    kind: string;
    label: string;
    status: "running" | "stopping" | "completed" | "killed" | "failed";
    detail?: string;
    startedAt: number;
    finishedAt?: number;
}
/**
 * 任务耗时格式化：(finishedAt ?? now) - startedAt。
 * 对齐 ycode fmtWorkTime：秒 < 60 用 `${s}s`，否则 `${m}m${s%60}s`。
 * 纯函数，便于单测。
 */
export declare function formatElapsed(job: JobView, now?: number): string;
/** 状态文案映射。纯函数，便于单测。 */
export declare function statusLabel(status: JobView["status"]): string;
export interface TasksPanelProps {
    /** 框架注入的全局 slot hook；缺省时按空数组容错。 */
    useSessions?: (sel: (s: any) => any) => any;
    /** 终止任务回调（预留，client 接口确认后由父注入）。 */
    onKill?: (id: string) => void;
    /** 刷新回调（可选）。 */
    onRefresh?: () => void;
}
export declare function TasksPanel({ useSessions, onKill, onRefresh }: TasksPanelProps): import("react").JSX.Element;
export default TasksPanel;

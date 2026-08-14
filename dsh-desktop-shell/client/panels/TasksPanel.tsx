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
export function formatElapsed(job: JobView, now: number = Date.now()): string {
	const ms = Math.max(0, (job.finishedAt ?? now) - job.startedAt);
	const s = Math.floor(ms / 1000);
	if (s < 60) return `${s}s`;
	return `${Math.floor(s / 60)}m${s % 60}s`;
}

/** 状态文案映射。纯函数，便于单测。 */
export function statusLabel(status: JobView["status"]): string {
	switch (status) {
		case "running":
			return "运行中";
		case "stopping":
			return "停止中";
		case "completed":
			return "已完成";
		case "killed":
			return "已终止";
		case "failed":
			return "失败";
		default:
			return status;
	}
}

export interface TasksPanelProps {
	/** 框架注入的全局 slot hook；缺省时按空数组容错。 */
	useSessions?: (sel: (s: any) => any) => any;
	/** 终止任务回调（预留，client 接口确认后由父注入）。 */
	onKill?: (id: string) => void;
	/** 刷新回调（可选）。 */
	onRefresh?: () => void;
}

const css = `
.yc-tasks { display: flex; flex-direction: column; gap: 8px; }
.yc-tasks-head { display: flex; justify-content: space-between; align-items: center; }
.yc-tasks-title { font-size: 12px; font-weight: 600; color: var(--yc-text-dim); }
.yc-tasks-refresh { font-size: 11px; padding: 2px 8px; border-radius: 99px; background: var(--yc-bg2); border: 1px solid var(--yc-border); color: var(--yc-text-dim); cursor: pointer; }
.yc-tasks-refresh:hover { border-color: var(--yc-accent); color: var(--yc-text); }
.yc-tasks-empty { color: var(--yc-text-faint); font-size: 12px; padding: 12px 0; }
.yc-task-card { background: var(--yc-bg2); border: 1px solid var(--yc-border); border-radius: 8px; padding: 9px 12px; display: flex; flex-direction: column; gap: 6px; }
.yc-task-row { display: flex; justify-content: space-between; align-items: center; gap: 8px; }
.yc-task-label { font-family: var(--yc-mono); font-size: 12px; font-weight: 600; color: var(--yc-text); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.yc-task-status { font-size: 11px; flex-shrink: 0; }
.yc-task-status.yc-st-ok { color: var(--yc-green); }
.yc-task-status.yc-st-err { color: var(--yc-red); }
.yc-task-status.yc-st-warn { color: var(--yc-yellow); }
.yc-task-meta { font-size: 11px; color: var(--yc-text-faint); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.yc-task-kill { font-size: 11px; padding: 1px 8px; border-radius: 6px; background: transparent; border: 1px solid var(--yc-border); color: var(--yc-red); cursor: pointer; flex-shrink: 0; }
.yc-task-kill:hover { border-color: var(--yc-red); }
`;

const STATUS_CLASS: Record<JobView["status"], string> = {
	running: "yc-st-ok",
	completed: "yc-st-ok",
	failed: "yc-st-err",
	killed: "yc-st-err",
	stopping: "yc-st-warn",
};

export function TasksPanel({ useSessions, onKill, onRefresh }: TasksPanelProps) {
	const jobs: readonly JobView[] = useSessions
		? (useSessions((s: any) => (s?.current ? s.jobsBySession?.[s.current] : undefined)) ?? [])
		: [];
	return (
		<div className="yc-tasks">
			<style>{css}</style>
			<div className="yc-tasks-head">
				<span className="yc-tasks-title">后台运行任务</span>
				<button type="button" className="yc-tasks-refresh" onClick={() => onRefresh?.()}>
					↻ 刷新
				</button>
			</div>
			{jobs.length === 0 ? (
				<div className="yc-tasks-empty">暂无后台活跃任务</div>
			) : (
				jobs.map((job) => (
					<div key={job.id} className="yc-task-card">
						<div className="yc-task-row">
							<span className="yc-task-label">{job.label}</span>
							<span className={"yc-task-status " + (STATUS_CLASS[job.status] ?? "")}>
								{statusLabel(job.status)}
							</span>
						</div>
						<div className="yc-task-row">
							<span className="yc-task-meta">
								{job.detail ? `${job.kind} · ${job.detail}` : formatElapsed(job)}
							</span>
							{job.status === "running" && (
								<button type="button" className="yc-task-kill" onClick={() => onKill?.(job.id)}>
									终止
								</button>
							)}
						</div>
					</div>
				))
			)}
		</div>
	);
}

export default TasksPanel;

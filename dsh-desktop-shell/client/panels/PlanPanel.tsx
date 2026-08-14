/**
 * PlanPanel —— 计划面板（接 DSH 真实 todo 数据）。
 *
 * 数据接入方式（已确认）：
 * DSH 会话的 todo 列表经 session 作用域 slot 组件的标准注入 hook
 * `useProjection` 读取：`useProjection("todos") ?? []`。
 * useProjection 由框架注入到 slot 组件的 props（签名 (key:string)=>T|undefined），
 * 组件从 props 接收；缺省时容错为空数组（渲染空态）。
 *
 * TodoItem 类型（来自 @deepseek-ai/dsh-session/types）：
 *   { content: string; status: 'pending' | 'in_progress' | 'completed' }
 *
 * 视觉对齐 ycode RightPanel.vue 的 plan 区：顶部进度行 + 进度条，
 * 下方每项一行卡片，左侧状态图标（✓ / ⟳ / ○）。
 * CSS 变量一律 var(--yc-*)。
 */

/** DSH TodoItem（与 @deepseek-ai/dsh-session/types 对齐） */
export interface TodoItem {
	content: string;
	status: "pending" | "in_progress" | "completed";
}

/**
 * 计划进度：completed 数 / 总数 * 100，四舍五入；空数组返回 0。
 * 纯函数，便于单测。
 */
export function planProgress(todos: TodoItem[]): number {
	if (todos.length === 0) return 0;
	const done = todos.filter((t) => t.status === "completed").length;
	return Math.round((done / todos.length) * 100);
}

export interface PlanPanelProps {
	/** 框架注入的 session 作用域投影 hook；缺省时按空数组容错。 */
	useProjection?: (key: string) => any;
}

const css = `
.yc-plan { display: flex; flex-direction: column; gap: 10px; }
.yc-plan-progress-row { display: flex; justify-content: space-between; align-items: center; }
.yc-plan-progress-label { font-size: 12.5px; font-weight: 600; color: var(--yc-text); }
.yc-plan-progress-pct { font-size: 12.5px; font-weight: 700; color: var(--yc-accent); }
.yc-plan-bar { height: 6px; background: var(--yc-bg3); border-radius: 99px; overflow: hidden; }
.yc-plan-bar-fill { height: 100%; background: var(--yc-accent); border-radius: 99px; transition: width 0.25s ease; }
.yc-plan-empty { color: var(--yc-text-faint); font-size: 12px; padding: 12px 0; }
.yc-plan-list { display: flex; flex-direction: column; gap: 8px; }
.yc-plan-item { display: flex; align-items: flex-start; gap: 8px; background: var(--yc-bg2); border: 1px solid var(--yc-border); border-radius: 8px; padding: 8px 10px; font-size: 12.5px; line-height: 1.5; color: var(--yc-text); }
.yc-plan-item-icon { flex-shrink: 0; width: 16px; text-align: center; }
.yc-plan-item-content { flex: 1; min-width: 0; }
.yc-plan-item.yc-done { }
.yc-plan-item.yc-done .yc-plan-item-icon { color: var(--yc-green); }
.yc-plan-item.yc-done .yc-plan-item-content { text-decoration: line-through; color: var(--yc-text-faint); }
.yc-plan-item.yc-doing { border-color: var(--yc-accent); }
.yc-plan-item.yc-doing .yc-plan-item-icon { color: var(--yc-accent); }
.yc-plan-item.yc-doing .yc-plan-item-content { font-weight: 600; }
.yc-plan-item.yc-pending .yc-plan-item-icon { color: var(--yc-text-faint); }
`;

const STATUS_ICON: Record<TodoItem["status"], string> = {
	completed: "✓",
	in_progress: "⟳",
	pending: "○",
};

const STATUS_CLASS: Record<TodoItem["status"], string> = {
	completed: "yc-done",
	in_progress: "yc-doing",
	pending: "yc-pending",
};

const EMPTY_TEXT = "暂无活动计划。当星瑶编排复杂大任务时，计划全景会自动在此呈现。";

export function PlanPanel({ useProjection }: PlanPanelProps) {
	const todos: TodoItem[] = useProjection ? (useProjection("todos") ?? []) : [];
	const progress = planProgress(todos);
	return (
		<div className="yc-plan">
			<style>{css}</style>
			<div className="yc-plan-progress-row">
				<span className="yc-plan-progress-label">任务计划</span>
				<span className="yc-plan-progress-pct">{progress}%</span>
			</div>
			<div className="yc-plan-bar" role="progressbar" aria-valuenow={progress} aria-valuemin={0} aria-valuemax={100}>
				<div className="yc-plan-bar-fill" style={{ width: progress + "%" }} />
			</div>
			{todos.length === 0 ? (
				<div className="yc-plan-empty">{EMPTY_TEXT}</div>
			) : (
				<div className="yc-plan-list">
					{todos.map((todo, i) => (
						<div key={i} className={"yc-plan-item " + (STATUS_CLASS[todo.status] ?? "yc-pending")}>
							<span className="yc-plan-item-icon">{STATUS_ICON[todo.status] ?? "○"}</span>
							<span className="yc-plan-item-content">{todo.content}</span>
						</div>
					))}
				</div>
			)}
		</div>
	);
}

export default PlanPanel;

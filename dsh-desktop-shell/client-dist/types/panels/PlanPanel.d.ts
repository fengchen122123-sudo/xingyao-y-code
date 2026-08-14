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
export declare function planProgress(todos: TodoItem[]): number;
export interface PlanPanelProps {
    /** 框架注入的 session 作用域投影 hook；缺省时按空数组容错。 */
    useProjection?: (key: string) => any;
}
export declare function PlanPanel({ useProjection }: PlanPanelProps): import("react").JSX.Element;
export default PlanPanel;

/**
 * YcodeDetailsPanel —— ycode 风格右栏外壳（4 标签页容器）。
 *
 * 遮蔽 DSH 官方 details 栏（priority -10），内部用 ThemeScope 注入 ycode token。
 * 4 个标签：计划 / 任务 / 终端 / 文件，对齐 ycode RightPanel.vue 的视觉。
 *
 * 数据接线（本任务的"框架接线"）：
 * details slot 的 scope 为 'session'（dsh-client-ui-layout SlotMap 声明），
 * 故本组件自动获得框架标准注入：
 *   - GlobalStandardProps: useSessions / useWorkspaces
 *   - SessionStandardProps: useSession / sessionId / useProjection
 * 另有 client/index.tsx 的 inject 工厂提供 listDirectory / openPath
 *（来自 ctx.workspaces）。本组件把这些 hook 透传给四个面板；
 * 全部 props 保持可选（无参渲染时空态容错，兼容现有测试）。
 */

import { useState } from "react";
import { ThemeScope } from "../theme/ThemeScope";
import { PlanPanel, type TodoItem } from "../panels/PlanPanel";
import { TasksPanel } from "../panels/TasksPanel";
import { TermPanel } from "../panels/TermPanel";
import { FilesPanel, type DirectoryListing } from "../panels/FilesPanel";

export type DetailsTab = "plan" | "tasks" | "term" | "files";

const TABS: Array<{ id: DetailsTab; label: string }> = [
	{ id: "plan", label: "计划" },
	{ id: "tasks", label: "任务" },
	{ id: "term", label: "终端" },
	{ id: "files", label: "文件" },
];

const css = `
.yc-details { height: 100%; display: flex; flex-direction: column; background: var(--yc-bg1); border-left: 1px solid var(--yc-border); }
.yc-details-tabs { display: flex; border-bottom: 1px solid var(--yc-border); flex-shrink: 0; }
.yc-rtab { flex: 1; padding: 9px 0; text-align: center; font-size: 12.5px; color: var(--yc-text-dim); cursor: pointer; background: none; border: none; border-bottom: 2px solid transparent; transition: 0.15s; position: relative; font-family: var(--yc-sans); }
.yc-rtab:hover { color: var(--yc-text); }
.yc-rtab.yc-active { color: var(--yc-accent); border-bottom-color: var(--yc-accent); }
.yc-rtab .yc-badge { color: var(--yc-accent); font-size: 8px; position: absolute; top: 4px; right: 6px; }
.yc-details-body { flex: 1; overflow-y: auto; overflow-x: auto; padding: 12px; min-height: 0; }
`;

export interface YcodeDetailsPanelProps {
	/** 是否有计划项（控制计划标签的徽标点）；未传时由 useProjection("todos") 推导。 */
	hasPlan?: boolean;
	/** 框架注入的全局 slot hook（GlobalStandardProps）；缺省时子面板按空态容错。 */
	useSessions?: (sel: (s: any) => any) => any;
	/** 框架注入的全局 slot hook（GlobalStandardProps）；缺省时 FilesPanel 按空态容错。 */
	useWorkspaces?: (sel: (s: any) => any) => any;
	/** 框架注入的 session 作用域投影 hook（SessionStandardProps）；缺省时 PlanPanel/徽标按空态容错。 */
	useProjection?: (key: string) => any;
	/** 框架注入的会话 id（SessionStandardProps）；当前未直接使用，透传预留。 */
	sessionId?: string;
	/** 列目录（client/index.tsx 的 inject 工厂自 ctx.workspaces.listDirectory 提供）。 */
	listDirectory?: (path?: string) => Promise<DirectoryListing>;
	/** 在系统文件管理器中打开（inject 工厂自 ctx.workspaces.openPath 提供）。 */
	openPath?: (path: string) => void;
}

export function YcodeDetailsPanel(props: YcodeDetailsPanelProps) {
	const {
		hasPlan,
		useSessions,
		useWorkspaces,
		useProjection,
		listDirectory,
		openPath,
	} = props;
	const [activeTab, setActiveTab] = useState<DetailsTab>("plan");
	// hasPlan 显式传入时优先；否则尝试从 useProjection("todos") 推导（不可用时为 false）。
	const todos: TodoItem[] | undefined = useProjection
		? ((useProjection("todos") ?? undefined) as TodoItem[] | undefined)
		: undefined;
	const showPlanBadge = hasPlan ?? (todos !== undefined && todos.length > 0);
	return (
		<ThemeScope theme="dark">
			<div className="yc-details">
				<style>{css}</style>
				<div className="yc-details-tabs">
					{TABS.map((tab) => (
						<button
							key={tab.id}
							type="button"
							className={"yc-rtab" + (activeTab === tab.id ? " yc-active" : "")}
							aria-current={activeTab === tab.id ? "true" : undefined}
							onClick={() => setActiveTab(tab.id)}
						>
							{tab.label}
							{tab.id === "plan" && showPlanBadge ? <span className="yc-badge">●</span> : null}
						</button>
					))}
				</div>
				<div className="yc-details-body">
					{activeTab === "plan" && <PlanPanel useProjection={useProjection} />}
					{activeTab === "tasks" && <TasksPanel useSessions={useSessions} />}
					{activeTab === "term" && <TermPanel useSessions={useSessions} />}
					{activeTab === "files" && (
						<FilesPanel
							useWorkspaces={useWorkspaces}
							listDirectory={listDirectory}
							openPath={openPath}
						/>
					)}
				</div>
			</div>
		</ThemeScope>
	);
}

export default YcodeDetailsPanel;

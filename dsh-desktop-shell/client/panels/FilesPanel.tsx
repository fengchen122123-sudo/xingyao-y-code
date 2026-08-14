/**
 * FilesPanel —— 文件面板（目录浏览器，接 DSH workspace listDirectory）。
 *
 * 数据接入方式（已确认）：
 * - 工作区列表：全局 slot hook `useWorkspaces(s => s.items)` → WorkspaceView[]，
 *   每项含 path/title 等。由父/slot inject 注入 props；缺省时容错为空数组。
 * - 列目录：`ctx.workspaces.listDirectory(path?)` → DirectoryListing。
 *   slot 组件拿不到 ctx，故经 props 注入 `listDirectory(path?: string)`。
 * - 在系统打开：`ctx.workspaces.openPath(path)`，经 props 注入 `openPath?(path)`。
 *
 * 类型（@deepseek-ai/dsh-host-apiproxy 相关接口对齐）：
 *   DirectoryEntry   = { name: string; path: string; hidden: boolean }
 *   DirectoryListing = { path: string; home: string; crumbs: DirectoryEntry[];
 *                        entries: DirectoryEntry[]; truncated: boolean }
 *
 * 注意：DSH listDirectory 的 entries 只含【子目录】（不返回文件），故本面板
 * 为"目录浏览器"：面包屑 crumbs + 子目录列表，点击进入 / 回跳。文件内容预览
 * DSH 无公开接口，不做。
 *
 * hidden 隐藏目录处理策略：默认不显示（与 ycode RightPanel.vue 一致，
 * 避免 .git / node_modules 等噪音干扰）。
 *
 * 视觉对齐 ycode RightPanel.vue 的 files 区：顶部标题行（左"项目文件"，右刷新
 * chip）+ 面包屑 + 目录列表。CSS 变量一律 var(--yc-*)。
 */

import { useCallback, useEffect, useState } from "react";

/** DSH DirectoryEntry（与 host listDirectory 返回结构对齐） */
export interface DirectoryEntry {
	name: string;
	path: string;
	hidden: boolean;
}

/** DSH DirectoryListing（与 host listDirectory 返回结构对齐） */
export interface DirectoryListing {
	path: string;
	home: string;
	crumbs: DirectoryEntry[];
	entries: DirectoryEntry[];
	truncated: boolean;
}

export interface FilesPanelProps {
	/** 框架注入的全局 slot hook；缺省时按空数组容错。 */
	useWorkspaces?: (sel: (s: any) => any) => any;
	/** 列目录（父/slot inject 自 ctx.workspaces.listDirectory）。 */
	listDirectory?: (path?: string) => Promise<DirectoryListing>;
	/** 在系统文件管理器中打开（父/slot inject 自 ctx.workspaces.openPath）。 */
	openPath?: (path: string) => void;
}

const css = `
.yc-files { display: flex; flex-direction: column; gap: 8px; }
.yc-files-head { display: flex; justify-content: space-between; align-items: center; }
.yc-files-title { font-size: 12px; font-weight: 600; color: var(--yc-text-dim); }
.yc-files-refresh { font-size: 11px; padding: 2px 8px; border-radius: 99px; background: var(--yc-bg2); border: 1px solid var(--yc-border); color: var(--yc-text-dim); cursor: pointer; }
.yc-files-refresh:hover { border-color: var(--yc-accent); color: var(--yc-text); }
.yc-files-crumbs { display: flex; flex-wrap: wrap; align-items: center; gap: 2px; font-size: 12px; color: var(--yc-text-dim); }
.yc-files-crumb { background: none; border: none; padding: 1px 2px; font-size: 12px; color: var(--yc-text-dim); cursor: pointer; border-radius: 4px; }
.yc-files-crumb:hover { color: var(--yc-text); background: var(--yc-bg2); }
.yc-files-crumb.yc-crumb-current { color: var(--yc-text); font-weight: 600; }
.yc-files-sep { color: var(--yc-text-faint); }
.yc-files-list { display: flex; flex-direction: column; gap: 2px; }
.yc-files-row { display: flex; align-items: center; gap: 6px; padding: 6px 8px; border-radius: 6px; font-size: 12.5px; color: var(--yc-text); cursor: pointer; background: none; border: none; width: 100%; text-align: left; }
.yc-files-row:hover { background: var(--yc-bg2); }
.yc-files-name { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.yc-files-open { font-size: 11px; padding: 0 6px; border-radius: 4px; background: transparent; border: 1px solid transparent; color: var(--yc-text-faint); cursor: pointer; flex-shrink: 0; }
.yc-files-row:hover .yc-files-open { border-color: var(--yc-border); color: var(--yc-text-dim); }
.yc-files-open:hover { border-color: var(--yc-accent) !important; color: var(--yc-text) !important; }
.yc-files-empty { color: var(--yc-text-faint); font-size: 12px; padding: 12px 0; }
.yc-files-truncated { font-size: 11px; color: var(--yc-yellow); padding: 4px 0 0; }
`;

export function FilesPanel({ useWorkspaces, listDirectory, openPath }: FilesPanelProps) {
	const workspaces: readonly any[] = useWorkspaces
		? (useWorkspaces((s: any) => s?.items) ?? [])
		: [];
	// 当前浏览目录；空串 = 未选择（初始时若有工作区则取第一个的 path 作为根）。
	const [currentPath, setCurrentPath] = useState<string>("");
	const [listing, setListing] = useState<DirectoryListing | null>(null);
	const [loading, setLoading] = useState(false);
	const [error, setError] = useState(false);

	// 初始：工作区就绪且尚未选择目录时，以第一个工作区的 path 作为根。
	useEffect(() => {
		if (!currentPath && workspaces.length > 0 && workspaces[0]?.path) {
			setCurrentPath(workspaces[0].path);
		}
	}, [currentPath, workspaces]);

	const load = useCallback(
		(path: string) => {
			if (!listDirectory || !path) return;
			setLoading(true);
			setError(false);
			listDirectory(path).then(
				(res) => {
					setListing(res ?? null);
					setLoading(false);
				},
				() => {
					setError(true);
					setLoading(false);
				},
			);
		},
		[listDirectory],
	);

	useEffect(() => {
		if (currentPath) load(currentPath);
	}, [currentPath, load]);

	const crumbs = listing?.crumbs ?? [];
	// 隐藏目录默认不显示（与 ycode 一致）。
	const entries = (listing?.entries ?? []).filter((e) => !e.hidden);
	const hasWorkspace = workspaces.length > 0;

	return (
		<div className="yc-files">
			<style>{css}</style>
			<div className="yc-files-head">
				<span className="yc-files-title">项目文件</span>
				<button
					type="button"
					className="yc-files-refresh"
					onClick={() => load(currentPath)}
				>
					↻ 刷新
				</button>
			</div>

			{crumbs.length > 0 && (
				<div className="yc-files-crumbs">
					{crumbs.map((c, i) => (
						<span key={c.path || c.name || i}>
							{i > 0 && <span className="yc-files-sep">/</span>}
							<button
								type="button"
								className={
									"yc-files-crumb" + (i === crumbs.length - 1 ? " yc-crumb-current" : "")
								}
								onClick={() => setCurrentPath(c.path)}
							>
								{c.name}
							</button>
						</span>
					))}
				</div>
			)}

			{loading ? (
				<div className="yc-files-empty">加载中…</div>
			) : error ? (
				<div className="yc-files-empty">目录读取失败</div>
			) : listing ? (
				<div className="yc-files-list">
					{entries.length === 0 ? (
						<div className="yc-files-empty">该目录下没有子目录</div>
					) : (
						entries.map((e) => (
							<button
								key={e.path}
								type="button"
								className="yc-files-row"
								onClick={() => setCurrentPath(e.path)}
							>
								<span>📁</span>
								<span className="yc-files-name">{e.name}</span>
								{openPath && (
									<span
										className="yc-files-open"
										role="button"
										title="在系统中打开"
										onClick={(ev) => {
											ev.stopPropagation();
											openPath(e.path);
										}}
									>
										🗂
									</span>
								)}
							</button>
						))
					)}
					{listing.truncated && (
						<div className="yc-files-truncated">⚠ 结果过多，已截断显示</div>
					)}
				</div>
			) : !hasWorkspace ? (
				<div className="yc-files-empty">当前为通用自由对话，暂未引入项目文件夹</div>
			) : (
				<div className="yc-files-empty">加载中…</div>
			)}
		</div>
	);
}

export default FilesPanel;

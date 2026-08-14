/**
 * YcodeSidebar —— ycode 风格侧边栏外壳（遮蔽 DSH 官方 sidebar）。
 *
 * 遮蔽策略（priority -10）：
 * 官方 sidebar 由 dsh-client-ui-sidebar 注册（priority 0），我们用 -10 胜出。
 * 官方 sidebar 声明了子槽 sidebar.workspaces / sidebar.settings /
 * sidebar.footer.action（declaring is claiming）。
 * 我们遮蔽后须接管这些声明，但会话浏览是复杂功能——
 * 我们保留委托：重新声明同名子槽，由官方 ui-workspace / ui-settings
 * 继续注册进这些槽（注册不变，只是声明者换了），然后我们在对应位置
 * renderSlot("sidebar.workspaces") 委托渲染。
 *
 * 这样 ycode 外壳（logo / 新会话 / 菜单 / 底部设置行）是我们的，
 * 会话列表内容是 DSH 原生的——功能不丢、样式是 ycode 的。
 *
 * 视觉对齐 ycode Sidebar.vue：240px 宽、logo 行、菜单项、会话分组、
 * 底部工作区/设置行；窄态 56px 图标栏（对齐 AppFrame SIDEBAR_COLLAPSED）。
 *
 * 数据接入：
 * sidebar slot 的 scope 为 'root'（GlobalStandardProps 自带
 * useSessions / useWorkspaces）；inject 工厂提供 startSession /
 * toggleSidebar（与官方 sidebar 一致，来自 ctx.workspaces / ctx.layout）。
 */

import { useState, type ReactNode } from "react";
import { ThemeScope } from "../theme/ThemeScope";
import { ICON_DATA_URI } from "../assets/icon";

export interface YcodeSidebarProps {
	/** 宽/窄态（官方传 wide: boolean；窄=56px 图标栏）。 */
	wide?: boolean;
	/** 宽态切换回调（rail 图标点击展开）。 */
	expandSidebar?: () => void;
	/** 框架注入的子槽渲染（官方 sidebar 声明的子槽，我们委托）。 */
	renderSlot?: (key: string, owner?: object, opts?: object) => ReactNode;
	/** inject 工厂：新建会话。 */
	startSession?: (workspaceId?: string) => void;
	/** inject 工厂：折叠/展开侧栏。 */
	toggleSidebar?: () => void;
	/** 框架注入的全局 hook（会话列表）。 */
	useSessions?: (sel: (s: any) => any) => any;
	/** 框架注入的全局 hook（工作区列表）。 */
	useWorkspaces?: (sel: (s: any) => any) => any;
}

const css = `
.yc-sidebar { height: 100%; display: flex; flex-direction: column; background: var(--yc-bg1); color: var(--yc-text); font-family: var(--yc-sans); border-right: 1px solid var(--yc-border); overflow: hidden; }
.yc-sidebar.yc-narrow { width: 56px; }
.yc-sidebar.yc-wide { width: 100%; }
.yc-sb-logo { display: flex; align-items: center; gap: 9px; padding: 12px 12px 8px; flex-shrink: 0; }
.yc-sb-logo img { width: 28px; height: 28px; border-radius: 7px; object-fit: cover; display: block; }
.yc-sb-logo .yc-sb-name { font-weight: 600; font-size: 14px; letter-spacing: 0.5px; color: var(--yc-text); }
.yc-sb-narrow-icons { display: flex; flex-direction: column; align-items: center; gap: 12px; padding: 12px 0; flex: 1; }
.yc-sb-narrow-ico { width: 32px; height: 32px; border-radius: 8px; border: none; background: transparent; color: var(--yc-text-dim); font-size: 15px; display: flex; align-items: center; justify-content: center; cursor: pointer; transition: 0.15s; }
.yc-sb-narrow-ico:hover { background: var(--yc-bg2); color: var(--yc-text); }
.yc-sb-menu { padding: 8px 8px 2px; border-bottom: 1px solid var(--yc-border); flex-shrink: 0; }
.yc-sb-menu-item { display: flex; align-items: center; gap: 9px; padding: 8px 10px; border-radius: 8px; cursor: pointer; color: var(--yc-text-dim); margin-bottom: 3px; transition: 0.15s; background: none; border: none; width: 100%; text-align: left; font-family: var(--yc-sans); }
.yc-sb-menu-item:hover { background: var(--yc-bg2); color: var(--yc-text); }
.yc-sb-mi-ico { font-size: 14px; width: 18px; text-align: center; flex-shrink: 0; }
.yc-sb-mi-label { font-size: 13px; font-weight: 500; }
.yc-sb-mi-count { margin-left: auto; font-size: 11px; color: var(--yc-text-faint); background: var(--yc-bg3); border-radius: 8px; padding: 1px 7px; }
.yc-sb-mi-arrow { color: var(--yc-text-faint); font-size: 14px; }
.yc-sb-section { padding: 10px 10px 4px; color: var(--yc-text-faint); font-size: 11.5px; letter-spacing: 1px; text-transform: uppercase; display: flex; justify-content: space-between; align-items: center; flex-shrink: 0; }
.yc-sb-section-add { background: none; border: none; color: var(--yc-text-faint); font-size: 13px; padding: 1px 5px; border-radius: 4px; transition: 0.15s; cursor: pointer; }
.yc-sb-section-add:hover { color: var(--yc-accent); background: var(--yc-bg3); }
.yc-sb-scroll { flex: 1; overflow-y: auto; padding: 0 6px; }
.yc-sb-footer { border-top: 1px solid var(--yc-border); padding: 6px; flex-shrink: 0; }
.yc-sb-footer-btn { display: flex; align-items: center; gap: 8px; width: 100%; padding: 8px 10px; border-radius: 8px; border: none; background: none; color: var(--yc-text-dim); cursor: pointer; transition: 0.15s; text-align: left; font-family: var(--yc-sans); }
.yc-sb-footer-btn:hover { background: var(--yc-bg2); color: var(--yc-text); }
.yc-sb-footer-btn .yc-sb-fc { display: flex; flex-direction: column; }
.yc-sb-footer-btn .yc-sb-fc b { font-size: 13px; color: var(--yc-text); }
.yc-sb-footer-btn .yc-sb-fc small { font-size: 10.5px; color: var(--yc-text-faint); }
.yc-sb-footer-btn > span:first-child { font-size: 15px; width: 18px; text-align: center; }
`;

/** 菜单项配置（技能/MCP/插件在 DSH 无直接对应，先占位） */
const MENU_ITEMS = [
	{ id: "skills", icon: "❖", label: "技能 Skills" },
	{ id: "mcp", icon: "⧉", label: "MCP 服务" },
	{ id: "plugins", icon: "⬡", label: "插件" },
];

export function YcodeSidebar(props: YcodeSidebarProps) {
	const { wide = true, expandSidebar, renderSlot, startSession, useSessions, useWorkspaces } = props;
	const [collapsedGroups, setCollapsedGroups] = useState<Record<string, boolean>>({});

	// 会话列表（来自 DSH useSessions），用于分组展示
	const sessions: readonly any[] = useSessions
		? (useSessions((s: any) => s?.items ?? s?.list ?? []) as readonly any[])
		: [];

	// 工作区列表（用于底部标签）
	const workspaces: readonly any[] = useWorkspaces
		? (useWorkspaces((s: any) => s?.items ?? []) as readonly any[])
		: [];
	const currentWsLabel = workspaces.length > 0 ? (workspaces[0]?.title ?? "项目") : "对话";

	if (!wide) {
		// 窄态：56px 图标栏
		return (
			<ThemeScope theme="dark">
				<div className="yc-sidebar yc-narrow">
					<style>{css}</style>
					<div className="yc-sb-narrow-icons">
						<button type="button" className="yc-sb-narrow-ico" title="新会话" onClick={() => startSession?.()}>
							✎
						</button>
						<button type="button" className="yc-sb-narrow-ico" title="技能" onClick={() => {}}>
							❖
						</button>
						<button type="button" className="yc-sb-narrow-ico" title="设置" onClick={() => {}}>
							⚙
						</button>
						<button
							type="button"
							className="yc-sb-narrow-ico"
							title="展开侧栏"
							onClick={() => expandSidebar?.()}
						>
							☰
						</button>
					</div>
				</div>
			</ThemeScope>
		);
	}

	return (
		<ThemeScope theme="dark">
			<div className="yc-sidebar yc-wide">
				<style>{css}</style>
				{/* logo 行 */}
				<div className="yc-sb-logo">
					<img src={ICON_DATA_URI} alt="Y-CODE" />
					<span className="yc-sb-name">Y-CODE</span>
				</div>

				{/* 菜单 */}
				<div className="yc-sb-menu">
					<button
						type="button"
						className="yc-sb-menu-item"
						onClick={() => startSession?.()}
					>
						<span className="yc-sb-mi-ico">✎</span>
						<span className="yc-sb-mi-label">新会话</span>
						<span className="yc-sb-mi-count">{currentWsLabel}</span>
						<span className="yc-sb-mi-arrow">＋</span>
					</button>
					{MENU_ITEMS.map((item) => (
						<button
							key={item.id}
							type="button"
							className="yc-sb-menu-item"
							onClick={() => { /* 占位，DSH 无直接对应 */ }}
						>
							<span className="yc-sb-mi-ico">{item.icon}</span>
							<span className="yc-sb-mi-label">{item.label}</span>
							<span className="yc-sb-mi-arrow">›</span>
						</button>
					))}
				</div>

				{/* 会话区标题 */}
				<div className="yc-sb-section">
					<span>项目会话</span>
					<button
						type="button"
						className="yc-sb-section-add"
						title="引入项目文件夹"
						onClick={() => startSession?.()}
					>
						▣
					</button>
				</div>

				{/* 会话列表：委托 DSH 原生 sidebar.workspaces 子槽渲染 */}
				<div className="yc-sb-scroll">
					{renderSlot ? (
						renderSlot("sidebar.workspaces", { wide, expandSidebar })
					) : (
						<div style={{ padding: "10px", color: "var(--yc-text-faint)", fontSize: 12 }}>
							暂无历史会话
						</div>
					)}
				</div>

				{/* 底部 */}
				<div className="yc-sb-footer">
					<button type="button" className="yc-sb-footer-btn" onClick={() => startSession?.()}>
						<span>▣</span>
						<span className="yc-sb-fc">
							<b>{currentWsLabel}</b>
							<small>DSH 已连接</small>
						</span>
					</button>
					<button type="button" className="yc-sb-footer-btn" onClick={() => {}}>
						<span>⚙</span>
						<span className="yc-sb-fc">
							<b>设置</b>
							<small>模型配置</small>
						</span>
					</button>
				</div>
			</div>
		</ThemeScope>
	);
}

export default YcodeSidebar;

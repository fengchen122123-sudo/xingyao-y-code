/**
 * YcodeTopbar —— ycode 风格顶栏（通过 shell.overlay slot 渲染）。
 *
 * DSH 无独立"顶栏"slot（已探测确认）：AppFrame 的三栏 sidebar/conversation/
 * details 内没有 header 槽。顶栏功能分散在 sidebar.logoRow 与
 * conversation.session.header 里。
 *
 * 本组件注册进 shell.overlay slot（AppFrame 在 overlayLayer 渲染它，
 * data-shell-overlay），作为 fixed 48px 顶栏覆盖在内容上方。
 * 顶栏内容对齐 ycode Topbar.vue：☰ 折叠侧栏 / logo / 弹性占位 /
 * ☾ 主题切换 / ▐ 切换右栏。
 *
 * 主题切换：通过 inject 工厂拿到 settings 服务，写 ui-theme.preference
 *（dark / light）。DSH 侧 theme-ycode.css 套壳响应 theme 切换。
 *
 * 侧栏/右栏切换：通过 inject 工厂拿到 ctx.layout 的
 * toggleSidebar() / openDetails() / closeDetails()。
 */

import { useState, type ReactNode } from "react";
import { ThemeScope } from "../theme/ThemeScope";
import { ICON_DATA_URI } from "../assets/icon";

export interface YcodeTopbarProps {
	/** inject 工厂：折叠/展开侧栏。 */
	toggleSidebar?: () => void;
	/** inject 工厂：打开右栏。 */
	openDetails?: () => void;
	/** inject 工厂：关闭右栏。 */
	closeDetails?: () => void;
	/** inject 工厂：切换主题（写 ui-theme.preference）。 */
	onToggleTheme?: (next: "dark" | "light") => void;
	/** 当前主题（初始 dark）。 */
	theme?: "dark" | "light";
}

const css = `
.yc-topbar { position: fixed; top: 0; left: 0; right: 0; height: 48px; z-index: 9000; background: var(--yc-bg1); border-bottom: 1px solid var(--yc-border); display: flex; align-items: center; padding: 0 14px; gap: 12px; flex-shrink: 0; font-family: var(--yc-sans); box-shadow: 0 1px 3px rgba(0,0,0,0.15); }
.yc-tb-iconbtn { background: transparent; border: 1px solid transparent; color: var(--yc-text-dim); border-radius: 7px; width: 32px; height: 32px; font-size: 15px; display: flex; align-items: center; justify-content: center; cursor: pointer; transition: 0.15s; }
.yc-tb-iconbtn:hover { background: var(--yc-bg2); color: var(--yc-text); }
.yc-tb-logo { display: flex; align-items: center; gap: 9px; user-select: none; }
.yc-tb-logo img { width: 28px; height: 28px; border-radius: 7px; object-fit: cover; display: block; }
.yc-tb-logo .yc-tb-name { font-weight: 600; font-size: 14px; letter-spacing: 0.5px; color: var(--yc-text); }
.yc-tb-spacer { flex: 1; }
`;

/**
 * AppFrame 让位：顶栏是 fixed 48px，会把 AppFrame grid 内容顶部盖住 48px。
 * 这里注入一条全局 CSS，给 AppFrame 根节点（grid 容器，class 含 "frame"）
 * 加 padding-top: 48px，使三栏内容整体下移、不被顶栏遮挡。
 * 选择器用属性匹配（[class*="frame"][style*="grid-template-rows"]）避开
 * CSS Module 的构建期 hash 前缀（pI_x6G_ 等会随 DSH 版本变化）。
 * box-sizing: border-box 保证 padding 不撑破高度。
 */
const OFFSET_CSS = `
[data-shell-overlay] { }
[class*="frame"][style*="grid-template-rows"],
[class*="frame"]:has(> [class*="overlayLayer"]) { padding-top: 48px !important; box-sizing: border-box !important; }
`;
const OFFSET_STYLE_ID = "yc-topbar-offset";

function ensureOffsetInjected() {
	if (typeof document === "undefined") return;
	if (document.getElementById(OFFSET_STYLE_ID)) return;
	const style = document.createElement("style");
	style.id = OFFSET_STYLE_ID;
	style.textContent = OFFSET_CSS;
	document.head.appendChild(style);
}

export function YcodeTopbar(props: YcodeTopbarProps) {
	const { toggleSidebar, openDetails, closeDetails, onToggleTheme } = props;
	const [theme, setTheme] = useState<"dark" | "light">(props.theme ?? "dark");
	const [rightOpen, setRightOpen] = useState(true);
	ensureOffsetInjected();

	const handleTheme = () => {
		const next = theme === "dark" ? "light" : "dark";
		setTheme(next);
		onToggleTheme?.(next);
	};

	const handleRight = () => {
		if (rightOpen) {
			closeDetails?.();
			setRightOpen(false);
		} else {
			openDetails?.();
			setRightOpen(true);
		}
	};

	return (
		<ThemeScope theme={theme}>
			<div className="yc-topbar" data-shell-overlay-replace="true">
				<style>{css}</style>
				<button
					type="button"
					className="yc-tb-iconbtn"
					title="切换侧栏"
					onClick={() => toggleSidebar?.()}
				>
					☰
				</button>
				<div className="yc-tb-logo">
					<img src={ICON_DATA_URI} alt="Y-CODE" />
					<span className="yc-tb-name">Y-CODE</span>
				</div>
				<div className="yc-tb-spacer" />
				<button
					type="button"
					className="yc-tb-iconbtn"
					title="切换主题"
					onClick={handleTheme}
				>
					{theme === "dark" ? "☾" : "☀"}
				</button>
				<button
					type="button"
					className="yc-tb-iconbtn"
					title="切换右栏"
					onClick={handleRight}
				>
					▐
				</button>
			</div>
		</ThemeScope>
	);
}

export default YcodeTopbar;

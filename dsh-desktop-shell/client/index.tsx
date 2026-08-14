/**
 * dsh-desktop-shell 的 client 侧入口。
 *
 * 用 priority: -10 注册 "details" slot 的 YcodeDetailsPanel（ycode 右栏外壳，
 * 4 标签页容器），遮蔽官方 dsh-client-ui-conversation 注册的 DetailsPanel
 *（官方注册均为默认 priority 0，同一 cell 按 priority 升序，最低者胜出）。
 *
 * 产物格式要求（见 tsdown.config.ts 的 banner/footer 说明）：
 * 本入口经 tsdown 打包成 CJS body，再由外层包裹
 *   window.__ModuleLoader__.load({ id: "dsh-desktop-shell", factory: (require) => { ... } })
 * react / react/jsx-runtime / @deepseek-ai/* 均为 external，由宿主模块加载器
 * 通过 factory 的 require 提供。
 */

import { YcodeDetailsPanel } from "./shell/YcodeDetailsPanel";
import { YcodeSidebar } from "./shell/YcodeSidebar";
import { YcodeTopbar } from "./shell/YcodeTopbar";

/**
 * client 插件入口签名：export function apply(ctx)
 * 参考官方写法：ctx.effect(() => ctx.slots.register({ name, ... }, Component), "...")
 *
 * 注册三个遮蔽组件（均 priority -10，胜出官方 priority 0）：
 *
 * 1. "details" slot → YcodeDetailsPanel（右栏 4 标签页）
 *    session 作用域：框架自动注入 useSession/sessionId/useProjection
 *    + useSessions/useWorkspaces；inject 工厂补 listDirectory/openPath
 *    （来自 ctx.workspaces，供 FilesPanel 用）。
 *
 * 2. "sidebar" slot → YcodeSidebar（侧边栏外壳）
 *    root 作用域：框架自动注入 useSessions/useWorkspaces；
 *    需重新声明官方占用的子槽 sidebar.workspaces/settings/footer.action
 *    （declaring is claiming —— 遮蔽后必须接管声明），但委托 renderSlot
 *    给官方 ui-workspace / ui-settings 继续渲染（功能不丢）；
 *    inject 工厂补 startSession/toggleSidebar（与官方 sidebar 一致）。
 *
 * 3. "shell.overlay" slot → YcodeTopbar（顶栏 overlay）
 *    root 作用域；inject 工厂补 toggleSidebar/openDetails/closeDetails
 *    （来自 ctx.layout）+ 主题切换（写 ui-theme.preference via ctx.settings）。
 */
export function apply(ctx: any) {
	// --- 1. 右栏 details ---
	ctx.effect(
		() =>
			ctx.slots.register(
				{
					name: "details",
					priority: -10,
					inject: () => ({
						listDirectory: (path?: string) => ctx.workspaces.listDirectory(path),
						openPath: (path: string) => {
							void ctx.workspaces.openPath(path);
						},
					}),
				},
				YcodeDetailsPanel,
			),
		"dsh-desktop-shell: details",
	);

	// --- 2. 侧边栏 sidebar（遮蔽官方，重新声明子槽，委托渲染）---
	ctx.effect(
		() =>
			ctx.slots.register(
				{
					name: "sidebar",
					priority: -10,
					// 重新声明官方占用的子槽（declaring is claiming）
					children: {
						"sidebar.workspaces": { kind: "single", scope: "root" },
						"sidebar.settings": { kind: "single", scope: "root" },
						"sidebar.footer.action": { kind: "list", scope: "root" },
					},
					inject: () => ({
						startSession: (workspaceId?: string) => {
							ctx.workspaces.startSession(workspaceId);
						},
						toggleSidebar: () => {
							ctx.layout.toggleSidebar();
						},
					}),
				},
				YcodeSidebar,
			),
		"dsh-desktop-shell: sidebar",
	);

	// --- 3. 顶栏 shell.overlay ---
	ctx.effect(
		() =>
			ctx.slots.register(
				{
					name: "shell.overlay",
					priority: -10,
					inject: () => ({
						toggleSidebar: () => {
							ctx.layout.toggleSidebar();
						},
						openDetails: () => {
							ctx.layout.openDetails();
						},
						closeDetails: () => {
							ctx.layout.closeDetails();
						},
					onToggleTheme: (next: "dark" | "light") => {
						try {
							// 正道：dsh-client-ui-theme 的 ThemeRuntime（ctx.provide("theme", ...)），
							// setTheme(id) 写 settings scope 并发 theme/change 事件。
							const theme = ctx.get?.("theme");
							if (theme && typeof theme.setTheme === "function") {
								theme.setTheme(next);
							} else {
								// 兜底：直接写 settings scope（client 侧 settingsScope.bind）
								const scope = ctx.settingsScope?.bind?.({ namespace: "ui-theme" });
								void scope?.update?.({ preference: next });
							}
						} catch {
							/* 主题服务不可用时静默容错 */
						}
					},					}),
				},
				YcodeTopbar,
			),
		"dsh-desktop-shell: topbar",
	);
}

/** client 插件依赖注入声明 */
export const inject = [
	"@deepseek-ai/dsh-client-runtime",
	"@deepseek-ai/dsh-client-ui-layout",
	"@deepseek-ai/dsh-client-locale",
	// 主题切换依赖（ctx.get("theme") → ThemeRuntime.setTheme）
	"@deepseek-ai/dsh-client-ui-theme",
];

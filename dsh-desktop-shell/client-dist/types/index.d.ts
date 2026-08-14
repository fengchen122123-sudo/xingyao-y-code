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
export declare function apply(ctx: any): void;
/** client 插件依赖注入声明 */
export declare const inject: string[];

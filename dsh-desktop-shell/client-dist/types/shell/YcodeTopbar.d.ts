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
export declare function YcodeTopbar(props: YcodeTopbarProps): import("react").JSX.Element;
export default YcodeTopbar;

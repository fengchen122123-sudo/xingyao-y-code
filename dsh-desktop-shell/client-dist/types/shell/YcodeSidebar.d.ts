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
import { type ReactNode } from "react";
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
export declare function YcodeSidebar(props: YcodeSidebarProps): import("react").JSX.Element;
export default YcodeSidebar;

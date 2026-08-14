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
import { type DirectoryListing } from "../panels/FilesPanel";
export type DetailsTab = "plan" | "tasks" | "term" | "files";
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
export declare function YcodeDetailsPanel(props: YcodeDetailsPanelProps): import("react").JSX.Element;
export default YcodeDetailsPanel;

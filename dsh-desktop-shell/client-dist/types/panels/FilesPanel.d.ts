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
export declare function FilesPanel({ useWorkspaces, listDirectory, openPath }: FilesPanelProps): import("react").JSX.Element;
export default FilesPanel;

/**
 * TermPanel —— 终端面板（只读展示当前会话的终端类 job 活动）。
 *
 * 重要前提：DSH 未暴露 client 可直接"执行一条 shell 命令"的公开接口
 * （dsh-tool-bash/pwsh/terminal/jobs 均无 dsh.client 入口）。因此本面板
 * 采用降级方案：
 * - 视觉与交互复刻 ycode 终端框（深色终端、$ 提示符、按行着色）。
 * - 数据复用全局 slot 标准 hook `useSessions` 读当前会话的 JobView[]，
 *   把 kind 为 bash/pwsh/pty-send 的 job 作为"终端活动"只读展示
 *   （label=命令、status/detail 如 exit code）。
 * - 输入行保留 UI，但因无执行接口，输入框 disabled，
 *   placeholder 提示"终端执行由对话内工具完成"（语义诚实，不假装能执行）。
 *
 * 数据接入（与 TasksPanel 相同）：
 *   useSessions(s => (s.current ? s.jobsBySession[s.current] : undefined) ?? [])
 * JobView: { id, kind, label, status, detail?, startedAt, finishedAt? }
 *
 * 视觉对齐 ycode RightPanel.vue 的 term 区；CSS 变量一律 var(--yc-*)。
 */
export interface TermPanelProps {
    /** 框架注入的全局 slot hook；缺省时按空数组容错。 */
    useSessions?: (sel: (s: any) => any) => any;
}
/**
 * 行着色分类，对齐 ycode 终端着色规则。纯函数，便于单测。
 * - '$ ' 开头 → 't-cmd'（命令行）
 * - '[进程已退出，代码 0]' → 't-ok'（正常退出）
 * - '[进程已退出'/'[启动失败'/'…[输出过长' → 't-warn'（异常/截断）
 * - 其它 → ''（默认）
 */
export declare function classifyLine(text: string): "" | "t-cmd" | "t-ok" | "t-warn";
/**
 * 从 detail 文本中提取退出码（形如 "exit code 3" / "退出码 3" / "code=3"）。
 * 找不到时返回 undefined。纯函数。
 */
export declare function extractExitCode(detail?: string): number | undefined;
/**
 * 把一个终端 job 映射成若干显示行：
 * 第一行恒为 `$ <命令>`，第二行为状态行（借 ycode 着色规则显示）。
 * 纯函数，便于单测。
 */
export declare function jobToLines(job: {
    kind: string;
    label: string;
    status: string;
    detail?: string;
}): string[];
export declare function TermPanel({ useSessions }: TermPanelProps): import("react").JSX.Element;
export default TermPanel;

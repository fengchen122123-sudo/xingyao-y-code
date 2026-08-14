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

/** 终端类 job 的 kind 集合（对齐 ycode 终端工具） */
const TERM_KINDS = ["bash", "pwsh", "pty-send"];

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
export function classifyLine(text: string): "" | "t-cmd" | "t-ok" | "t-warn" {
	if (text.startsWith("$ ")) return "t-cmd";
	if (text.startsWith("[进程已退出，代码 0]")) return "t-ok";
	if (
		text.startsWith("[进程已退出") ||
		text.startsWith("[启动失败") ||
		text.startsWith("…[输出过长")
	)
		return "t-warn";
	return "";
}

/**
 * 从 detail 文本中提取退出码（形如 "exit code 3" / "退出码 3" / "code=3"）。
 * 找不到时返回 undefined。纯函数。
 */
export function extractExitCode(detail?: string): number | undefined {
	if (!detail) return undefined;
	const m = detail.match(/(?:exit\s*code|退出码|code)[=:：\s]*(-?\d+)/i);
	return m ? Number(m[1]) : undefined;
}

/**
 * 把一个终端 job 映射成若干显示行：
 * 第一行恒为 `$ <命令>`，第二行为状态行（借 ycode 着色规则显示）。
 * 纯函数，便于单测。
 */
export function jobToLines(job: {
	kind: string;
	label: string;
	status: string;
	detail?: string;
}): string[] {
	const lines: string[] = [`$ ${job.label}`];
	const code = extractExitCode(job.detail);
	switch (job.status) {
		case "running":
		case "stopping":
			lines.push("[运行中…]");
			break;
		case "completed":
			lines.push(`[进程已退出，代码 ${code ?? 0}]`);
			break;
		case "failed":
		case "killed":
		default:
			lines.push(`[进程已退出，代码 ${code ?? 1}]`);
			break;
	}
	return lines;
}

const css = `
.yc-term { display: flex; flex-direction: column; gap: 8px; }
.yc-term-title { font-size: 12px; font-weight: 600; color: var(--yc-text-dim); }
.yc-term-body {
	background: var(--yc-code-bg);
	border: 1px solid var(--yc-border);
	border-radius: 9px;
	padding: 12px;
	font-family: var(--yc-mono);
	font-size: 12px;
	line-height: 1.65;
	display: flex;
	flex-direction: column;
	gap: 8px;
}
.yc-term-out { display: flex; flex-direction: column; white-space: pre-wrap; word-break: break-all; }
.yc-term-line { color: var(--yc-text-dim); }
.yc-term-line.t-cmd { color: var(--yc-text-bright); }
.yc-term-line.t-ok { color: var(--yc-green); }
.yc-term-line.t-warn { color: var(--yc-yellow); }
.yc-term-input-row { display: flex; align-items: center; gap: 6px; }
.yc-term-prompt { color: var(--yc-green); font-weight: 600; }
.yc-term-input {
	flex: 1;
	background: transparent;
	border: none;
	outline: none;
	color: var(--yc-text-bright);
	font-family: var(--yc-mono);
	font-size: 12px;
}
.yc-term-input::placeholder { color: var(--yc-text-faint); }
.yc-term-input:disabled { cursor: not-allowed; opacity: 0.7; }
`;

export function TermPanel({ useSessions }: TermPanelProps) {
	const jobs: readonly { id: string; kind: string; label: string; status: string; detail?: string }[] =
		useSessions
			? (useSessions((s: any) => (s?.current ? s.jobsBySession?.[s.current] : undefined)) ?? [])
			: [];
	const termJobs = jobs.filter((j) => TERM_KINDS.includes(j.kind));
	const lines: { key: string; text: string }[] = termJobs.flatMap((j) =>
		jobToLines(j).map((text, i) => ({ key: `${j.id}:${i}`, text })),
	);
	return (
		<div className="yc-term">
			<style>{css}</style>
			<span className="yc-term-title">终端</span>
			<div className="yc-term-body">
				<div className="yc-term-out">
					{lines.length === 0 ? (
						<div className="yc-term-line t-cmd">$ # 终端已准备就绪，支持执行 Shell 命令</div>
					) : (
						lines.map((l) => (
							<div key={l.key} className={"yc-term-line " + classifyLine(l.text)}>
								{l.text}
							</div>
						))
					)}
				</div>
				<div className="yc-term-input-row">
					<span className="yc-term-prompt">$</span>
					<input
						className="yc-term-input"
						type="text"
						disabled
						placeholder="终端执行由对话内工具完成"
						aria-label="终端输入"
					/>
				</div>
			</div>
		</div>
	);
}

export default TermPanel;

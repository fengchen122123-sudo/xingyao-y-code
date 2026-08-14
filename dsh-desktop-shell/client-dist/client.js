window.__ModuleLoader__.load({
	id: "dsh-desktop-shell",
	factory: (require) => {
		var module = { exports: {} };
		var exports = module.exports;
//#region rolldown:runtime
var __create = Object.create;
var __defProp = Object.defineProperty;
var __getOwnPropDesc = Object.getOwnPropertyDescriptor;
var __getOwnPropNames = Object.getOwnPropertyNames;
var __getProtoOf = Object.getPrototypeOf;
var __hasOwnProp = Object.prototype.hasOwnProperty;
var __copyProps = (to, from, except, desc) => {
	if (from && typeof from === "object" || typeof from === "function") for (var keys = __getOwnPropNames(from), i = 0, n = keys.length, key; i < n; i++) {
		key = keys[i];
		if (!__hasOwnProp.call(to, key) && key !== except) __defProp(to, key, {
			get: ((k) => from[k]).bind(null, key),
			enumerable: !(desc = __getOwnPropDesc(from, key)) || desc.enumerable
		});
	}
	return to;
};
var __toESM = (mod, isNodeMode, target) => (target = mod != null ? __create(__getProtoOf(mod)) : {}, __copyProps(isNodeMode || !mod || !mod.__esModule ? __defProp(target, "default", {
	value: mod,
	enumerable: true
}) : target, mod));

//#endregion
const react = __toESM(require("react"));
const react_jsx_runtime = __toESM(require("react/jsx-runtime"));

//#region client/theme/tokens.ts
/**
* tokens.css 的字符串内联版本 —— 与 tokens.css 同步维护。
*
* 原因：tsdown(rolldown) 在 Windows 下无法解析 `import "./tokens.css?inline"`
*（?inline 查询串触发 os error 123），导致 build:client 失败；
* 故将 CSS 内容以字符串常量内联在此，ThemeScope 直接从本文件导入。
* tokens.css 文件保留不动，供测试（jsdom 解析）、类型检查与人工核对使用。
*/
const tokensCss = `/**
 * ycode（星瑶）主题 token —— 暖中性色板。
 *
 * 变量统一使用 --yc- 前缀，避免与 DSH 宿主界面的 --dsw- 变量冲突；
 * 不挂在 :root 上，而是挂在 [data-ycode-theme] 选择器下，
 * 由 ThemeScope 容器组件通过 data-ycode-theme 属性切换深/浅色。
 *
 * 深色值：逐值移植自 ycode frontend/src/style.css 的 :root（变量名加 yc- 前缀）。
 * 浅色值：逐值移植自 ycode frontend/src/style.css 的 body.light（显式浅色定义）。
 */

/* ---------- 深色（默认）：暖中性黑，不带蓝调 ---------- */
[data-ycode-theme="dark"] {
  --yc-bg0: #1a1a18;
  --yc-bg1: #20201e;
  --yc-bg2: #272724;
  --yc-bg3: #302f2b;
  --yc-border: #34332f;
  --yc-text: #d9d9d4;
  --yc-text-bright: #edede9;
  --yc-text-dim: #a8a7a0;
  --yc-text-faint: #85847d;
  --yc-accent: #6a92d8;
  --yc-accent-soft: rgba(106, 146, 216, 0.16);
  --yc-green: #4ea96a;
  --yc-yellow: #c9a24a;
  --yc-red: #d9635b;
  --yc-orange: #d08a4e;
  --yc-cyan: #4aabba;
  --yc-purple: #a58bd8;
  --yc-code-bg: #141412;
  --yc-float-bg: rgba(32, 32, 30, 0.97);
  --yc-mono: 'JetBrains Mono', 'Cascadia Code', 'Consolas', monospace;
  --yc-sans: 'Segoe UI', 'Microsoft YaHei', system-ui, sans-serif;
}

/* ---------- 浅色：暖白，逐值移植自 ycode style.css 的 body.light ---------- */
[data-ycode-theme="light"] {
  --yc-bg0: #f4f4f1;
  --yc-bg1: #ffffff;
  --yc-bg2: #efefec;
  --yc-bg3: #e4e4e0;
  --yc-border: #e3e3de;
  --yc-text: #2c2c2a;
  --yc-text-bright: #171716;
  --yc-text-dim: #5c5c56;
  --yc-text-faint: #7a7a73;
  --yc-accent: #3a6dd8;
  --yc-accent-soft: rgba(58, 109, 216, 0.10);
  --yc-green: #2c8a4c;
  --yc-yellow: #96700f;
  --yc-red: #cf3d36;
  --yc-orange: #b85e1f;
  --yc-cyan: #1a7f92;
  --yc-purple: #7b5fc0;
  --yc-code-bg: #f0f0ec;
  --yc-float-bg: rgba(255, 255, 255, 0.97);
  --yc-mono: 'JetBrains Mono', 'Cascadia Code', 'Consolas', monospace;
  --yc-sans: 'Segoe UI', 'Microsoft YaHei', system-ui, sans-serif;
}
`;

//#endregion
//#region client/theme/ThemeScope.tsx
const STYLE_ID = "ycode-theme-tokens";
/** 幂等注入 token 样式表 */
function ensureTokensInjected() {
	if (typeof document === "undefined") return;
	if (document.getElementById(STYLE_ID)) return;
	const style = document.createElement("style");
	style.id = STYLE_ID;
	style.textContent = tokensCss;
	document.head.appendChild(style);
}
function ThemeScope({ theme, children }) {
	ensureTokensInjected();
	return /* @__PURE__ */ (0, react_jsx_runtime.jsx)("div", {
		className: "yc-scope",
		"data-ycode-theme": theme,
		style: {
			height: "100%",
			background: "var(--yc-bg0)",
			color: "var(--yc-text)",
			fontFamily: "var(--yc-sans)"
		},
		children
	});
}

//#endregion
//#region client/panels/PlanPanel.tsx
/**
* 计划进度：completed 数 / 总数 * 100，四舍五入；空数组返回 0。
* 纯函数，便于单测。
*/
function planProgress(todos) {
	if (todos.length === 0) return 0;
	const done = todos.filter((t) => t.status === "completed").length;
	return Math.round(done / todos.length * 100);
}
const css$6 = `
.yc-plan { display: flex; flex-direction: column; gap: 10px; }
.yc-plan-progress-row { display: flex; justify-content: space-between; align-items: center; }
.yc-plan-progress-label { font-size: 12.5px; font-weight: 600; color: var(--yc-text); }
.yc-plan-progress-pct { font-size: 12.5px; font-weight: 700; color: var(--yc-accent); }
.yc-plan-bar { height: 6px; background: var(--yc-bg3); border-radius: 99px; overflow: hidden; }
.yc-plan-bar-fill { height: 100%; background: var(--yc-accent); border-radius: 99px; transition: width 0.25s ease; }
.yc-plan-empty { color: var(--yc-text-faint); font-size: 12px; padding: 12px 0; }
.yc-plan-list { display: flex; flex-direction: column; gap: 8px; }
.yc-plan-item { display: flex; align-items: flex-start; gap: 8px; background: var(--yc-bg2); border: 1px solid var(--yc-border); border-radius: 8px; padding: 8px 10px; font-size: 12.5px; line-height: 1.5; color: var(--yc-text); }
.yc-plan-item-icon { flex-shrink: 0; width: 16px; text-align: center; }
.yc-plan-item-content { flex: 1; min-width: 0; }
.yc-plan-item.yc-done { }
.yc-plan-item.yc-done .yc-plan-item-icon { color: var(--yc-green); }
.yc-plan-item.yc-done .yc-plan-item-content { text-decoration: line-through; color: var(--yc-text-faint); }
.yc-plan-item.yc-doing { border-color: var(--yc-accent); }
.yc-plan-item.yc-doing .yc-plan-item-icon { color: var(--yc-accent); }
.yc-plan-item.yc-doing .yc-plan-item-content { font-weight: 600; }
.yc-plan-item.yc-pending .yc-plan-item-icon { color: var(--yc-text-faint); }
`;
const STATUS_ICON = {
	completed: "✓",
	in_progress: "⟳",
	pending: "○"
};
const STATUS_CLASS$1 = {
	completed: "yc-done",
	in_progress: "yc-doing",
	pending: "yc-pending"
};
const EMPTY_TEXT = "暂无活动计划。当星瑶编排复杂大任务时，计划全景会自动在此呈现。";
function PlanPanel({ useProjection }) {
	const todos = useProjection ? useProjection("todos") ?? [] : [];
	const progress = planProgress(todos);
	return /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
		className: "yc-plan",
		children: [
			/* @__PURE__ */ (0, react_jsx_runtime.jsx)("style", { children: css$6 }),
			/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
				className: "yc-plan-progress-row",
				children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
					className: "yc-plan-progress-label",
					children: "任务计划"
				}), /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("span", {
					className: "yc-plan-progress-pct",
					children: [progress, "%"]
				})]
			}),
			/* @__PURE__ */ (0, react_jsx_runtime.jsx)("div", {
				className: "yc-plan-bar",
				role: "progressbar",
				"aria-valuenow": progress,
				"aria-valuemin": 0,
				"aria-valuemax": 100,
				children: /* @__PURE__ */ (0, react_jsx_runtime.jsx)("div", {
					className: "yc-plan-bar-fill",
					style: { width: progress + "%" }
				})
			}),
			todos.length === 0 ? /* @__PURE__ */ (0, react_jsx_runtime.jsx)("div", {
				className: "yc-plan-empty",
				children: EMPTY_TEXT
			}) : /* @__PURE__ */ (0, react_jsx_runtime.jsx)("div", {
				className: "yc-plan-list",
				children: todos.map((todo, i) => /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
					className: "yc-plan-item " + (STATUS_CLASS$1[todo.status] ?? "yc-pending"),
					children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
						className: "yc-plan-item-icon",
						children: STATUS_ICON[todo.status] ?? "○"
					}), /* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
						className: "yc-plan-item-content",
						children: todo.content
					})]
				}, i))
			})
		]
	});
}

//#endregion
//#region client/panels/TasksPanel.tsx
/**
* 任务耗时格式化：(finishedAt ?? now) - startedAt。
* 对齐 ycode fmtWorkTime：秒 < 60 用 `${s}s`，否则 `${m}m${s%60}s`。
* 纯函数，便于单测。
*/
function formatElapsed(job, now = Date.now()) {
	const ms = Math.max(0, (job.finishedAt ?? now) - job.startedAt);
	const s = Math.floor(ms / 1e3);
	if (s < 60) return `${s}s`;
	return `${Math.floor(s / 60)}m${s % 60}s`;
}
/** 状态文案映射。纯函数，便于单测。 */
function statusLabel(status) {
	switch (status) {
		case "running": return "运行中";
		case "stopping": return "停止中";
		case "completed": return "已完成";
		case "killed": return "已终止";
		case "failed": return "失败";
		default: return status;
	}
}
const css$5 = `
.yc-tasks { display: flex; flex-direction: column; gap: 8px; }
.yc-tasks-head { display: flex; justify-content: space-between; align-items: center; }
.yc-tasks-title { font-size: 12px; font-weight: 600; color: var(--yc-text-dim); }
.yc-tasks-refresh { font-size: 11px; padding: 2px 8px; border-radius: 99px; background: var(--yc-bg2); border: 1px solid var(--yc-border); color: var(--yc-text-dim); cursor: pointer; }
.yc-tasks-refresh:hover { border-color: var(--yc-accent); color: var(--yc-text); }
.yc-tasks-empty { color: var(--yc-text-faint); font-size: 12px; padding: 12px 0; }
.yc-task-card { background: var(--yc-bg2); border: 1px solid var(--yc-border); border-radius: 8px; padding: 9px 12px; display: flex; flex-direction: column; gap: 6px; }
.yc-task-row { display: flex; justify-content: space-between; align-items: center; gap: 8px; }
.yc-task-label { font-family: var(--yc-mono); font-size: 12px; font-weight: 600; color: var(--yc-text); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.yc-task-status { font-size: 11px; flex-shrink: 0; }
.yc-task-status.yc-st-ok { color: var(--yc-green); }
.yc-task-status.yc-st-err { color: var(--yc-red); }
.yc-task-status.yc-st-warn { color: var(--yc-yellow); }
.yc-task-meta { font-size: 11px; color: var(--yc-text-faint); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.yc-task-kill { font-size: 11px; padding: 1px 8px; border-radius: 6px; background: transparent; border: 1px solid var(--yc-border); color: var(--yc-red); cursor: pointer; flex-shrink: 0; }
.yc-task-kill:hover { border-color: var(--yc-red); }
`;
const STATUS_CLASS = {
	running: "yc-st-ok",
	completed: "yc-st-ok",
	failed: "yc-st-err",
	killed: "yc-st-err",
	stopping: "yc-st-warn"
};
function TasksPanel({ useSessions, onKill, onRefresh }) {
	const jobs = useSessions ? useSessions((s) => s?.current ? s.jobsBySession?.[s.current] : void 0) ?? [] : [];
	return /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
		className: "yc-tasks",
		children: [
			/* @__PURE__ */ (0, react_jsx_runtime.jsx)("style", { children: css$5 }),
			/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
				className: "yc-tasks-head",
				children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
					className: "yc-tasks-title",
					children: "后台运行任务"
				}), /* @__PURE__ */ (0, react_jsx_runtime.jsx)("button", {
					type: "button",
					className: "yc-tasks-refresh",
					onClick: () => onRefresh?.(),
					children: "↻ 刷新"
				})]
			}),
			jobs.length === 0 ? /* @__PURE__ */ (0, react_jsx_runtime.jsx)("div", {
				className: "yc-tasks-empty",
				children: "暂无后台活跃任务"
			}) : jobs.map((job) => /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
				className: "yc-task-card",
				children: [/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
					className: "yc-task-row",
					children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
						className: "yc-task-label",
						children: job.label
					}), /* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
						className: "yc-task-status " + (STATUS_CLASS[job.status] ?? ""),
						children: statusLabel(job.status)
					})]
				}), /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
					className: "yc-task-row",
					children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
						className: "yc-task-meta",
						children: job.detail ? `${job.kind} · ${job.detail}` : formatElapsed(job)
					}), job.status === "running" && /* @__PURE__ */ (0, react_jsx_runtime.jsx)("button", {
						type: "button",
						className: "yc-task-kill",
						onClick: () => onKill?.(job.id),
						children: "终止"
					})]
				})]
			}, job.id))
		]
	});
}

//#endregion
//#region client/panels/TermPanel.tsx
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
const TERM_KINDS = [
	"bash",
	"pwsh",
	"pty-send"
];
/**
* 行着色分类，对齐 ycode 终端着色规则。纯函数，便于单测。
* - '$ ' 开头 → 't-cmd'（命令行）
* - '[进程已退出，代码 0]' → 't-ok'（正常退出）
* - '[进程已退出'/'[启动失败'/'…[输出过长' → 't-warn'（异常/截断）
* - 其它 → ''（默认）
*/
function classifyLine(text) {
	if (text.startsWith("$ ")) return "t-cmd";
	if (text.startsWith("[进程已退出，代码 0]")) return "t-ok";
	if (text.startsWith("[进程已退出") || text.startsWith("[启动失败") || text.startsWith("…[输出过长")) return "t-warn";
	return "";
}
/**
* 从 detail 文本中提取退出码（形如 "exit code 3" / "退出码 3" / "code=3"）。
* 找不到时返回 undefined。纯函数。
*/
function extractExitCode(detail) {
	if (!detail) return void 0;
	const m = detail.match(/(?:exit\s*code|退出码|code)[=:：\s]*(-?\d+)/i);
	return m ? Number(m[1]) : void 0;
}
/**
* 把一个终端 job 映射成若干显示行：
* 第一行恒为 `$ <命令>`，第二行为状态行（借 ycode 着色规则显示）。
* 纯函数，便于单测。
*/
function jobToLines(job) {
	const lines = [`$ ${job.label}`];
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
const css$4 = `
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
function TermPanel({ useSessions }) {
	const jobs = useSessions ? useSessions((s) => s?.current ? s.jobsBySession?.[s.current] : void 0) ?? [] : [];
	const termJobs = jobs.filter((j) => TERM_KINDS.includes(j.kind));
	const lines = termJobs.flatMap((j) => jobToLines(j).map((text, i) => ({
		key: `${j.id}:${i}`,
		text
	})));
	return /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
		className: "yc-term",
		children: [
			/* @__PURE__ */ (0, react_jsx_runtime.jsx)("style", { children: css$4 }),
			/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
				className: "yc-term-title",
				children: "终端"
			}),
			/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
				className: "yc-term-body",
				children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("div", {
					className: "yc-term-out",
					children: lines.length === 0 ? /* @__PURE__ */ (0, react_jsx_runtime.jsx)("div", {
						className: "yc-term-line t-cmd",
						children: "$ # 终端已准备就绪，支持执行 Shell 命令"
					}) : lines.map((l) => /* @__PURE__ */ (0, react_jsx_runtime.jsx)("div", {
						className: "yc-term-line " + classifyLine(l.text),
						children: l.text
					}, l.key))
				}), /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
					className: "yc-term-input-row",
					children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
						className: "yc-term-prompt",
						children: "$"
					}), /* @__PURE__ */ (0, react_jsx_runtime.jsx)("input", {
						className: "yc-term-input",
						type: "text",
						disabled: true,
						placeholder: "终端执行由对话内工具完成",
						"aria-label": "终端输入"
					})]
				})]
			})
		]
	});
}

//#endregion
//#region client/panels/FilesPanel.tsx
const css$3 = `
.yc-files { display: flex; flex-direction: column; gap: 8px; }
.yc-files-head { display: flex; justify-content: space-between; align-items: center; }
.yc-files-title { font-size: 12px; font-weight: 600; color: var(--yc-text-dim); }
.yc-files-refresh { font-size: 11px; padding: 2px 8px; border-radius: 99px; background: var(--yc-bg2); border: 1px solid var(--yc-border); color: var(--yc-text-dim); cursor: pointer; }
.yc-files-refresh:hover { border-color: var(--yc-accent); color: var(--yc-text); }
.yc-files-crumbs { display: flex; flex-wrap: wrap; align-items: center; gap: 2px; font-size: 12px; color: var(--yc-text-dim); }
.yc-files-crumb { background: none; border: none; padding: 1px 2px; font-size: 12px; color: var(--yc-text-dim); cursor: pointer; border-radius: 4px; }
.yc-files-crumb:hover { color: var(--yc-text); background: var(--yc-bg2); }
.yc-files-crumb.yc-crumb-current { color: var(--yc-text); font-weight: 600; }
.yc-files-sep { color: var(--yc-text-faint); }
.yc-files-list { display: flex; flex-direction: column; gap: 2px; }
.yc-files-row { display: flex; align-items: center; gap: 6px; padding: 6px 8px; border-radius: 6px; font-size: 12.5px; color: var(--yc-text); cursor: pointer; background: none; border: none; width: 100%; text-align: left; }
.yc-files-row:hover { background: var(--yc-bg2); }
.yc-files-name { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.yc-files-open { font-size: 11px; padding: 0 6px; border-radius: 4px; background: transparent; border: 1px solid transparent; color: var(--yc-text-faint); cursor: pointer; flex-shrink: 0; }
.yc-files-row:hover .yc-files-open { border-color: var(--yc-border); color: var(--yc-text-dim); }
.yc-files-open:hover { border-color: var(--yc-accent) !important; color: var(--yc-text) !important; }
.yc-files-empty { color: var(--yc-text-faint); font-size: 12px; padding: 12px 0; }
.yc-files-truncated { font-size: 11px; color: var(--yc-yellow); padding: 4px 0 0; }
`;
function FilesPanel({ useWorkspaces, listDirectory, openPath }) {
	const workspaces = useWorkspaces ? useWorkspaces((s) => s?.items) ?? [] : [];
	const [currentPath, setCurrentPath] = (0, react.useState)("");
	const [listing, setListing] = (0, react.useState)(null);
	const [loading, setLoading] = (0, react.useState)(false);
	const [error, setError] = (0, react.useState)(false);
	(0, react.useEffect)(() => {
		if (!currentPath && workspaces.length > 0 && workspaces[0]?.path) setCurrentPath(workspaces[0].path);
	}, [currentPath, workspaces]);
	const load = (0, react.useCallback)((path) => {
		if (!listDirectory || !path) return;
		setLoading(true);
		setError(false);
		listDirectory(path).then((res) => {
			setListing(res ?? null);
			setLoading(false);
		}, () => {
			setError(true);
			setLoading(false);
		});
	}, [listDirectory]);
	(0, react.useEffect)(() => {
		if (currentPath) load(currentPath);
	}, [currentPath, load]);
	const crumbs = listing?.crumbs ?? [];
	const entries = (listing?.entries ?? []).filter((e) => !e.hidden);
	const hasWorkspace = workspaces.length > 0;
	return /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
		className: "yc-files",
		children: [
			/* @__PURE__ */ (0, react_jsx_runtime.jsx)("style", { children: css$3 }),
			/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
				className: "yc-files-head",
				children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
					className: "yc-files-title",
					children: "项目文件"
				}), /* @__PURE__ */ (0, react_jsx_runtime.jsx)("button", {
					type: "button",
					className: "yc-files-refresh",
					onClick: () => load(currentPath),
					children: "↻ 刷新"
				})]
			}),
			crumbs.length > 0 && /* @__PURE__ */ (0, react_jsx_runtime.jsx)("div", {
				className: "yc-files-crumbs",
				children: crumbs.map((c, i) => /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("span", { children: [i > 0 && /* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
					className: "yc-files-sep",
					children: "/"
				}), /* @__PURE__ */ (0, react_jsx_runtime.jsx)("button", {
					type: "button",
					className: "yc-files-crumb" + (i === crumbs.length - 1 ? " yc-crumb-current" : ""),
					onClick: () => setCurrentPath(c.path),
					children: c.name
				})] }, c.path || c.name || i))
			}),
			loading ? /* @__PURE__ */ (0, react_jsx_runtime.jsx)("div", {
				className: "yc-files-empty",
				children: "加载中…"
			}) : error ? /* @__PURE__ */ (0, react_jsx_runtime.jsx)("div", {
				className: "yc-files-empty",
				children: "目录读取失败"
			}) : listing ? /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
				className: "yc-files-list",
				children: [entries.length === 0 ? /* @__PURE__ */ (0, react_jsx_runtime.jsx)("div", {
					className: "yc-files-empty",
					children: "该目录下没有子目录"
				}) : entries.map((e) => /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("button", {
					type: "button",
					className: "yc-files-row",
					onClick: () => setCurrentPath(e.path),
					children: [
						/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", { children: "📁" }),
						/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
							className: "yc-files-name",
							children: e.name
						}),
						openPath && /* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
							className: "yc-files-open",
							role: "button",
							title: "在系统中打开",
							onClick: (ev) => {
								ev.stopPropagation();
								openPath(e.path);
							},
							children: "🗂"
						})
					]
				}, e.path)), listing.truncated && /* @__PURE__ */ (0, react_jsx_runtime.jsx)("div", {
					className: "yc-files-truncated",
					children: "⚠ 结果过多，已截断显示"
				})]
			}) : !hasWorkspace ? /* @__PURE__ */ (0, react_jsx_runtime.jsx)("div", {
				className: "yc-files-empty",
				children: "当前为通用自由对话，暂未引入项目文件夹"
			}) : /* @__PURE__ */ (0, react_jsx_runtime.jsx)("div", {
				className: "yc-files-empty",
				children: "加载中…"
			})
		]
	});
}

//#endregion
//#region client/shell/YcodeDetailsPanel.tsx
const TABS = [
	{
		id: "plan",
		label: "计划"
	},
	{
		id: "tasks",
		label: "任务"
	},
	{
		id: "term",
		label: "终端"
	},
	{
		id: "files",
		label: "文件"
	}
];
const css$2 = `
.yc-details { height: 100%; display: flex; flex-direction: column; background: var(--yc-bg1); border-left: 1px solid var(--yc-border); }
.yc-details-tabs { display: flex; border-bottom: 1px solid var(--yc-border); flex-shrink: 0; }
.yc-rtab { flex: 1; padding: 9px 0; text-align: center; font-size: 12.5px; color: var(--yc-text-dim); cursor: pointer; background: none; border: none; border-bottom: 2px solid transparent; transition: 0.15s; position: relative; font-family: var(--yc-sans); }
.yc-rtab:hover { color: var(--yc-text); }
.yc-rtab.yc-active { color: var(--yc-accent); border-bottom-color: var(--yc-accent); }
.yc-rtab .yc-badge { color: var(--yc-accent); font-size: 8px; position: absolute; top: 4px; right: 6px; }
.yc-details-body { flex: 1; overflow-y: auto; overflow-x: auto; padding: 12px; min-height: 0; }
`;
function YcodeDetailsPanel(props) {
	const { hasPlan, useSessions, useWorkspaces, useProjection, listDirectory, openPath } = props;
	const [activeTab, setActiveTab] = (0, react.useState)("plan");
	const todos = useProjection ? useProjection("todos") ?? void 0 : void 0;
	const showPlanBadge = hasPlan ?? (todos !== void 0 && todos.length > 0);
	return /* @__PURE__ */ (0, react_jsx_runtime.jsx)(ThemeScope, {
		theme: "dark",
		children: /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
			className: "yc-details",
			children: [
				/* @__PURE__ */ (0, react_jsx_runtime.jsx)("style", { children: css$2 }),
				/* @__PURE__ */ (0, react_jsx_runtime.jsx)("div", {
					className: "yc-details-tabs",
					children: TABS.map((tab) => /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("button", {
						type: "button",
						className: "yc-rtab" + (activeTab === tab.id ? " yc-active" : ""),
						"aria-current": activeTab === tab.id ? "true" : void 0,
						onClick: () => setActiveTab(tab.id),
						children: [tab.label, tab.id === "plan" && showPlanBadge ? /* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
							className: "yc-badge",
							children: "●"
						}) : null]
					}, tab.id))
				}),
				/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
					className: "yc-details-body",
					children: [
						activeTab === "plan" && /* @__PURE__ */ (0, react_jsx_runtime.jsx)(PlanPanel, { useProjection }),
						activeTab === "tasks" && /* @__PURE__ */ (0, react_jsx_runtime.jsx)(TasksPanel, { useSessions }),
						activeTab === "term" && /* @__PURE__ */ (0, react_jsx_runtime.jsx)(TermPanel, { useSessions }),
						activeTab === "files" && /* @__PURE__ */ (0, react_jsx_runtime.jsx)(FilesPanel, {
							useWorkspaces,
							listDirectory,
							openPath
						})
					]
				})
			]
		})
	});
}

//#endregion
//#region client/assets/icon.ts
/**
* ycode logo（icon.png 缩到 64x64 后的 base64 内联）。
* 生成原因：client.js 是 CJS 产物（无 import.meta），且插件运行时
* 无静态资源服务器可用，故把 logo 内联为 data URI，零外部依赖。
* 源：client/assets/icon.png（ycode frontend/public/icon.png 的拷贝）。
* 重新生成：node -e "..."（见本文件底部注释）或直接替换 ICON_DATA_URI。
*/
const ICON_DATA_URI = "data:image/png;base64,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";

//#endregion
//#region client/shell/YcodeSidebar.tsx
const css$1 = `
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
	{
		id: "skills",
		icon: "❖",
		label: "技能 Skills"
	},
	{
		id: "mcp",
		icon: "⧉",
		label: "MCP 服务"
	},
	{
		id: "plugins",
		icon: "⬡",
		label: "插件"
	}
];
function YcodeSidebar(props) {
	const { wide = true, expandSidebar, renderSlot, startSession, useSessions, useWorkspaces } = props;
	const [collapsedGroups, setCollapsedGroups] = (0, react.useState)({});
	const sessions = useSessions ? useSessions((s) => s?.items ?? s?.list ?? []) : [];
	const workspaces = useWorkspaces ? useWorkspaces((s) => s?.items ?? []) : [];
	const currentWsLabel = workspaces.length > 0 ? workspaces[0]?.title ?? "项目" : "对话";
	if (!wide) return /* @__PURE__ */ (0, react_jsx_runtime.jsx)(ThemeScope, {
		theme: "dark",
		children: /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
			className: "yc-sidebar yc-narrow",
			children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("style", { children: css$1 }), /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
				className: "yc-sb-narrow-icons",
				children: [
					/* @__PURE__ */ (0, react_jsx_runtime.jsx)("button", {
						type: "button",
						className: "yc-sb-narrow-ico",
						title: "新会话",
						onClick: () => startSession?.(),
						children: "✎"
					}),
					/* @__PURE__ */ (0, react_jsx_runtime.jsx)("button", {
						type: "button",
						className: "yc-sb-narrow-ico",
						title: "技能",
						onClick: () => {},
						children: "❖"
					}),
					/* @__PURE__ */ (0, react_jsx_runtime.jsx)("button", {
						type: "button",
						className: "yc-sb-narrow-ico",
						title: "设置",
						onClick: () => {},
						children: "⚙"
					}),
					/* @__PURE__ */ (0, react_jsx_runtime.jsx)("button", {
						type: "button",
						className: "yc-sb-narrow-ico",
						title: "展开侧栏",
						onClick: () => expandSidebar?.(),
						children: "☰"
					})
				]
			})]
		})
	});
	return /* @__PURE__ */ (0, react_jsx_runtime.jsx)(ThemeScope, {
		theme: "dark",
		children: /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
			className: "yc-sidebar yc-wide",
			children: [
				/* @__PURE__ */ (0, react_jsx_runtime.jsx)("style", { children: css$1 }),
				/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
					className: "yc-sb-logo",
					children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("img", {
						src: ICON_DATA_URI,
						alt: "Y-CODE"
					}), /* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
						className: "yc-sb-name",
						children: "Y-CODE"
					})]
				}),
				/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
					className: "yc-sb-menu",
					children: [/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("button", {
						type: "button",
						className: "yc-sb-menu-item",
						onClick: () => startSession?.(),
						children: [
							/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
								className: "yc-sb-mi-ico",
								children: "✎"
							}),
							/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
								className: "yc-sb-mi-label",
								children: "新会话"
							}),
							/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
								className: "yc-sb-mi-count",
								children: currentWsLabel
							}),
							/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
								className: "yc-sb-mi-arrow",
								children: "＋"
							})
						]
					}), MENU_ITEMS.map((item) => /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("button", {
						type: "button",
						className: "yc-sb-menu-item",
						onClick: () => {},
						children: [
							/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
								className: "yc-sb-mi-ico",
								children: item.icon
							}),
							/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
								className: "yc-sb-mi-label",
								children: item.label
							}),
							/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
								className: "yc-sb-mi-arrow",
								children: "›"
							})
						]
					}, item.id))]
				}),
				/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
					className: "yc-sb-section",
					children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", { children: "项目会话" }), /* @__PURE__ */ (0, react_jsx_runtime.jsx)("button", {
						type: "button",
						className: "yc-sb-section-add",
						title: "引入项目文件夹",
						onClick: () => startSession?.(),
						children: "▣"
					})]
				}),
				/* @__PURE__ */ (0, react_jsx_runtime.jsx)("div", {
					className: "yc-sb-scroll",
					children: renderSlot ? renderSlot("sidebar.workspaces", {
						wide,
						expandSidebar
					}) : /* @__PURE__ */ (0, react_jsx_runtime.jsx)("div", {
						style: {
							padding: "10px",
							color: "var(--yc-text-faint)",
							fontSize: 12
						},
						children: "暂无历史会话"
					})
				}),
				/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
					className: "yc-sb-footer",
					children: [/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("button", {
						type: "button",
						className: "yc-sb-footer-btn",
						onClick: () => startSession?.(),
						children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", { children: "▣" }), /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("span", {
							className: "yc-sb-fc",
							children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("b", { children: currentWsLabel }), /* @__PURE__ */ (0, react_jsx_runtime.jsx)("small", { children: "DSH 已连接" })]
						})]
					}), /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("button", {
						type: "button",
						className: "yc-sb-footer-btn",
						onClick: () => {},
						children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", { children: "⚙" }), /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("span", {
							className: "yc-sb-fc",
							children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("b", { children: "设置" }), /* @__PURE__ */ (0, react_jsx_runtime.jsx)("small", { children: "模型配置" })]
						})]
					})]
				})
			]
		})
	});
}

//#endregion
//#region client/shell/YcodeTopbar.tsx
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
function YcodeTopbar(props) {
	const { toggleSidebar, openDetails, closeDetails, onToggleTheme } = props;
	const [theme, setTheme] = (0, react.useState)(props.theme ?? "dark");
	const [rightOpen, setRightOpen] = (0, react.useState)(true);
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
	return /* @__PURE__ */ (0, react_jsx_runtime.jsx)(ThemeScope, {
		theme,
		children: /* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
			className: "yc-topbar",
			"data-shell-overlay-replace": "true",
			children: [
				/* @__PURE__ */ (0, react_jsx_runtime.jsx)("style", { children: css }),
				/* @__PURE__ */ (0, react_jsx_runtime.jsx)("button", {
					type: "button",
					className: "yc-tb-iconbtn",
					title: "切换侧栏",
					onClick: () => toggleSidebar?.(),
					children: "☰"
				}),
				/* @__PURE__ */ (0, react_jsx_runtime.jsxs)("div", {
					className: "yc-tb-logo",
					children: [/* @__PURE__ */ (0, react_jsx_runtime.jsx)("img", {
						src: ICON_DATA_URI,
						alt: "Y-CODE"
					}), /* @__PURE__ */ (0, react_jsx_runtime.jsx)("span", {
						className: "yc-tb-name",
						children: "Y-CODE"
					})]
				}),
				/* @__PURE__ */ (0, react_jsx_runtime.jsx)("div", { className: "yc-tb-spacer" }),
				/* @__PURE__ */ (0, react_jsx_runtime.jsx)("button", {
					type: "button",
					className: "yc-tb-iconbtn",
					title: "切换主题",
					onClick: handleTheme,
					children: theme === "dark" ? "☾" : "☀"
				}),
				/* @__PURE__ */ (0, react_jsx_runtime.jsx)("button", {
					type: "button",
					className: "yc-tb-iconbtn",
					title: "切换右栏",
					onClick: handleRight,
					children: "▐"
				})
			]
		})
	});
}

//#endregion
//#region client/index.tsx
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
function apply(ctx) {
	ctx.effect(() => ctx.slots.register({
		name: "details",
		priority: -10,
		inject: () => ({
			listDirectory: (path) => ctx.workspaces.listDirectory(path),
			openPath: (path) => {
				ctx.workspaces.openPath(path);
			}
		})
	}, YcodeDetailsPanel), "dsh-desktop-shell: details");
	ctx.effect(() => ctx.slots.register({
		name: "sidebar",
		priority: -10,
		children: {
			"sidebar.workspaces": {
				kind: "single",
				scope: "root"
			},
			"sidebar.settings": {
				kind: "single",
				scope: "root"
			},
			"sidebar.footer.action": {
				kind: "list",
				scope: "root"
			}
		},
		inject: () => ({
			startSession: (workspaceId) => {
				ctx.workspaces.startSession(workspaceId);
			},
			toggleSidebar: () => {
				ctx.layout.toggleSidebar();
			}
		})
	}, YcodeSidebar), "dsh-desktop-shell: sidebar");
	ctx.effect(() => ctx.slots.register({
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
			onToggleTheme: (next) => {
				try {
					const theme = ctx.get?.("theme");
					if (theme && typeof theme.setTheme === "function") theme.setTheme(next);
					else {
						const scope = ctx.settingsScope?.bind?.({ namespace: "ui-theme" });
						scope?.update?.({ preference: next });
					}
				} catch {}
			}
		})
	}, YcodeTopbar), "dsh-desktop-shell: topbar");
}
/** client 插件依赖注入声明 */
const inject = [
	"@deepseek-ai/dsh-client-runtime",
	"@deepseek-ai/dsh-client-ui-layout",
	"@deepseek-ai/dsh-client-locale",
	"@deepseek-ai/dsh-client-ui-theme"
];

//#endregion
exports.apply = apply;
exports.inject = inject;
		return module.exports;
	}
});

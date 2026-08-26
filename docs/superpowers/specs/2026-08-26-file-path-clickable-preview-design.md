# 回复正文文件路径可点击（内嵌预览）设计

日期：2026-08-26
状态：已确认，待实现

## 1. 背景与目标

AI 助手在回复正文里经常输出文件路径（如设计文档、改动文件、日志等），当前这些路径只是纯文本，无法直接点击查看。用户希望路径能变成可点击，点击后在 Y-CODE 的内嵌文件预览弹窗中查看文件内容。

## 2. 范围

### 覆盖

1. 显式 Markdown 链接 `[文本](路径)`，其中路径为本地文件路径。
2. 裸文件路径（未用链接语法包裹）自动识别为可点击：
   - 相对路径：`docs/foo.md`、`src/app.py`
   - 相对前缀：`./foo.py`、`../foo.py`
   - Windows 绝对路径：`D:\x\y.py`、`C:/x/y.py`
   - `file:///` 协议路径
3. 反引号包裹的路径 `` `docs/foo.md` ``：按严格「路径形态」判断，命中则识别为可点击，未命中则保持为行内代码。

### 不覆盖

- 多行代码块 ``` ``` ``` ``` 内的路径：保持代码原样，不做识别。
- `http/https/mailto` 链接：维持现状，浏览器打开。
- 非文件路径的普通文本（如 `import os`、`1.2.3`）：不得误识别。

## 3. 现状约束

- `frontend/src/utils/markdown.ts` 的 `sanitizeHref` 目前只放行 `http/https/mailto`，其余一律返回 `#`，导致本地路径链接不可用。
- `MessageBubble.vue` 的 `onMdBodyClick` 是现成的事件委托入口（代码块复制按钮在此处理），可扩展文件链接点击。
- 后端 `read_file_preview` + 前端 `store.openFilePreview` 已完整可用（文件树点击文件即走此链路），自带工作区边界校验、二进制/编码拒绝。
- 后端 `_resolve_real_path` 基于 GUI 进程 CWD 解析相对路径，多会话下不可靠；相对路径必须在前端用 `store.workdir` 拼成绝对路径再传后端（`ChangedFilesPanel.vue` 已有先例）。

## 4. 架构与数据流

```
AI 输出 "docs/foo.md"
  → renderMarkdown → <a href="#" data-file-link="docs/foo.md">docs/foo.md</a>
  → 用户点击 → onMdBodyClick 捕获 a[data-file-link]
  → preventDefault()
  → 相对路径用 store.workdir 拼绝对路径；绝对路径直接用
  → store.openFilePreview(绝对路径)
  → api.read_file_preview(绝对路径)
  → 内嵌预览弹窗展示
```

## 5. 组件改动

### 5.1 `frontend/src/utils/markdown.ts`

**新增路径识别工具：**

- `isLocalFileLink(url: string): boolean` —— 判断一个链接 URL 是否为本地文件路径形态。
- 本地路径判定规则（分层，命中任一即视为本地路径）：
  1. 结构锚点：`file://` 前缀、Windows 盘符 `^[A-Za-z]:[\\/]`、`./`、`../` 前缀、`^/` 开头（在链接括号上下文）。
  2. 路径形态 + 已知扩展名：`[A-Za-z0-9_\-./\\]+\.ext`，`ext` ∈ 已知文本文件扩展名白名单，且 token 含 `/` 或 `\`。
  3. 纯文件名（无分隔符）但以已知扩展名结尾：识别，但需额外排除 `数字.数字.数字` 之类的版本号形态。
- 已知扩展名白名单：`.md .markdown .py .ts .tsx .js .jsx .vue .html .htm .css .scss .less .json .yml .yaml .toml .ini .cfg .conf .txt .log .csv .xml .sql .sh .bat .ps1 .c .h .cpp .hpp .java .go .rs .rb .php .kt .swift .cs .fs .m .gradle .properties .env .gitignore`。

**扩展 `sanitizeHref`：**

- 保持原有 `http/https/mailto` 放行逻辑不变。
- 新增本地路径分支：`isLocalFileLink` 命中 → 返回标记，渲染为 `<a href="#" data-file-link="<原始路径>" data-file-local="1">label</a>`。
- 危险协议（`javascript:`、`data:` 等）仍返回 `#`。

**渲染管线调整：**

- 行内代码 `` `...` `` 提取时，先判断反引号内容是否 `isLocalFileLink`：是 → 提取为文件链接占位（`data-file-link`），否 → 维持 `<code>` 占位。
- 转义后的正文中，对裸路径（未包裹）按 5.1 规则做正则识别，包成 `data-file-link` 链接。裸路径识别只应用结构锚点与「分隔符 + 扩展名」强信号，避免误伤普通文本。

### 5.2 `frontend/src/components/MessageBubble.vue`

**`onMdBodyClick` 新增分支：**

```ts
const link = (e.target as HTMLElement).closest('a[data-file-link]');
if (link) {
  e.preventDefault();
  const raw = link.getAttribute('data-file-link') || '';
  if (!raw) return;
  const target = toAbsoluteLocalPath(raw, store.workdir);
  if (!target) return; // 已在 toAbsoluteLocalPath 内提示
  store.openFilePreview(target);
  return;
}
```

**新增路径拼接工具（建议放 `utils/markdown.ts` 或新 `utils/localPath.ts`）：**

```ts
function toAbsoluteLocalPath(raw: string, workdir: string): string | null
```

- 已是绝对路径（盘符 / `\\server` / `^/` / `file://`）→ 归一化返回。
- 相对路径且 `workdir` 非空 → `workdir + 分隔符 + raw`，归一化后返回。
- 相对路径且 `workdir` 为空 → 调用 `store.notifyModal` 提示「当前会话未绑定工作目录」并返回 null。

### 5.3 样式

本地文件链接加视觉标记（与普通 http 链接区分），例如虚线下划线或前缀图标。放在 `MessageBubble.vue` 的 `<style scoped>` 或全局 `style.css` 的 `.md-body a[data-file-link]`。

## 6. 错误处理与安全

- 文件不存在 / 越界 / 二进制 / 编码不支持 → 后端 `read_file_preview` 返回 `{ok:false,msg}`，`openFilePreview` 已有 `notifyModal` 提示与弹窗关闭兜底，无需新增。
- 相对路径 + 未绑定工作目录 → 前端提示，不静默失败。
- `javascript:` / `data:` 等危险协议仍被 `sanitizeHref` 拦截为 `#`（防注入回归，已有测试覆盖）。
- 后端工作区边界校验保证越界文件无法预览；预览只读、不执行文件。

## 7. 测试计划（TDD，先写失败测试）

1. 裸路径 `docs/foo.md` → `renderMarkdown` 输出含 `data-file-link="docs/foo.md"`。
2. 反引号路径 `` `docs/foo.md` `` → 输出含 `data-file-link`。
3. 显式链接 `[查看](docs/foo.md)` → 输出含 `data-file-link`。
4. `[x](javascript:alert(1))` → 仍被拦截为 `#`（回归）。
5. 普通文本 `import os`、`1.2.3` → 不误识别成路径。
6. `toAbsoluteLocalPath('docs/foo.md', 'D:\\proj')` → `D:\proj\docs\foo.md`；绝对路径原样返回；空 workdir + 相对路径 → null。

## 8. 验收标准

- 上述测试全部通过。
- 手动验证：回复中输出裸路径与显式链接路径，点击均能弹出内嵌预览并显示文件内容。
- 普通文本与代码块内的路径不被误识别。

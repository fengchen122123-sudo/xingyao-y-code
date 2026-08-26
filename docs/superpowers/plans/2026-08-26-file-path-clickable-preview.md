# 回复正文文件路径可点击（内嵌预览）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 AI 回复正文中的本地文件路径（显式链接 + 裸路径 + 反引号路径）点击后在 Y-CODE 内嵌预览弹窗中打开。

**Architecture:** 新增 `localPath.ts` 纯函数模块（路径识别 + 相对路径拼绝对路径）；`markdown.ts` 在渲染时把本地路径包成 `<a data-file-link>`；`MessageBubble.vue` 复用现有事件委托捕获点击，调用既有的 `store.openFilePreview` 走内嵌预览。

**Tech Stack:** Vue 3 + Pinia + TypeScript + Vitest（jsdom）+ @vue/test-utils。

## Global Constraints

- 测试框架 Vitest，运行命令 `cd frontend; npm test`（等价 `vitest run`）。
- 组件测试用 `createPinia` + `setActivePinia`，后端 API 通过 `(window as any).pywebview = { api: {...} }` mock。
- 本地文件链接渲染为 `<a href="#" data-file-link="<原始路径>" data-file-local="1">label</a>`；`href="#"` 阻止默认跳转，真实路径放 `data-file-link`。
- 危险协议（`javascript:`/`data:` 等）仍必须被拦截为 `#`（回归）。
- 相对路径必须在前端用 `store.workdir` 拼成绝对路径后再调后端（后端 `_resolve_real_path` 按进程 CWD 解析，多会话下不可靠）。
- 不识别含空白或 `< > " ' \`` ` 的字符串为路径（防注入 + 防误判）。

---

### Task 1: 新建 localPath.ts 纯函数模块（路径识别 + 相对路径拼绝对路径）

**Files:**
- Create: `frontend/src/utils/localPath.ts`
- Test: `frontend/src/utils/localPath.test.ts`

**Interfaces:**
- Consumes: 无（本计划第一个任务）。
- Produces:
  - `isLocalFileLink(url: string): boolean` —— 判断字符串是否为本地文件路径形态。
  - `toAbsoluteLocalPath(raw: string, workdir: string): string | null` —— 相对路径拼 workdir 成绝对路径；绝对路径原样返回；相对路径且 workdir 为空返回 null。

- [ ] **Step 1: 写失败测试**

创建 `frontend/src/utils/localPath.test.ts`：

```ts
import { describe, expect, it } from 'vitest';
import { isLocalFileLink, toAbsoluteLocalPath } from './localPath';

describe('isLocalFileLink', () => {
  it('识别相对路径（含分隔符 + 已知扩展名）', () => {
    expect(isLocalFileLink('docs/foo.md')).toBe(true);
    expect(isLocalFileLink('src\\app.py')).toBe(true);
  });
  it('识别相对前缀 ./ 与 ../', () => {
    expect(isLocalFileLink('./foo.py')).toBe(true);
    expect(isLocalFileLink('../foo.py')).toBe(true);
  });
  it('识别盘符绝对路径', () => {
    expect(isLocalFileLink('D:\\x\\y.py')).toBe(true);
    expect(isLocalFileLink('C:/x/y.py')).toBe(true);
  });
  it('识别 file:// 协议路径', () => {
    expect(isLocalFileLink('file:///C:/x/y.md')).toBe(true);
  });
  it('识别纯文件名（已知扩展名）', () => {
    expect(isLocalFileLink('app.py')).toBe(true);
    expect(isLocalFileLink('2026-08-26.md')).toBe(true);
  });
  it('拒绝 URL 与危险协议', () => {
    expect(isLocalFileLink('https://example.com')).toBe(false);
    expect(isLocalFileLink('https://example.md')).toBe(false);
    expect(isLocalFileLink('javascript:alert(1)')).toBe(false);
  });
  it('拒绝非路径普通文本', () => {
    expect(isLocalFileLink('1.2.3')).toBe(false);
    expect(isLocalFileLink('foo.bar')).toBe(false);
    expect(isLocalFileLink('import os')).toBe(false);
    expect(isLocalFileLink('hello world.md')).toBe(false);
    expect(isLocalFileLink('<img src=x.md>')).toBe(false);
  });
});

describe('toAbsoluteLocalPath', () => {
  it('相对路径拼 workdir 成绝对路径（统一反斜杠）', () => {
    expect(toAbsoluteLocalPath('docs/foo.md', 'D:\\proj')).toBe('D:\\proj\\docs\\foo.md');
    expect(toAbsoluteLocalPath('./foo.py', 'D:\\proj')).toBe('D:\\proj\\foo.py');
  });
  it('绝对路径原样返回', () => {
    expect(toAbsoluteLocalPath('D:\\x\\y.py', 'D:\\proj')).toBe('D:\\x\\y.py');
    expect(toAbsoluteLocalPath('C:/x/y.py', '')).toBe('C:/x/y.py');
  });
  it('file:// 转为本地路径', () => {
    expect(toAbsoluteLocalPath('file:///C:/x/y.md', '')).toBe('C:/x/y.md');
  });
  it('相对路径且 workdir 为空返回 null', () => {
    expect(toAbsoluteLocalPath('docs/foo.md', '')).toBeNull();
  });
});
```

- [ ] **Step 2: 运行测试验证失败**

Run: `cd frontend; npx vitest run src/utils/localPath.test.ts`
Expected: 全部 FAIL，报 `Cannot find module './localPath'`。

- [ ] **Step 3: 写实现**

创建 `frontend/src/utils/localPath.ts`：

```ts
// 本地文件路径识别与归一化工具。
// 渲染层（markdown.ts）用它判断哪些链接/文本是本地文件路径；
// 点击层（MessageBubble.vue）用它把相对路径拼成绝对路径再交后端预览。

const KNOWN_EXTENSIONS = new Set([
  'md', 'markdown', 'py', 'ts', 'tsx', 'js', 'jsx', 'vue', 'html', 'htm',
  'css', 'scss', 'less', 'json', 'yml', 'yaml', 'toml', 'ini', 'cfg', 'conf',
  'txt', 'log', 'csv', 'xml', 'sql', 'sh', 'bat', 'ps1', 'c', 'h', 'cpp',
  'hpp', 'java', 'go', 'rs', 'rb', 'php', 'kt', 'swift', 'cs', 'fs', 'm',
  'gradle', 'properties', 'env', 'gitignore',
]);

function extOf(s: string): string {
  const m = s.match(/\.([A-Za-z0-9]+)$/);
  return m ? m[1].toLowerCase() : '';
}

function isKnownExtension(s: string): boolean {
  return KNOWN_EXTENSIONS.has(extOf(s));
}

export function isLocalFileLink(url: string): boolean {
  const s = url.trim();
  if (!s) return false;
  // 含空白 / HTML 特殊字符 / 反引号的字符串一律不算路径（防注入 + 防误判）
  if (/[\s<>"'`]/.test(s)) return false;
  if (s.toLowerCase().startsWith('file://')) return true;
  // 其他带 scheme 的 URL（http/https/ftp/...）一律非本地路径
  if (/^[A-Za-z][A-Za-z0-9+.-]*:\/\//.test(s)) return false;
  if (/^[A-Za-z]:[\\/]/.test(s)) return true; // 盘符
  if (s.startsWith('./') || s.startsWith('../')) return true;
  if (s.startsWith('/') && s.length > 1 && !s.startsWith('//')) return true; // Unix 绝对路径
  if (/[\\/]/.test(s) && isKnownExtension(s)) return true; // 含分隔符 + 已知扩展名
  if (!/[\\/]/.test(s) && isKnownExtension(s)) {
    // 纯文件名：去掉扩展名后仍须含字母，避免 "1.2.3" 这类被误判（'3' 本就不在
    // 白名单，此处兜底排除 "1.2.md" 之类纯数字主体）
    return /[A-Za-z]/.test(s.slice(0, -extOf(s).length));
  }
  return false;
}

export function toAbsoluteLocalPath(raw: string, workdir: string): string | null {
  const s = raw.trim();
  if (!s) return null;
  if (s.toLowerCase().startsWith('file://')) {
    let p = s.slice('file://'.length);
    try { p = decodeURIComponent(p); } catch { /* 保留原样 */ }
    if (/^\/[A-Za-z]:[\\/]/.test(p)) p = p.slice(1); // file:///C:/x -> C:/x
    return p;
  }
  if (/^[A-Za-z]:[\\/]/.test(s) || s.startsWith('\\\\') || s.startsWith('/')) {
    return s; // 盘符 / UNC / Unix 根，绝对路径原样返回
  }
  if (!workdir) return null;
  const base = workdir.replace(/[\\/]+$/, '');
  const rel = s.replace(/^[\\/]+/, '').replace(/\//g, '\\');
  return base + '\\' + rel;
}
```

- [ ] **Step 4: 运行测试验证通过**

Run: `cd frontend; npx vitest run src/utils/localPath.test.ts`
Expected: 全部 PASS。

- [ ] **Step 5: Commit**

```bash
git add frontend/src/utils/localPath.ts frontend/src/utils/localPath.test.ts
git commit -m "feat: 新增本地文件路径识别与归一化工具 localPath"
```

---

### Task 2: markdown.ts 渲染本地文件路径为 data-file-link 链接

**Files:**
- Modify: `frontend/src/utils/markdown.ts`
- Test: `frontend/src/components/markdown-filelink.test.ts`

**Interfaces:**
- Consumes: `isLocalFileLink(url: string): boolean`（Task 1）。
- Produces: 渲染输出中的 `<a href="#" data-file-link="..." data-file-local="1">` 结构，供 MessageBubble 点击委托消费。

- [ ] **Step 1: 写失败测试**

创建 `frontend/src/components/markdown-filelink.test.ts`：

```ts
import { describe, expect, it } from 'vitest';
import { renderMarkdown } from '../utils/markdown';

describe('renderMarkdown 本地文件路径链接', () => {
  it('裸路径渲染为 data-file-link 链接', () => {
    const html = renderMarkdown('见 docs/foo.md 文件');
    expect(html).toContain('data-file-link="docs/foo.md"');
    expect(html).toContain('data-file-local="1"');
  });
  it('反引号包裹的路径也渲染为可点击链接', () => {
    const html = renderMarkdown('`docs/foo.md`');
    expect(html).toContain('data-file-link="docs/foo.md"');
    expect(html).not.toContain('<code>docs/foo.md</code>');
  });
  it('显式链接的本地路径渲染为可点击链接', () => {
    const html = renderMarkdown('[查看](docs/foo.md)');
    expect(html).toContain('data-file-link="docs/foo.md"');
    expect(html).toContain('>查看</a>');
  });
  it('危险协议链接仍被拦截为 #', () => {
    const html = renderMarkdown('[x](javascript:alert(1))');
    expect(html).toContain('href="#"');
    expect(html).not.toContain('javascript:');
  });
  it('普通文本不被误识别为路径', () => {
    const html = renderMarkdown('import os 和 1.2.3 版本');
    expect(html).not.toContain('data-file-link');
  });
  it('代码块内的路径不做识别', () => {
    const html = renderMarkdown('```\ndocs/foo.md\n```');
    expect(html).not.toContain('data-file-link');
  });
  it('http 链接保持原有 target 与 rel 属性', () => {
    const html = renderMarkdown('[站点](https://example.com)');
    expect(html).toContain('href="https://example.com"');
    expect(html).toContain('target="_blank"');
  });
});
```

- [ ] **Step 2: 运行测试验证失败**

Run: `cd frontend; npx vitest run src/components/markdown-filelink.test.ts`
Expected: 含 `data-file-link` 的断言 FAIL（当前渲染不产生 `data-file-link`）。

- [ ] **Step 3: 写实现**

用 `edit_file` 对 `frontend/src/utils/markdown.ts` 做如下修改。

**3a. 顶部引入 localPath：**

将：

```ts
// 极简 Markdown 渲染器（手写实现，无第三方依赖）
```

替换为：

```ts
// 极简 Markdown 渲染器（手写实现，无第三方依赖）
import { isLocalFileLink } from './localPath';
```

**3b. 在 `sanitizeHref` 之后新增 `fileLinkHtml` 辅助：**

将：

```ts
export function renderMarkdown(md: string): string {
```

替换为：

```ts
// 本地文件路径链接：href 用 # 阻止默认跳转，真实路径放 data-file-link 属性，
// 由 MessageBubble 的点击委托捕获后走内嵌预览。
function fileLinkHtml(path: string): string {
  return `<a href="#" data-file-link="${escapeHtml(path)}" data-file-local="1">${escapeHtml(path)}</a>`;
}

export function renderMarkdown(md: string): string {
```

**3c. 在占位符声明处新增两个数组：**

将：

```ts
  const codeBlocks: string[] = [];
  const inlineCodes: string[] = [];
```

替换为：

```ts
  const codeBlocks: string[] = [];
  const inlineCodes: string[] = [];
  const fileLinks: string[] = [];
  const links: string[] = [];
```

**3d. 修改行内代码保护逻辑（区分路径与代码）：**

将：

```ts
  // 2. 保护行内代码 `code`
  const inlineCodes: string[] = [];
  text = text.replace(/`([^`]+)`/g, (_, code) => {
    const idx = inlineCodes.length;
    const escaped = escapeHtml(code);
    inlineCodes.push(`<code>${escaped}</code>`);
    return `${PH}IC${idx}${PH}`;
  });
```

替换为：

```ts
  // 2. 行内代码：反引号内若是本地文件路径则渲染为可点击链接，否则保持 <code>
  text = text.replace(/`([^`]+)`/g, (_, code) => {
    if (isLocalFileLink(code)) {
      const idx = fileLinks.length;
      fileLinks.push(fileLinkHtml(code));
      return `${PH}FL${idx}${PH}`;
    }
    const idx = inlineCodes.length;
    inlineCodes.push(`<code>${escapeHtml(code)}</code>`);
    return `${PH}IC${idx}${PH}`;
  });
```

**3e. 在转义前新增显式链接与裸路径识别，并把转义下移到其后：**

将（含原「3. 转义」与「5. 链接」两段）：

```ts
  // 3. 关键修复：转义剩余的全部文本，再叠加标记。
  //    此步之后，文本中原有的 <, >, &, ", ' 均已变为安全实体，
  //    后续所有正则只会在“已转义”的文本上叠加真正的标签。
  text = escapeHtml(text);

  // 4. 标题（# / ## / ###，井号不会被转义，故可在转义后继续匹配）
  text = text.replace(/^### (.*)$/gim, '<h4>$1</h4>');
  text = text.replace(/^## (.*)$/gim, '<h3>$1</h3>');
  text = text.replace(/^# (.*)$/gim, '<h2>$1</h2>');

  // 4.5 分割线 --- 或 ***
  text = text.replace(/^(?:---|\*\*\*)$/gim, '<hr>');

  // 5. 链接 [text](url) —— href 经 sanitizeHref 白名单过滤
  text = text.replace(/\[([^\]]*)\]\(([^)\s]+)\)/g, (_m, label, url) => {
    const safeHref = sanitizeHref(url);
    return `<a href="${safeHref}" target="_blank" rel="noopener noreferrer">${label}</a>`;
  });
```

替换为：

```ts
  // 3. 显式链接 [label](url)：本地文件路径 → 可点击预览；否则走 http/https/mailto 白名单
  text = text.replace(/\[([^\]]*)\]\(([^)\s]+)\)/g, (_m, label, url) => {
    const idx = links.length;
    if (isLocalFileLink(url)) {
      links.push(`<a href="#" data-file-link="${escapeHtml(url)}" data-file-local="1">${escapeHtml(label)}</a>`);
    } else {
      const safeHref = sanitizeHref(url);
      links.push(`<a href="${safeHref}" target="_blank" rel="noopener noreferrer">${escapeHtml(label)}</a>`);
    }
    return `${PH}L${idx}${PH}`;
  });

  // 4. 裸路径识别：此时代码块/行内代码/显式链接均已占位，只在剩余纯文本上匹配，
  //    命中后用 isLocalFileLink 二次过滤（正则的尾段会匹配到 1.2 / foo.bar 等，靠白名单排除）
  const LOCAL_PATH_RE = /(?:file:\/\/[^\s<>"'`()]+|[A-Za-z]:[\\/][^\s<>"'`()]+|\.{1,2}[\\/][^\s<>"'`()]+|[^\s<>"'`()]+[\\/][^\s<>"'`()]*\.[A-Za-z0-9]+|[A-Za-z0-9_\-]+\.[A-Za-z0-9]+)/g;
  text = text.replace(LOCAL_PATH_RE, (m) => {
    if (!isLocalFileLink(m)) return m;
    const idx = fileLinks.length;
    fileLinks.push(fileLinkHtml(m));
    return `${PH}FL${idx}${PH}`;
  });

  // 5. 关键修复：转义剩余的全部文本，再叠加标记。
  //    此步之后，文本中原有的 <, >, &, ", ' 均已变为安全实体，
  //    后续所有正则只会在“已转义”的文本上叠加真正的标签。
  text = escapeHtml(text);

  // 6. 标题（# / ## / ###，井号不会被转义，故可在转义后继续匹配）
  text = text.replace(/^### (.*)$/gim, '<h4>$1</h4>');
  text = text.replace(/^## (.*)$/gim, '<h3>$1</h3>');
  text = text.replace(/^# (.*)$/gim, '<h2>$1</h2>');

  // 6.5 分割线 --- 或 ***
  text = text.replace(/^(?:---|\*\*\*)$/gim, '<hr>');
```

**3f. 调整粗体/斜体注释序号（可选，不改逻辑），并扩展占位符还原：**

将文件末尾的：

```ts
  codeBlocks.forEach((cb, idx) => {
    html = html.replace(`${PH}CB${idx}${PH}`, () => cb);
  });
  inlineCodes.forEach((ic, idx) => {
    html = html.replace(`${PH}IC${idx}${PH}`, () => ic);
  });

  return html;
```

替换为：

```ts
  codeBlocks.forEach((cb, idx) => {
    html = html.replace(`${PH}CB${idx}${PH}`, () => cb);
  });
  inlineCodes.forEach((ic, idx) => {
    html = html.replace(`${PH}IC${idx}${PH}`, () => ic);
  });
  fileLinks.forEach((fl, idx) => {
    html = html.replace(`${PH}FL${idx}${PH}`, () => fl);
  });
  links.forEach((l, idx) => {
    html = html.replace(`${PH}L${idx}${PH}`, () => l);
  });

  return html;
```

- [ ] **Step 4: 运行测试验证通过**

Run: `cd frontend; npx vitest run src/components/markdown-filelink.test.ts`
Expected: 全部 PASS。同时跑既有 markdown 系列测试确认无回归：`cd frontend; npx vitest run src/components/markdown-codeblock.test.ts src/components/markdown-list.test.ts src/components/markdown-table.test.ts`。

- [ ] **Step 5: Commit**

```bash
git add frontend/src/utils/markdown.ts frontend/src/components/markdown-filelink.test.ts
git commit -m "feat: markdown 渲染本地文件路径为可点击链接"
```

---

### Task 3: MessageBubble 点击本地链接打开内嵌预览 + 样式

**Files:**
- Modify: `frontend/src/components/MessageBubble.vue`
- Test: `frontend/src/components/MessageBubble.test.ts`

**Interfaces:**
- Consumes: `toAbsoluteLocalPath(raw: string, workdir: string): string | null`（Task 1）、`store.openFilePreview(path: string)`（已有）、`store.workdir`（已有）、`store.notifyModal(title: string, msg: string)`（已有）。
- Produces: 点击 `<a data-file-link>` 触发内嵌预览的行为。

- [ ] **Step 1: 写失败测试**

在 `frontend/src/components/MessageBubble.test.ts` 顶部 import 处加入：

```ts
import { flushPromises, mount } from '@vue/test-utils';
```
（原文件已 `import { mount, flushPromises } from '@vue/test-utils';`，无需改动 import。）

在 `describe('MessageBubble', () => {` 内、现有用例之后追加两个用例：

```ts
  it('点击本地文件链接用绝对路径打开内嵌预览', async () => {
    const store = useAppStore();
    store.workdir = 'D:\\proj';
    const readFilePreview = vi.fn().mockResolvedValue({ ok: true, content: 'hello', truncated: false, path: 'D:\\proj\\docs\\foo.md' });
    (window as any).pywebview = { api: { read_file_preview: readFilePreview } };

    const wrapper = mount(MessageBubble, {
      props: { msg: assistantMessage({ content: '见 docs/foo.md 文件' }) },
    });

    const link = wrapper.find('a[data-file-link]');
    expect(link.exists()).toBe(true);
    await link.trigger('click');
    await flushPromises();

    expect(readFilePreview).toHaveBeenCalledWith('D:\\proj\\docs\\foo.md');
    wrapper.unmount();
    delete (window as any).pywebview;
  });

  it('相对路径且未绑定工作目录时提示，不发起预览', async () => {
    const store = useAppStore();
    store.workdir = '';
    const readFilePreview = vi.fn();
    (window as any).pywebview = { api: { read_file_preview: readFilePreview } };
    const notify = vi.spyOn(store, 'notifyModal').mockImplementation(() => {});

    const wrapper = mount(MessageBubble, {
      props: { msg: assistantMessage({ content: 'docs/foo.md' }) },
    });

    await wrapper.find('a[data-file-link]').trigger('click');
    await flushPromises();

    expect(notify).toHaveBeenCalled();
    expect(readFilePreview).not.toHaveBeenCalled();
    wrapper.unmount();
    delete (window as any).pywebview;
  });
```

- [ ] **Step 2: 运行测试验证失败**

Run: `cd frontend; npx vitest run src/components/MessageBubble.test.ts`
Expected: 两个新用例 FAIL——当前点击 `<a data-file-link>` 无处理，`readFilePreview` 未被调用。

- [ ] **Step 3: 写实现**

**3a. 引入 toAbsoluteLocalPath：**

在 `frontend/src/components/MessageBubble.vue` 的 `<script setup lang="ts">` import 区，将：

```ts
import { renderMarkdown } from '../utils/markdown';
```

替换为：

```ts
import { renderMarkdown } from '../utils/markdown';
import { toAbsoluteLocalPath } from '../utils/localPath';
```

**3b. 修改 `onMdBodyClick` 增加本地链接分支：**

将：

```ts
// Markdown 代码块复制按钮：事件委托，命中 .code-copy 时复制同级 <code> 内容
function onMdBodyClick(e: MouseEvent) {
  const btn = (e.target as HTMLElement).closest('.code-copy');
  if (!btn) return;
```

替换为：

```ts
// Markdown 内容点击事件委托：本地文件链接走内嵌预览，代码块复制按钮走复制
function onMdBodyClick(e: MouseEvent) {
  const fileLink = (e.target as HTMLElement).closest('a[data-file-link]');
  if (fileLink) {
    e.preventDefault();
    const raw = fileLink.getAttribute('data-file-link') || '';
    if (!raw) return;
    const target = toAbsoluteLocalPath(raw, store.workdir);
    if (!target) {
      store.notifyModal('文件预览', '当前会话未绑定工作目录，无法解析相对路径');
      return;
    }
    store.openFilePreview(target);
    return;
  }

  const btn = (e.target as HTMLElement).closest('.code-copy');
  if (!btn) return;
```

**3c. 增加本地链接视觉样式：**

在 `<style scoped>` 内 `.md-body { ... }` 规则之后追加：

```css
/* 本地文件链接：虚线强调，与普通 http 链接区分，点击走内嵌预览 */
.md-body :deep(a[data-file-link]) {
  color: var(--accent);
  text-decoration: underline dotted;
  cursor: pointer;
}
```

- [ ] **Step 4: 运行测试验证通过**

Run: `cd frontend; npx vitest run src/components/MessageBubble.test.ts`
Expected: 全部 PASS（含新增两个用例与既有用例）。

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/MessageBubble.vue frontend/src/components/MessageBubble.test.ts
git commit -m "feat: 点击回复中的本地文件路径打开内嵌预览"
```

---

### Task 4: 全量测试 + 类型/构建验证 + 收尾

**Files:**
- 无新改动（仅验证与可能的修正）。

**Interfaces:**
- Consumes: Task 1-3 的全部产物。
- Produces: 全绿测试 + 通过 vue-tsc 类型检查与 vite 构建。

- [ ] **Step 1: 全量前端测试**

Run: `cd frontend; npm test`
Expected: 所有测试 PASS，无 error/warning。

- [ ] **Step 2: 类型检查 + 构建**

Run: `cd frontend; npm run build`
Expected: `vue-tsc -b` 与 `vite build` 均成功，无 TS 报错。

- [ ] **Step 3: 若 Step 1/2 暴露回归，定位并修正后重跑对应验证**

若失败，读报错定位根因（大概率是 `markdown.ts` 注释序号、或某占位符还原遗漏），修正后回到 Step 1/2。

- [ ] **Step 4: 最终提交（如有修正）**

```bash
git status
git add -u
git commit -m "test: 文件路径可点击内嵌预览全量验证修正"
```

---

## Self-Review

**1. Spec coverage：**
- 裸路径识别 → Task 2 步骤 4 + 测试「裸路径」。
- 反引号路径识别 → Task 2 步骤 3d + 测试「反引号包裹」。
- 显式链接识别 → Task 2 步骤 3e + 测试「显式链接」。
- 危险协议拦截回归 → Task 2 测试「危险协议」。
- 普通文本不误识别 → Task 1 `isLocalFileLink` 用例 + Task 2 测试「普通文本」。
- 相对路径拼绝对路径 → Task 1 `toAbsoluteLocalPath` + Task 3 测试。
- 未绑定工作目录提示 → Task 3 测试「未绑定工作目录」。
- 后端 `read_file_preview` 复用 → Task 3 调用 `store.openFilePreview`（内部已走 `read_file_preview`）。

**2. Placeholder scan：** 无 TBD/TODO；每个代码 step 均给出完整代码。

**3. Type consistency：** `isLocalFileLink` / `toAbsoluteLocalPath` / `fileLinkHtml` 命名在 Task 1-3 中一致；`data-file-link` / `data-file-local` 属性名一致。

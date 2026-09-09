# 流式块级增量渲染 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 GUI 流式输出期间只重渲染「正在生长的最后一块」，已完成块的 HTML 与 DOM 全部复用，消除每 60ms 全量重渲染带来的 CPU 开销。

**Architecture:** 新增源码级切分器 `splitStableBlocks`（在安全空行处切分，取倒数第二个空行作边界，保证生长中的块永远留在尾部）；`renderMarkdown` 拆出携带 `mediaCount` 的详细版以支持媒体序号跨块传递；`MessageBubble.vue` 改为块数组 + 稳定 key 渲染，已完成块走组件级缓存。

**Tech Stack:** Vue 3.5 / TypeScript / Vitest 4 / Vite 8

**设计文档：** `docs/superpowers/specs/2026-09-09-streaming-block-render-design.md`

## Global Constraints

- 所有前端命令在 `E:\fch\Y-code_tui\frontend` 目录执行。
- 测试命令：`npm test`（等价 `vitest run`）；类型检查与构建：`npm run build`。
- `renderMarkdown(md: string, mediaCtx?: MediaContext): string` 对外签名**必须保持不变**（7 个现有测试文件依赖它）。
- 黄金断言：分块渲染拼接结果必须与整段渲染**逐字节一致**。
- 现有测试 `markdown.test.ts` / `markdown-media.test.ts` / `markdown-mermaid.test.ts` / `markdown-filelink.test.ts` / `markdown-codeblock.test.ts` / `markdown-list.test.ts` / `markdown-table.test.ts` 必须全绿。
- 每个任务结束必须提交一次 git commit（分支 `dev`）。

---

### Task 1: markdown.ts 拆出详细版渲染 + 代码块范围查询

**Files:**
- Modify: `frontend/src/utils/markdown.ts`
- Create: `frontend/src/utils/markdown-detailed.test.ts`

**Interfaces:**
- Produces:
  - `findCodeBlockRanges(source: string): Array<[number, number]>` — 返回所有**已闭合**代码块的 `[start, end)` 字符范围
  - `renderMarkdownDetailed(md: string, mediaCtx?: MediaContext, startMediaOrdinal?: number): { html: string; mediaCount: number }`
  - `renderMarkdown(md: string, mediaCtx?: MediaContext): string` — 签名不变

- [ ] **Step 1: 写失败测试** — 创建 `frontend/src/utils/markdown-detailed.test.ts`

```ts
import { describe, it, expect } from 'vitest';
import { findCodeBlockRanges, renderMarkdown, renderMarkdownDetailed } from './markdown';
import type { MediaItem } from '../types/media';

const img = (ordinal: number): MediaItem => ({
  ordinal,
  kind: 'image',
  mime: 'image/png',
  ext: 'png',
  caption: '图',
  url: `http://127.0.0.1:50000/m/${'a'.repeat(64)}.png?t=tok`,
});

describe('findCodeBlockRanges', () => {
  it('返回已闭合代码块的字符范围', () => {
    const src = 'a\n```ts\nconst x = 1;\n```\nb';
    const ranges = findCodeBlockRanges(src);
    expect(ranges).toHaveLength(1);
    expect(src.slice(ranges[0][0], ranges[0][1])).toBe('```ts\nconst x = 1;\n```');
  });

  it('未闭合代码块不计入', () => {
    expect(findCodeBlockRanges('a\n```ts\nconst x = 1;\n')).toEqual([]);
  });

  it('空输入返回空数组', () => {
    expect(findCodeBlockRanges('')).toEqual([]);
  });

  it('多个代码块按顺序返回', () => {
    const src = '```a\n1\n```\n\n```b\n2\n```';
    expect(findCodeBlockRanges(src)).toHaveLength(2);
  });
});

describe('renderMarkdownDetailed', () => {
  it('html 与 renderMarkdown 完全一致', () => {
    const md = '# 标题\n\n正文 **加粗** 与 `code`\n\n- a\n- b';
    expect(renderMarkdownDetailed(md).html).toBe(renderMarkdown(md));
  });

  it('无媒体时 mediaCount 为 0', () => {
    expect(renderMarkdownDetailed('纯文本').mediaCount).toBe(0);
  });

  it('mediaCount 统计本地媒体语法数量（含清单越界项）', () => {
    const md = '![一](a.png)\n\n![二](b.png)';
    expect(renderMarkdownDetailed(md, { items: [] }).mediaCount).toBe(2);
  });

  it('起始序号生效：data-media-ordinal 从 startMediaOrdinal 起算', () => {
    const md = '![一](a.png)';
    const html = renderMarkdownDetailed(md, { items: [img(0), img(1)] }, 1).html;
    expect(html).toContain('data-media-ordinal="1"');
  });

  it('空输入返回空 html 与 0 计数', () => {
    expect(renderMarkdownDetailed('')).toEqual({ html: '', mediaCount: 0 });
  });
});
```

- [ ] **Step 2: 跑测试确认失败**

Run: `npx vitest run src/utils/markdown-detailed.test.ts`
Expected: FAIL — `findCodeBlockRanges is not a function`

- [ ] **Step 3: 实现**

在 `markdown.ts` 中（`escapeHtml` 定义之后）新增：

```ts
// 代码块模式：renderMarkdown 提取与 findCodeBlockRanges 共用同一 pattern，
// 保证切分器与渲染器对「代码块范围」的判定完全一致。
const CODE_BLOCK_PATTERN = '```(\\w*)\\n([\\s\\S]*?)```';

/** 返回所有已闭合代码块的 [start, end) 字符范围。 */
export function findCodeBlockRanges(source: string): Array<[number, number]> {
  if (!source) return [];
  const ranges: Array<[number, number]> = [];
  const re = new RegExp(CODE_BLOCK_PATTERN, 'g');
  let match: RegExpExecArray | null;
  while ((match = re.exec(source)) !== null) {
    ranges.push([match.index, match.index + match[0].length]);
  }
  return ranges;
}
```

把原 `renderMarkdown` 改造为详细版 + 兼容包装：

```ts
export function renderMarkdown(md: string, mediaCtx?: MediaContext): string {
  return renderMarkdownDetailed(md, mediaCtx).html;
}

export function renderMarkdownDetailed(
  md: string,
  mediaCtx?: MediaContext,
  startMediaOrdinal = 0,
): { html: string; mediaCount: number } {
  if (!md) return { html: '', mediaCount: 0 };
  let text = md;
  // ...（其余实现保持不变）...
```

改动点（原实现内）：

1. `let mediaOrdinal = 0;` → `let mediaOrdinal = startMediaOrdinal;`
2. 代码块替换 `text.replace(/```(\w*)\n([\s\S]*?)```/g, ...)` → `text.replace(new RegExp(CODE_BLOCK_PATTERN, 'g'), ...)`
3. 结尾 `return html;` → `return { html, mediaCount: mediaOrdinal - startMediaOrdinal };`

- [ ] **Step 4: 跑测试确认通过**

Run: `npx vitest run src/utils/markdown-detailed.test.ts src/utils/markdown.test.ts src/utils/markdown-media.test.ts`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add frontend/src/utils/markdown.ts frontend/src/utils/markdown-detailed.test.ts
git commit -m "refactor(markdown): 拆出 renderMarkdownDetailed 与 findCodeBlockRanges"
```

---

### Task 2: markdownBlocks.ts 源码切分器

**Files:**
- Create: `frontend/src/utils/markdownBlocks.ts`
- Create: `frontend/src/utils/markdownBlocks.test.ts`

**Interfaces:**
- Consumes: `findCodeBlockRanges`、`renderMarkdown`、`renderMarkdownDetailed`（Task 1）
- Produces: `splitStableBlocks(source: string): { blocks: string[]; tail: string }`

- [ ] **Step 1: 写失败测试** — 创建 `frontend/src/utils/markdownBlocks.test.ts`

```ts
import { describe, it, expect } from 'vitest';
import { splitStableBlocks } from './markdownBlocks';
import { renderMarkdown, renderMarkdownDetailed } from './markdown';
import type { MediaItem } from '../types/media';

const img = (ordinal: number): MediaItem => ({
  ordinal,
  kind: 'image',
  mime: 'image/png',
  ext: 'png',
  caption: '图',
  url: `http://127.0.0.1:50000/m/${'a'.repeat(64)}.png?t=tok`,
});

describe('splitStableBlocks', () => {
  it('无空行时全部进 tail', () => {
    expect(splitStableBlocks('一行文字')).toEqual({ blocks: [], tail: '一行文字' });
  });

  it('只有一个安全空行时全部进 tail（滞后一块）', () => {
    const src = 'a\n\nb';
    expect(splitStableBlocks(src)).toEqual({ blocks: [], tail: src });
  });

  it('两个安全空行时第一个空行前成为稳定块', () => {
    const src = 'a\n\nb\n\nc';
    expect(splitStableBlocks(src)).toEqual({ blocks: ['a\n\n'], tail: 'b\n\nc' });
  });

  it('代码块内的空行不算切分点', () => {
    const src = '```ts\nconst a = 1;\n\nconst b = 2;\n```\n\n尾部';
    expect(splitStableBlocks(src)).toEqual({ blocks: [], tail: src });
  });

  it('空输入返回空块与空尾', () => {
    expect(splitStableBlocks('')).toEqual({ blocks: [], tail: '' });
  });

  it('黄金断言：分块渲染拼接 === 整段渲染', () => {
    const cases = [
      'a\n\nb\n\nc\n\nd',
      '# 标题\n\n正文\n\n- 列表一\n- 列表二\n\n```ts\nconst x = 1;\n```\n\n结尾',
      '| a | b |\n| --- | --- |\n| 1 | 2 |\n\n后面一段',
      '> 引用\n> 第二行\n\n普通段落\n\n![图](a.png)',
      '```js\nconsole.log(1)\n```',
      '没有空行的一段话',
      'a\n\n\n\nb\n\nc',
    ];
    for (const src of cases) {
      const { blocks, tail } = splitStableBlocks(src);
      const joined =
        blocks.map((b) => renderMarkdown(b)).join('') + renderMarkdown(tail);
      expect(joined, src).toBe(renderMarkdown(src));
    }
  });

  it('媒体序号跨块连续（黄金断言 + ordinal 校验）', () => {
    const src = '![一](a.png)\n\n![二](b.png)\n\n![三](c.png)';
    const items = [img(0), img(1), img(2)];
    const { blocks, tail } = splitStableBlocks(src);
    let ordinal = 0;
    const html =
      blocks
        .map((b) => {
          const r = renderMarkdownDetailed(b, { items }, ordinal);
          ordinal += r.mediaCount;
          return r.html;
        })
        .join('') + renderMarkdownDetailed(tail, { items }, ordinal).html;
    expect(html).toBe(renderMarkdown(src, { items }));
    expect(html).toContain('data-media-ordinal="0"');
    expect(html).toContain('data-media-ordinal="1"');
    expect(html).toContain('data-media-ordinal="2"');
  });
});
```

- [ ] **Step 2: 跑测试确认失败**

Run: `npx vitest run src/utils/markdownBlocks.test.ts`
Expected: FAIL — 模块不存在

- [ ] **Step 3: 实现** — 创建 `frontend/src/utils/markdownBlocks.ts`

```ts
// 流式块级增量渲染的源码切分器。
//
// 目标：把正在流式生长的 markdown 源码切成「已完成块」与「可变尾部」，让前端只重渲染尾部。
// 切分点选「倒数第二个安全空行」——生长中的最后一块（列表未结束、代码块未闭合）永远留在尾部，
// 不会被误判为已完成；代价是稳定边界滞后一个块，视觉无感。
//
// 安全空行 = 只含空白字符、且不在任何已闭合代码块范围内的行。
// 代码块范围复用 markdown.ts 的 findCodeBlockRanges，保证与渲染器判定一致。

import { findCodeBlockRanges } from './markdown';

export function splitStableBlocks(source: string): { blocks: string[]; tail: string } {
  if (!source) return { blocks: [], tail: '' };

  const ranges = findCodeBlockRanges(source);
  const inCode = (offset: number): boolean =>
    ranges.some(([start, end]) => offset >= start && offset < end);

  // 收集「安全空行之后」的偏移，即下一块的起点
  const cutPoints: number[] = [];
  let lineStart = 0;
  for (let i = 0; i <= source.length; i++) {
    if (i === source.length || source[i] === '\n') {
      const lineText = source.slice(lineStart, i);
      if (lineText.trim() === '' && !inCode(lineStart)) {
        cutPoints.push(i < source.length ? i + 1 : i);
      }
      lineStart = i + 1;
    }
  }

  if (cutPoints.length < 2) return { blocks: [], tail: source };

  const cut = cutPoints[cutPoints.length - 2];
  const blocks: string[] = [];
  let prev = 0;
  for (const point of cutPoints) {
    if (point >= cut) break;
    const chunk = source.slice(prev, point);
    if (chunk.trim()) blocks.push(chunk);
    prev = point;
  }
  const head = source.slice(prev, cut);
  if (head.trim()) blocks.push(head);

  return { blocks, tail: source.slice(cut) };
}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `npx vitest run src/utils/markdownBlocks.test.ts`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add frontend/src/utils/markdownBlocks.ts frontend/src/utils/markdownBlocks.test.ts
git commit -m "feat(markdown): 新增流式块级切分器 splitStableBlocks"
```

---

### Task 3: MessageBubble.vue 块级渲染

**Files:**
- Modify: `frontend/src/components/MessageBubble.vue`
- Create: `frontend/src/components/MessageBubble.blocks.test.ts`

**Interfaces:**
- Consumes: `splitStableBlocks`（Task 2）、`renderMarkdownDetailed`（Task 1）
- Produces: 组件内 `mdBlocks: ComputedRef<{ key: string; html: string }[]>`

- [ ] **Step 1: 写失败测试** — 创建 `frontend/src/components/MessageBubble.blocks.test.ts`

```ts
import { mount, flushPromises } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import MessageBubble from './MessageBubble.vue';
import { useAppStore, type ChatMessage } from '../stores/appStore';

function assistantMessage(overrides: Partial<ChatMessage> = {}): ChatMessage {
  return {
    role: 'assistant',
    content: '',
    reasoning_content: '',
    tool_calls: [],
    legacy_diffs: [],
    execution_state: 'completed',
    ...overrides,
  };
}

beforeEach(() => {
  setActivePinia(createPinia());
  // 文件链接后置校验会请求后端，测试里短路掉
  vi.spyOn(useAppStore(), 'checkPathsKind').mockResolvedValue([]);
});

describe('MessageBubble 块级渲染', () => {
  it('按稳定块 + 尾部块渲染出多个 .md-block', async () => {
    const wrapper = mount(MessageBubble, {
      props: { msg: assistantMessage({ content: 'a\n\nb\n\nc' }) },
    });
    await flushPromises();
    const blocks = wrapper.findAll('.md-block');
    expect(blocks).toHaveLength(2); // 1 个稳定块 + 1 个尾部块
    expect(wrapper.html()).toContain('a');
    expect(wrapper.html()).toContain('b');
    expect(wrapper.html()).toContain('c');
  });

  it('无空行时只有一个尾部块', async () => {
    const wrapper = mount(MessageBubble, {
      props: { msg: assistantMessage({ content: '一段没有空行的文字' }) },
    });
    await flushPromises();
    expect(wrapper.findAll('.md-block')).toHaveLength(1);
  });

  it('流式追加内容时已完成块的 DOM 节点被复用', async () => {
    const msg = assistantMessage({ content: 'a\n\nb\n\nc', execution_state: 'running' });
    const wrapper = mount(MessageBubble, { props: { msg } });
    await flushPromises();
    const firstBlock = wrapper.findAll('.md-block')[0].element;

    await wrapper.setProps({ msg: { ...msg, content: 'a\n\nb\n\nc\n\nd' } });
    await flushPromises();

    const after = wrapper.findAll('.md-block');
    expect(after[0].element).toBe(firstBlock); // 同一个 DOM 节点，未重建
    expect(wrapper.html()).toContain('d');
  });

  it('空内容不渲染 .md-body', async () => {
    const wrapper = mount(MessageBubble, {
      props: { msg: assistantMessage({ content: '' }) },
    });
    await flushPromises();
    expect(wrapper.find('.md-body').exists()).toBe(false);
  });
});
```

- [ ] **Step 2: 跑测试确认失败**

Run: `npx vitest run src/components/MessageBubble.blocks.test.ts`
Expected: FAIL — 找不到 `.md-block`

- [ ] **Step 3: 实现** — 修改 `MessageBubble.vue`

模板中把原来的：

```vue
<div
  v-if="msg.content && msg.content.trim()"
  ref="mdBodyRef"
  class="md-body"
  v-html="renderMarkdown(msg.content, mediaCtx)"
  @click="onMdBodyClick"
></div>
```

改为：

```vue
<div
  v-if="msg.content && msg.content.trim()"
  ref="mdBodyRef"
  class="md-body"
  @click="onMdBodyClick"
>
  <div v-for="b in mdBlocks" :key="b.key" class="md-block" v-html="b.html"></div>
</div>
```

script 中把 import 改为：

```ts
import { renderMarkdownDetailed } from '../utils/markdown';
import { splitStableBlocks } from '../utils/markdownBlocks';
```

（若 `renderMarkdown` 在本文件已无其他引用则一并移除。）

新增块渲染逻辑（放在 `mediaCtx` 定义之后）：

```ts
// 块级增量渲染：已完成块的 HTML 按「起始媒体序号 + 块源码」缓存，
// 流式期间只重渲染尾部块。缓存是组件级的，随组件销毁回收，不跨消息串味。
const blockCache = new Map<string, { html: string; mediaCount: number }>();

interface MdBlock {
  key: string;
  html: string;
}

const mdBlocks = computed<MdBlock[]>(() => {
  const src = props.msg.content || '';
  if (!src.trim()) return [];
  const { blocks, tail } = splitStableBlocks(src);
  const out: MdBlock[] = [];
  let ordinal = 0;
  for (const block of blocks) {
    const key = `${ordinal}|${block}`;
    let cached = blockCache.get(key);
    if (!cached) {
      const rendered = renderMarkdownDetailed(block, mediaCtx.value, ordinal);
      cached = { html: rendered.html, mediaCount: rendered.mediaCount };
      blockCache.set(key, cached);
    }
    ordinal += cached.mediaCount;
    out.push({ key, html: cached.html });
  }
  out.push({
    key: 'tail',
    html: renderMarkdownDetailed(tail, mediaCtx.value, ordinal).html,
  });
  return out;
});
```

- [ ] **Step 4: 跑测试确认通过**

Run: `npx vitest run src/components/MessageBubble.blocks.test.ts src/components/MessageBubble.test.ts`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add frontend/src/components/MessageBubble.vue frontend/src/components/MessageBubble.blocks.test.ts
git commit -m "perf(gui): 助手消息改为块级增量渲染，已完成块 DOM 复用"
```

---

### Task 4: 全量回归与类型检查

**Files:**
- 无新增，仅验证

- [ ] **Step 1: 全量前端测试**

Run: `npm test`
Expected: 全部 PASS，无新增失败

- [ ] **Step 2: 类型检查与构建**

Run: `npm run build`
Expected: 退出码 0，无 TS 报错

- [ ] **Step 3: 后端语法自检（确认未误改 Python）**

Run: `venv\Scripts\python -m py_compile main.py core\llm.py`
Expected: 退出码 0

- [ ] **Step 4: 提交（如有残留改动）**

```bash
git status --short
```

若无改动则跳过；有改动则按实际内容提交。

---

## Self-Review

- **Spec 覆盖**：切分器（Task 2）、详细版渲染与媒体序号（Task 1）、块数组渲染与 DOM 复用（Task 3）、黄金断言与回归（Task 2 Step 1 + Task 4）均有对应任务。
- **类型一致性**：`findCodeBlockRanges` / `renderMarkdownDetailed` / `splitStableBlocks` 的签名在 Task 1、2、3 中一致；`MdBlock` 的 `key`/`html` 字段一致。
- **无占位符**：每个代码步骤均给出可执行代码。

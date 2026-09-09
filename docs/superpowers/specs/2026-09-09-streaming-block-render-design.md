# 流式块级增量渲染 设计文档

**日期：** 2026-09-09
**状态：** 已确认，待实施
**背景：** 用户反馈 Y-code GUI 运行时 CPU 占用高。实测确认空闲占用为 0，瓶颈在流式输出期间：后端每 60ms 冲刷一次 delta，前端每次都全量重跑 `renderMarkdown` 并整体重建 `v-html` DOM，成本随文本长度线性增长（累计 O(n²)）。

## 目标

流式输出期间，已完成内容不再重复渲染，只重渲染「正在生长的最后一块」。保持流式期间排版、代码高亮、链接、mermaid 全程不降级。

## 非目标

- 不改后端 60ms 冲刷策略
- 不改 `downgradeInvalidFileLinks` / `renderMermaidBlocks` 的既有机制
- 不引入完整的 Markdown AST 解析器

## 关键前提（已验证）

1. `renderMarkdown` 的生产调用方只有 `MessageBubble.vue:71`，其余为测试；改动接口安全。
2. `renderMarkdown` 是逐行状态机，**空行会 `closeMode()` 关闭列表/引用块**。因此在空行处切分源码、分块渲染、再拼接，结果与整段渲染逐字节一致（可作为黄金断言）。
3. 现有 CSS 全部使用后代选择器（`.md-body p`），不存在 `.md-body >` 直接子选择器，插入一层块容器不破坏样式。
4. `mediaOrdinal` 在 `mediaCtx.items[ordinal]` 存在性判断**之前**自增，越界也消耗序号；跨块必须传递起始序号。

## 架构

```
msg.content
  └─ splitStableBlocks(source)         ← 新增，纯函数
       ├─ blocks: string[]             ← 稳定块（已完成）
       └─ tail: string                 ← 可变尾部
  ├─ blocks → renderMarkdownDetailed(block, ctx, startOrdinal)
  │            └─ 组件级 Map 缓存，key = `${startOrdinal}|${blockSource}`
  └─ tail   → 每次渲染，不进缓存
  └─ v-for + 稳定 key + v-html         ← 已完成块 DOM 复用
```

### 切分算法

- 用**与 `renderMarkdown` 相同的代码块正则**计算所有已闭合代码块的 `[start, end)` 范围，避免自写围栏扫描产生判定偏差。
- 收集所有**安全空行**（只含空白、且偏移不在任何代码块范围内）的位置。
- 取**倒数第二个**安全空行作为切分点：之前的源码按空行切成 `blocks`，之后的源码是 `tail`。
- 安全空行不足 2 个 → `blocks` 为空，全部进 `tail`（等价于现状，天然回退）。

「倒数第二个」而非「最后一个」的原因：正在生长的块（列表未结束、代码块未闭合）永远留在 tail，不会被误判为已完成。代价是稳定边界滞后一个块，视觉无感。

### 接口

```ts
// markdown.ts（新增导出，renderMarkdown 签名保持不变）
export function findCodeBlockRanges(source: string): Array<[number, number]>;
export function renderMarkdownDetailed(
  md: string,
  mediaCtx?: MediaContext,
  startMediaOrdinal?: number,
): { html: string; mediaCount: number };
export function renderMarkdown(md: string, mediaCtx?: MediaContext): string;

// markdownBlocks.ts（新增）
export function splitStableBlocks(source: string): { blocks: string[]; tail: string };
```

### DOM 结构

```vue
<div ref="mdBodyRef" class="md-body">
  <div v-for="b in mdBlocks" :key="b.key" class="md-block" v-html="b.html"></div>
</div>
```

## 验收标准

1. **黄金断言**：任意输入下，`blocks.map(b => render(b, ctx, ordinal)).join('') + render(tail, ctx, ordinal)` 与整段 `renderMarkdown(source, ctx)` 输出逐字节相等。
2. 现有 7 个 markdown 测试文件全绿。
3. 新增切分器测试（代码块内空行、仅一个空行、无空行、loose list、媒体跨块序号连续）。
4. `npm run build` 类型检查通过。
5. 流式期间已完成块的 DOM 不重建。

## 风险与回退

- 主要风险：切分器与渲染器判定不一致 → 用「同一正则 + 黄金断言」双保险。
- 回退：切分器返回 `{ blocks: [], tail: source }` 即等价于改造前行为。

// Mermaid 图表异步渲染编排。
//
// 安全约定（与 markdown.ts 同一威胁模型：mermaid 源码来自模型输出，按不可信
// 输入处理）：
// - markdown.ts 只产出占位容器 + 已转义的隐藏源码，本模块负责把源码交给
//   mermaid 渲染为 SVG；
// - mermaid 以 securityLevel: 'strict' 初始化（净化标签、禁用交互回调）；
// - SVG 注入 v-html 前再经本模块 sanitizeSvg 二次净化：剥离 on* 事件属性与
//   javascript: 链接——这是整个管线中唯一的 SVG innerHTML 注入点。
//
// 渲染时机与文件链接后置校验（downgradeInvalidFileLinks）同一模式：
// renderMarkdown 是纯同步函数，DOM 挂载后由 MessageBubble 的内容 watcher
// 调用 renderMermaidBlocks。SVG 按源码 key 缓存，跨 v-html 重建存活——
// 流式期间每个 delta 都整体重建 DOM，重建后凭 key 直接命中缓存零闪烁。

type MermaidApi = {
  initialize: (config: Record<string, unknown>) => void;
  render: (id: string, source: string) => Promise<{ svg: string }>;
};

let mermaidModulePromise: Promise<MermaidApi | null> | null = null;
let initialized = false;
let renderSeq = 0;

const svgCache = new Map<string, string>();
const MAX_CACHE_ENTRIES = 50;

function isDarkTheme(): boolean {
  // themes/runtime.ts 以 documentElement 的 'light' class 标记浅色主题；
  // 缺该 class（含暗色主题与判定失败）一律按暗色处理，宁可深底浅字
  const root = typeof document !== 'undefined' ? document.documentElement : null;
  return !(root && root.classList.contains('light'));
}

function loadMermaid(): Promise<MermaidApi | null> {
  if (!mermaidModulePromise) {
    // 动态 import：mermaid 体积大（约 1.4MB min），拆成独立 chunk 按需加载，
    // 首个 mermaid 块出现才拉取；加载/初始化失败返回 null，块降级显示源码
    mermaidModulePromise = import('mermaid')
      .then((mod) => {
        const mermaid = ((mod as Record<string, unknown>).default ?? mod) as MermaidApi;
        if (!initialized) {
          mermaid.initialize({
            startOnLoad: false,
            securityLevel: 'strict',
            theme: isDarkTheme() ? 'dark' : 'default',
          });
          initialized = true;
        }
        return mermaid;
      })
      .catch(() => null);
  }
  return mermaidModulePromise;
}

// SVG 二次净化：剥离所有 on* 事件属性与 javascript: 链接。mermaid strict
// 模式已做一次净化，这里是注入 v-html 前的兜底（防上游净化缺口）。
export function sanitizeSvg(svg: string): string {
  if (!svg || !svg.includes('<svg')) return '';
  try {
    // 必须用 text/html 解析而非 image/svg+xml：mermaid 对 htmlLabels 输出的
    // <br> 未自闭合（源码写 <br/> 也会被序列化成 <br>），XML 解析直接报
    // "Opening and ending tag mismatch" 把整张图判空，所有含换行标签的图
    // 全部降级失败。HTML 解析器对 <svg> 走外来内容规则保留命名空间，对
    // 未闭合标签有标准容错（DOMPurify 同款做法）。
    const doc = new DOMParser().parseFromString(svg, 'text/html');
    const root = doc.querySelector('svg');
    if (!root) return '';
    for (const el of [root, ...Array.from(root.querySelectorAll('*'))]) {
      for (const attr of Array.from(el.attributes)) {
        const name = attr.name.toLowerCase();
        if (name.startsWith('on')) {
          el.removeAttribute(attr.name);
        } else if (
          (name === 'href' || name === 'xlink:href')
          && /^\s*javascript:/i.test(attr.value)
        ) {
          el.removeAttribute(attr.name);
        }
      }
    }
    // root 来自 HTML 文档但位于 SVG 命名空间，XMLSerializer 会补全 xmlns
    return new XMLSerializer().serializeToString(root);
  } catch {
    return '';
  }
}

function injectSvg(block: HTMLElement, svg: string): void {
  block.setAttribute('data-state', 'done');
  // 幂等兜底：气泡重挂载等场景下同一块可能被并发 watcher 重复注入，
  // 先清掉旧 SVG 再挂新的，杜绝一卡双图
  block.querySelectorAll(':scope > .mermaid-svg').forEach((n) => n.remove());
  const holder = document.createElement('div');
  holder.className = 'mermaid-svg';
  holder.innerHTML = svg;
  block.appendChild(holder);
  const source = block.querySelector('pre.mermaid-src');
  if (source) source.setAttribute('hidden', '');
}

function markError(block: HTMLElement): void {
  block.setAttribute('data-state', 'error');
  // 错误态展示源码供用户复制自救（CSS 按状态控制显隐）
  const source = block.querySelector('pre.mermaid-src');
  if (source) source.removeAttribute('hidden');
}

function cachePut(key: string, svg: string): void {
  if (svgCache.size >= MAX_CACHE_ENTRIES) {
    const oldest = svgCache.keys().next().value;
    if (oldest !== undefined) svgCache.delete(oldest);
  }
  svgCache.set(key, svg);
}

export async function renderMermaidBlocks(root: HTMLElement | null): Promise<void> {
  if (!root) return;
  const blocks = Array.from(
    root.querySelectorAll<HTMLElement>('.mermaid-block[data-state="pending"]'),
  );
  if (!blocks.length) return;
  const mermaid = await loadMermaid();
  for (const block of blocks) {
    // 等待模块加载期间 DOM 可能已被下一次 delta 重建：以 isConnected 复核
    if (!block.isConnected) continue;
    const key = block.dataset.mermaidKey || '';
    const source = block.querySelector('pre.mermaid-src')?.textContent ?? '';
    if (!key || !source.trim()) {
      markError(block);
      continue;
    }
    const cached = svgCache.get(key);
    if (cached) {
      // 缓存路径必须复核状态：气泡 DOM 重建（设置页往返/会话切回/回合折叠切换）
      // 会带着已预热的缓存触发两次以上收集，若不复核，每次收集都注入一份 SVG，
      // 图就会重复两遍（渲染路径的 state==='rendering' 守卫只护得住未命中缓存的并发）。
      if (block.dataset.state === 'pending') {
        injectSvg(block, cached);
      }
      continue;
    }
    if (!mermaid) {
      markError(block);
      continue;
    }
    block.setAttribute('data-state', 'rendering');
    try {
      const { svg } = await mermaid.render(`mmd-${key}-${++renderSeq}`, source);
      const clean = sanitizeSvg(svg);
      if (!clean) throw new Error('empty svg');
      cachePut(key, clean);
      // 异步渲染期间消息可能继续流入、DOM 已重建：只在元素仍连接且仍处于
      // rendering 态时注入；重建产生的新占位块会凭缓存 key 再次命中
      if (block.isConnected && block.dataset.state === 'rendering') {
        injectSvg(block, clean);
      }
    } catch {
      if (block.isConnected) markError(block);
    }
  }
}

// 测试专用：清空模块级缓存与惰性加载状态（mermaidRenderer.test.ts 用）
export function __resetMermaidRendererForTest(): void {
  mermaidModulePromise = null;
  initialized = false;
  renderSeq = 0;
  svgCache.clear();
}

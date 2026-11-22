
type MermaidApi = {
  initialize: (config: Record<string, unknown>) => void;
  render: (id: string, source: string) => Promise<{ svg: string }>;
};

let mermaidModulePromise: Promise<MermaidApi | null> | null = null;
let initialized = false;
let renderSeq = 0;
const LOAD_RETRY_COOLDOWN_MS = 5000;
let loadFailedAt = 0;
let mermaidImporter: () => Promise<unknown> = () => import('mermaid');

const svgCache = new Map<string, string>();
const MAX_CACHE_ENTRIES = 50;

function isDarkTheme(): boolean {
  const root = typeof document !== 'undefined' ? document.documentElement : null;
  return !(root && root.classList.contains('light'));
}

function loadMermaid(): Promise<MermaidApi | null> {
  if (!mermaidModulePromise) {
    if (loadFailedAt && Date.now() - loadFailedAt < LOAD_RETRY_COOLDOWN_MS) {
      return Promise.resolve(null);
    }
    // 动态 import：mermaid 体积大（约 1.4MB min），拆成独立 chunk 按需加载，
    // 首个 mermaid 块出现才拉取；加载/初始化失败返回 null，块降级显示源码
    mermaidModulePromise = mermaidImporter()
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
      .catch(() => {
        mermaidModulePromise = null;
        loadFailedAt = Date.now();
        return null;
      });
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
    return new XMLSerializer().serializeToString(root);
  } catch {
    return '';
  }
}

// 节点标签自动修复：模型输出常把 [DOC]、[VALUES] 这类方括号（以及裸圆括号）
// 直接写进标签文本（如 E[说明 [DOC] 关键词]），mermaid 的 jison 解析器把
// [ ] 当节点定界符直接 Parse error；官方解法是整段标签用双引号包裹
// （E["说明 [DOC] 关键词"]）。本函数只在首次渲染失败后作为兜底重试调用——
// 最坏结果 = 修复后仍失败、维持原降级显示，绝不影响本来合法的源码。
// 用逐字符扫描而非正则：嵌套方括号（G[[VALUES]...]）的闭合边界正则识别不了。
export function autofixMermaidSource(source: string): string {
  const SPECIAL_CHARS = /[[\](){}]/;
  const lines = source.split('\n');
  let changed = false;
  const fixed = lines.map((line) => {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith('%%')) return line;
    let out = '';
    let i = 0;
    let lineChanged = false;
    while (i < line.length) {
      const ch = line[i];
      if (ch === '[' && /[A-Za-z0-9_]/.test(out[out.length - 1] ?? '')) {
        if (line[i + 1] === '"') {
          let j = i + 2;
          while (j < line.length && line[j] !== '"') j++;
          const end = Math.min(j + 1, line.length);
          out += line.slice(i, end);
          i = end;
          continue;
        }
        // 扫描标签内容找真实闭合位置：内层 '[' 加深、']' 回退，深度归零才是
        // 该标签的边界（G[[VALUES]表.字段 查码值] 这类错乱写法靠它识别）
        let depth = 1;
        let j = i + 1;
        let hasQuote = false;
        while (j < line.length && depth > 0) {
          const c = line[j];
          if (c === '[') depth++;
          else if (c === ']') depth--;
          else if (c === '"') hasQuote = true;
          j++;
        }
        if (depth !== 0) {
          out += line.slice(i);
          i = line.length;
          continue;
        }
        const inner = line.slice(i + 1, j - 1);
        if (!hasQuote && SPECIAL_CHARS.test(inner)) {
          out += `["${inner}"]`;
          lineChanged = true;
        } else {
          out += line.slice(i, j);
        }
        i = j;
        continue;
      }
      out += ch;
      i++;
    }
    if (lineChanged) changed = true;
    return lineChanged ? out : line;
  });
  return changed ? fixed.join('\n') : source;
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

function markError(block: HTMLElement, reason = ''): void {
  block.setAttribute('data-state', 'error');
  const detail = String(reason || '').split('\n').filter(Boolean)[0]?.slice(0, 120) ?? '';
  block.setAttribute('data-error-reason', detail ? `原因：${detail} ` : '');
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
    if (!block.isConnected) continue;
    const key = block.dataset.mermaidKey || '';
    const source = block.querySelector('pre.mermaid-src')?.textContent ?? '';
    if (!key || !source.trim()) {
      markError(block, '源码为空');
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
      markError(block, '渲染器加载失败（可刷新界面恢复）');
      continue;
    }
    block.setAttribute('data-state', 'rendering');
    try {
      const { svg } = await mermaid.render(`mmd-${key}-${++renderSeq}`, source);
      const clean = sanitizeSvg(svg);
      if (!clean) throw new Error('empty svg');
      cachePut(key, clean);
      if (block.isConnected && block.dataset.state === 'rendering') {
        injectSvg(block, clean);
      }
    } catch (err) {
      // 失败兜底：模型输出的常见语法瑕疵（标签内裸写 [ ] ( ) 等特殊字符）
      // 做一次自动修复重试；成功则正常出图并写缓存，失败才走原降级。
      // 只在失败路径触发，合法源码的渲染行为完全不变。
      const fixed = autofixMermaidSource(source);
      let recovered = false;
      if (fixed !== source) {
        try {
          const { svg } = await mermaid.render(`mmd-${key}-${++renderSeq}`, fixed);
          const clean = sanitizeSvg(svg);
          if (clean) {
            cachePut(key, clean);
            if (block.isConnected && block.dataset.state === 'rendering') {
              injectSvg(block, clean);
            }
            recovered = true;
          }
        } catch {
          // 修复后仍失败：落入原降级，透出首次报错便于定位
        }
      }
      if (!recovered && block.isConnected) {
        markError(block, err instanceof Error ? err.message : String(err));
      }
    }
  }
}

export function __resetMermaidRendererForTest(): void {
  mermaidModulePromise = null;
  initialized = false;
  renderSeq = 0;
  loadFailedAt = 0;
  mermaidImporter = () => import('mermaid');
  svgCache.clear();
}

export function __setMermaidImporterForTest(
  importer: (() => Promise<unknown>) | null,
): void {
  mermaidImporter = importer ?? (() => import('mermaid'));
}

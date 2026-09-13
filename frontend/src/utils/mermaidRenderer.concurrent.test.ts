import { describe, it, expect, vi, beforeEach } from 'vitest';
import {
  renderMermaidBlocks,
  __resetMermaidRendererForTest,
} from './mermaidRenderer';

// 并发时序回归：模拟流式期间 watcher 反复触发 renderMermaidBlocks，
// 用可控的 render 延迟构造乱序完成，验证同一块绝不会被注入两份 SVG。
const initialize = vi.fn();
const render = vi.fn();

vi.mock('mermaid', () => ({
  default: { initialize, render },
}));

const SVG_OK =
  '<svg xmlns="http://www.w3.org/2000/svg" width="100"><text>hi</text></svg>';

function makeBlock(key: string, source: string): HTMLElement {
  const block = document.createElement('div');
  block.className = 'mermaid-block';
  block.setAttribute('data-mermaid-key', key);
  block.setAttribute('data-state', 'pending');
  const pre = document.createElement('pre');
  pre.className = 'mermaid-src';
  pre.textContent = source;
  block.appendChild(pre);
  return block;
}

// 手动Flush微任务的辅助
const flushMicro = () => new Promise<void>(r => setTimeout(r, 0));

describe('renderMermaidBlocks 并发时序', () => {
  beforeEach(() => {
    __resetMermaidRendererForTest();
    initialize.mockReset();
    render.mockReset();
    document.body.innerHTML = '';
  });

  it('两个并发调用同时收集同一 pending 块：只注入一份 SVG', async () => {
    const root = document.createElement('div');
    const block = makeBlock('k1', 'graph TD\nA-->B');
    root.appendChild(block);
    document.body.appendChild(root);

    // render 慢：两个调用都会在渲染完成前收集到同一块
    const resolvers: Array<(v: { svg: string }) => void> = [];
    render.mockImplementation(
      () => new Promise<{ svg: string }>(r => resolvers.push(r)),
    );

    const p1 = renderMermaidBlocks(root);
    const p2 = renderMermaidBlocks(root);
    await flushMicro();
    expect(render).toHaveBeenCalledTimes(2); // 双双进入渲染路径

    resolvers[0]({ svg: SVG_OK });
    await flushMicro();
    expect(block.querySelectorAll('.mermaid-svg')).toHaveLength(1);

    resolvers[1]({ svg: SVG_OK });
    await Promise.all([p1, p2]);

    expect(block.querySelectorAll('.mermaid-svg')).toHaveLength(1);
    expect(block.getAttribute('data-state')).toBe('done');
  });

  it('后启动的调用在块 rendering 后查询：不收集、不重复渲染', async () => {
    const root = document.createElement('div');
    const block = makeBlock('k2', 'graph TD\nA-->B');
    root.appendChild(block);
    document.body.appendChild(root);

    const resolvers: Array<(v: { svg: string }) => void> = [];
    render.mockImplementation(
      () => new Promise<{ svg: string }>(r => resolvers.push(r)),
    );

    const p1 = renderMermaidBlocks(root);
    await flushMicro();
    expect(render).toHaveBeenCalledTimes(1);

    const p2 = renderMermaidBlocks(root); // 块已 rendering：第二次查询收集不到
    await flushMicro();
    expect(render).toHaveBeenCalledTimes(1);

    resolvers[0]({ svg: SVG_OK });
    await Promise.all([p1, p2]);
    expect(block.querySelectorAll('.mermaid-svg')).toHaveLength(1);
  });

  it('收集发生在 pending、迭代发生在缓存写入之后（模块加载延迟窗口）：只注入一份', async () => {
    const root = document.createElement('div');
    const block = makeBlock('k3', 'graph TD\nA-->B');
    root.appendChild(block);
    document.body.appendChild(root);

    // 模拟首次模块加载慢：让调用A在 loadMermaid 处挂起，
    // 期间调用B完成整轮渲染并写缓存；A 恢复后必须因缓存/状态防护不再注入
    let resolveRender!: (v: { svg: string }) => void;
    render.mockImplementation(() => new Promise<{ svg: string }>(r => { resolveRender = r; }));

    const p1 = renderMermaidBlocks(root);
    await flushMicro();
    const p2 = renderMermaidBlocks(root);
    await flushMicro();

    resolveRender({ svg: SVG_OK });
    await Promise.all([p1, p2]);
    expect(block.querySelectorAll('.mermaid-svg')).toHaveLength(1);

    // 缓存已写入：新调用/重建块走缓存路径，依旧单份
    const p3 = renderMermaidBlocks(root); // 块已 done，不应再被收集
    await p3;
    expect(block.querySelectorAll('.mermaid-svg')).toHaveLength(1);
  });

  it('缓存已预热 + DOM 重建后两次并发收集（设置页往返重挂载形态）：只注入一份', async () => {
    // 预热缓存：先完成一次真实渲染
    render.mockResolvedValue({ svg: SVG_OK });
    const warmRoot = document.createElement('div');
    const warm = makeBlock('k-warm', 'graph TD\nA-->B');
    warmRoot.appendChild(warm);
    document.body.appendChild(warmRoot);
    await renderMermaidBlocks(warmRoot);
    expect(warm.querySelectorAll('.mermaid-svg')).toHaveLength(1);

    // 模拟设置页往返：缓存仍在（模块级），气泡 DOM 全新挂载，
    // 同一 pending 块被两个 watcher 回调先后收集，随后同批恢复
    const root = document.createElement('div');
    const block = makeBlock('k-warm', 'graph TD\nA-->B');
    root.appendChild(block);
    document.body.appendChild(root);

    const p1 = renderMermaidBlocks(root);
    const p2 = renderMermaidBlocks(root);
    await Promise.all([p1, p2]);

    expect(render).toHaveBeenCalledTimes(1); // 全程只预热那一次，后续全走缓存
    expect(block.getAttribute('data-state')).toBe('done');
    expect(block.querySelectorAll('.mermaid-svg')).toHaveLength(1);
  });
});

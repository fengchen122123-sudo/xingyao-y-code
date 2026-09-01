import { describe, it, expect, vi, beforeEach } from 'vitest';
import {
  renderMermaidBlocks,
  sanitizeSvg,
  svgToDataUrl,
  __resetMermaidRendererForTest,
} from './mermaidRenderer';

// mock mermaid 模块：jsdom 无法真实渲染 SVG（需要真实布局测量），
// 这里只验证编排逻辑——占位块状态流转、缓存命中、失败降级、SVG 净化。
const initialize = vi.fn();
const render = vi.fn();

vi.mock('mermaid', () => ({
  default: { initialize, render },
}));

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

// 注意：xlink:href 必须带命名空间声明——XML 解析下未声明前缀是非法的
// （真实 mermaid 输出自带 xmlns 声明，无此问题；这里模拟的是合法输入+恶意属性）
const SVG_OK =
  '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" width="100">'
  + '<g onclick="evil()"><a xlink:href="javascript:alert(1)"><text>hi</text></a></g></svg>';

describe('renderMermaidBlocks', () => {
  beforeEach(() => {
    __resetMermaidRendererForTest();
    initialize.mockReset();
    render.mockReset();
    document.body.innerHTML = '';
  });

  it('渲染成功：SVG 注入、状态置 done、源码隐藏', async () => {
    render.mockResolvedValue({ svg: SVG_OK });
    const root = document.createElement('div');
    const block = makeBlock('k1', 'graph TD\nA-->B');
    root.appendChild(block);
    document.body.appendChild(root);

    await renderMermaidBlocks(root);

    expect(initialize).toHaveBeenCalled();
    expect(block.getAttribute('data-state')).toBe('done');
    expect(block.querySelector('.mermaid-svg')).not.toBeNull();
    // 注入的 SVG 必须经过净化：onclick 与 javascript: 链接被剥离
    const svg = block.querySelector('.mermaid-svg svg')!;
    expect(svg.hasAttribute('onclick')).toBe(false);
    expect(svg.querySelector('a')!.getAttribute('xlink:href')).toBeNull();
    expect(block.querySelector('pre.mermaid-src')!.hasAttribute('hidden')).toBe(true);
  });

  it('缓存命中：同 key 第二个块直接注入，不再调用 mermaid.render', async () => {
    render.mockResolvedValue({ svg: SVG_OK });
    const root = document.createElement('div');
    document.body.appendChild(root);
    const first = makeBlock('same', 'graph TD\nA-->B');
    root.appendChild(first);
    await renderMermaidBlocks(root);
    expect(render).toHaveBeenCalledTimes(1);

    // 模拟 v-html 重建：全新 DOM 节点 + 相同 key
    const root2 = document.createElement('div');
    document.body.appendChild(root2);
    const second = makeBlock('same', 'graph TD\nA-->B');
    root2.appendChild(second);
    await renderMermaidBlocks(root2);

    expect(render).toHaveBeenCalledTimes(1);
    expect(second.getAttribute('data-state')).toBe('done');
    expect(second.querySelector('.mermaid-svg')).not.toBeNull();
  });

  it('渲染失败：状态置 error、源码重新展示，不抛出异常', async () => {
    render.mockRejectedValue(new Error('Syntax error in text'));
    const root = document.createElement('div');
    const block = makeBlock('k-err', 'graph TD\nA-->B broken {{{');
    root.appendChild(block);
    document.body.appendChild(root);

    await expect(renderMermaidBlocks(root)).resolves.toBeUndefined();

    expect(block.getAttribute('data-state')).toBe('error');
    expect(block.querySelector('.mermaid-svg')).toBeNull();
    expect(block.querySelector('pre.mermaid-src')!.hasAttribute('hidden')).toBe(false);
  });

  it('异步渲染期间 DOM 被重建（元素失联）：不注入、不报错，缓存仍写入', async () => {
    const root = document.createElement('div');
    const block = makeBlock('k-race', 'graph TD\nA-->B');
    root.appendChild(block);
    document.body.appendChild(root);

    // 渲染进行中（编排已把状态置为 rendering）元素被 v-html 重建移除
    render.mockImplementation(async () => {
      block.remove();
      return { svg: SVG_OK };
    });
    await renderMermaidBlocks(root);

    expect(block.getAttribute('data-state')).toBe('rendering');
    expect(block.querySelector('.mermaid-svg')).toBeNull();

    // 重建后的新块凭缓存 key 直接命中
    const root2 = document.createElement('div');
    document.body.appendChild(root2);
    const rebuilt = makeBlock('k-race', 'graph TD\nA-->B');
    root2.appendChild(rebuilt);
    await renderMermaidBlocks(root2);
    expect(rebuilt.getAttribute('data-state')).toBe('done');
    expect(rebuilt.querySelector('.mermaid-svg')).not.toBeNull();
  });

  it('空 key 或空源码：直接置 error 态', async () => {
    const root = document.createElement('div');
    document.body.appendChild(root);
    const noKey = makeBlock('', 'graph TD\nA-->B');
    const emptySrc = makeBlock('k-empty', '');
    root.appendChild(noKey);
    root.appendChild(emptySrc);
    await renderMermaidBlocks(root);
    expect(noKey.getAttribute('data-state')).toBe('error');
    expect(emptySrc.getAttribute('data-state')).toBe('error');
    expect(render).not.toHaveBeenCalled();
  });
});

describe('sanitizeSvg', () => {
  it('剥离 on* 事件属性与 javascript: 链接', () => {
    const dirty =
      '<svg onload="alert(1)"><g onclick="x()"><a href="javascript:alert(2)">t</a></g></svg>';
    const clean = sanitizeSvg(dirty);
    expect(clean).not.toContain('onload');
    expect(clean).not.toContain('onclick');
    expect(clean).not.toContain('javascript:');
  });

  it('正常 SVG 原样通过', () => {
    const svg = '<svg width="10"><text>流程图</text></svg>';
    expect(sanitizeSvg(svg)).toContain('<text>流程图</text>');
  });

  it('mermaid 真实输出形态：未自闭合 <br> 不再被判空（htmlLabels 换行回归）', () => {
    // 真实事故：源码写 <br/>，mermaid 输出序列化为未自闭合的 <br>，
    // 旧版 XML 解析报 "Opening and ending tag mismatch" 把整张图判空
    const realShape =
      '<svg xmlns="http://www.w3.org/2000/svg" width="100">'
      + '<foreignObject><div><p>定位根因后修复<br>下一行</p></div></foreignObject>'
      + '</svg>';
    const clean = sanitizeSvg(realShape);
    expect(clean).not.toBe('');
    expect(clean).toContain('foreignObject');
    // XMLSerializer 会把未闭合 <br> 规范化为自闭合 <br />，渲染效果等价
    expect(clean).toMatch(/<br[\s/>]/i);
  });

  it('解析失败或非 SVG 输入返回空串（走 error 降级）', () => {
    expect(sanitizeSvg('')).toBe('');
    expect(sanitizeSvg('not an svg')).toBe('');
  });
});

describe('svgToDataUrl', () => {
  it('输出 svg+xml data URL 前缀', () => {
    expect(svgToDataUrl('<svg></svg>')).toMatch(/^data:image\/svg\+xml;charset=utf-8,/);
  });

  it('特殊字符经 encodeURIComponent 编码（引号、尖括号、空白）', () => {
    const url = svgToDataUrl('<svg width="10"><text>a b</text></svg>');
    expect(url).not.toContain('<');
    expect(url).toContain(encodeURIComponent('<svg width="10">'));
  });

  it('含中文标签不抛异常且可解码还原（base64/btoa 的 Latin-1 陷阱回归）', () => {
    const svg = '<svg><text>流程图节点</text></svg>';
    const url = svgToDataUrl(svg);
    expect(decodeURIComponent(url.slice('data:image/svg+xml;charset=utf-8,'.length))).toBe(svg);
  });
});

import { describe, it, expect, vi, beforeEach } from 'vitest';
import {
  renderMermaidBlocks,
  sanitizeSvg,
  autofixMermaidSource,
  __resetMermaidRendererForTest,
  __setMermaidImporterForTest,
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

  it('渲染失败：error 态带 mermaid 原始报错，便于定位语法问题', async () => {
    render.mockRejectedValue(new Error('Parse error on line 2: A[foo(bar)]'));
    const root = document.createElement('div');
    const block = makeBlock('k-reason', 'graph TD\nA[foo(bar)]');
    root.appendChild(block);
    document.body.appendChild(root);
    await renderMermaidBlocks(root);
    expect(block.getAttribute('data-state')).toBe('error');
    expect(block.getAttribute('data-error-reason')).toContain('Parse error on line 2');
  });

  it('渲染失败后自动修复重试：标签内裸方括号源码成功出图', async () => {
    const broken = 'flowchart LR\n  B --> E[说明 [DOC] 关键词]';
    render
      .mockRejectedValueOnce(new Error('Parse error on line 2'))
      .mockResolvedValueOnce({ svg: SVG_OK });
    const root = document.createElement('div');
    const block = makeBlock('k-autofix', broken);
    root.appendChild(block);
    document.body.appendChild(root);

    await renderMermaidBlocks(root);

    expect(render).toHaveBeenCalledTimes(2);
    // 第二次渲染使用修复后的源码：标签整体双引号包裹
    expect(render.mock.calls[1][1]).toBe('flowchart LR\n  B --> E["说明 [DOC] 关键词"]');
    expect(block.getAttribute('data-state')).toBe('done');
    expect(block.querySelector('.mermaid-svg')).not.toBeNull();
    expect(block.querySelector('pre.mermaid-src')!.hasAttribute('hidden')).toBe(true);
  });

  it('自动修复后仍失败：维持原 error 降级，透出首次报错', async () => {
    render.mockRejectedValue(new Error('Parse error on line 2'));
    const root = document.createElement('div');
    const block = makeBlock('k-autofix-fail', 'flowchart LR\n  A[x [DOC] y]');
    root.appendChild(block);
    document.body.appendChild(root);

    await renderMermaidBlocks(root);

    expect(render).toHaveBeenCalledTimes(2);
    expect(block.getAttribute('data-state')).toBe('error');
    expect(block.getAttribute('data-error-reason')).toContain('Parse error');
    expect(block.querySelector('pre.mermaid-src')!.hasAttribute('hidden')).toBe(false);
  });

  it('无法自动修复的源码：不发起第二次渲染，直接走原降级', async () => {
    render.mockRejectedValue(new Error('Syntax error in text'));
    const root = document.createElement('div');
    const block = makeBlock('k-no-autofix', 'graph TD\nA-->B broken {{{');
    root.appendChild(block);
    document.body.appendChild(root);

    await renderMermaidBlocks(root);

    expect(render).toHaveBeenCalledTimes(1);
    expect(block.getAttribute('data-state')).toBe('error');
  });

  it('加载失败不永久降级：冷却期过后重新加载，成功后正常渲染', async () => {
    vi.useFakeTimers();
    try {
      let calls = 0;
      __setMermaidImporterForTest(() => {
        calls++;
        if (calls === 1) return Promise.reject(new Error('chunk load failed'));
        return Promise.resolve({ default: { initialize, render } });
      });
      render.mockResolvedValue({ svg: SVG_OK });

      const root = document.createElement('div');
      document.body.appendChild(root);

      const first = makeBlock('k-load-1', 'graph TD\nA-->B');
      root.appendChild(first);
      await renderMermaidBlocks(root);
      expect(first.getAttribute('data-state')).toBe('error');
      expect(first.getAttribute('data-error-reason')).toContain('渲染器加载失败');
      expect(calls).toBe(1);

      const second = makeBlock('k-load-2', 'graph TD\nA-->B');
      root.appendChild(second);
      await renderMermaidBlocks(root);
      expect(calls).toBe(1);

      vi.advanceTimersByTime(5000);
      const third = makeBlock('k-load-3', 'graph TD\nA-->B');
      root.appendChild(third);
      await renderMermaidBlocks(root);
      expect(calls).toBe(2);
      expect(third.getAttribute('data-state')).toBe('done');
    } finally {
      vi.useRealTimers();
    }
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

describe('autofixMermaidSource', () => {
  it('标签内裸方括号：整段标签双引号包裹', () => {
    const src = 'flowchart LR\n  B --> E[模型知道能查什么<br/> [DOC]关键词 调全文]';
    expect(autofixMermaidSource(src)).toBe(
      'flowchart LR\n  B --> E["模型知道能查什么<br/> [DOC]关键词 调全文"]',
    );
  });

  it('双左括号开头的错乱标签（G[[VALUES]...]）：内层到行尾整体包裹', () => {
    const src = 'flowchart LR\n  F --> G[[VALUES]表.字段 查码值]';
    expect(autofixMermaidSource(src)).toBe('flowchart LR\n  F --> G["[VALUES]表.字段 查码值"]');
  });

  it('标签内裸圆括号：同样包裹（mermaid 对裸 () 也 Parse error）', () => {
    const src = 'graph TD\n  A[foo(bar)] --> B';
    expect(autofixMermaidSource(src)).toBe('graph TD\n  A["foo(bar)"] --> B');
  });

  it('纯文字标签、已引号包裹标签不动', () => {
    const src =
      'flowchart LR\n  A[每次对话携带] --> B["领域知识目录<br/>9个领域的索引"]\n  A --> C["含 [DOC] 已包裹"]';
    expect(autofixMermaidSource(src)).toBe(src);
  });

  it('subgraph 引号标题不动', () => {
    const src = 'flowchart TD\n  subgraph S1["标题 (含括号)"]\n    A --> B\n  end';
    expect(autofixMermaidSource(src)).toBe(src);
  });

  it('注释行与空行不动', () => {
    const src = 'flowchart LR\n  %% 注释里的 A[x [DOC] y] 不是节点\n\n  A --> B';
    expect(autofixMermaidSource(src)).toBe(src);
  });

  it('标签内容含双引号或括号未闭合：放弃修复保持原样', () => {
    expect(autofixMermaidSource('flowchart LR\n  A[含"引号 [DOC] 混合]')).toBe(
      'flowchart LR\n  A[含"引号 [DOC] 混合]',
    );
    expect(autofixMermaidSource('flowchart LR\n  A[没闭合 [DOC]')).toBe(
      'flowchart LR\n  A[没闭合 [DOC]',
    );
  });

  it('同一行多个节点标签分别处理', () => {
    const src = 'flowchart LR\n  A[左 [X] 括] --> B[正常] --> C[右 [Y] 括]';
    expect(autofixMermaidSource(src)).toBe(
      'flowchart LR\n  A["左 [X] 括"] --> B[正常] --> C["右 [Y] 括"]',
    );
  });
});

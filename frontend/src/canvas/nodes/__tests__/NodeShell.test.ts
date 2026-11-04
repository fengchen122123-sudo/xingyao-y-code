// M3-T01 节点展示态单测：NodeShell 四态（title/选中/loading/error）+ 八类节点关键内容
// + 类型→组件映射表与默认尺寸，最后验 CanvasPane 真的把 project.nodes 渲染成 NodeShell。
//
// 环境：jsdom。节点组件不 import store，所以全部可以独立 mount；
// CanvasPane 用例把 canvas/bridge 整体 mock 掉，绝不触碰 window.pywebview。
import { createPinia, setActivePinia } from 'pinia';
import { describe, expect, it, vi } from 'vitest';
import { mount } from '@vue/test-utils';

import CanvasPane from '../../CanvasPane.vue';
import { useCanvasStore } from '../../../stores/canvas';
import type { CanvasNodeData, CanvasProject } from '../../types';
import AudioNode from '../AudioNode.vue';
import ConfigNode from '../ConfigNode.vue';
import DirectorNode from '../DirectorNode.vue';
import GroupNode from '../GroupNode.vue';
import ImageNode from '../ImageNode.vue';
import NodeShell from '../NodeShell.vue';
import PanoramaNode from '../PanoramaNode.vue';
import TextNode from '../TextNode.vue';
import VideoNode from '../VideoNode.vue';
import { NODE_COMPONENTS, NODE_DEFAULT_SIZE, getNodeComponent, getNodeDefaultSize } from '../index';

vi.mock('../../bridge', () => ({
  canvasBridge: {
    listProjects: vi.fn(async () => ({ ok: true, projects: [] })),
    loadProject: vi.fn(async () => ({ ok: false, msg: '旧版数据，不支持' })),
    saveProject: vi.fn(async () => ({ ok: true, updatedAt: 1700000000000 })),
    deleteProject: vi.fn(async () => ({ ok: true })),
  },
}));

const ALL_TYPES = ['image', 'panorama', 'text', 'config', 'video', 'audio', 'director', 'group'] as const;

function makeNode(overrides: Partial<CanvasNodeData> = {}): CanvasNodeData {
  return {
    id: 'n-1',
    type: 'image',
    title: '图片节点',
    position: { x: 0, y: 0 },
    width: 340,
    height: 240,
    metadata: {},
    ...overrides,
  };
}

function mountShell(
  node: CanvasNodeData = makeNode(),
  options: { selected?: boolean; interactive?: boolean } = {},
) {
  return mount(NodeShell, {
    props: {
      node,
      selected: options.selected ?? false,
      interactive: options.interactive ?? true,
    },
    slots: { default: '<div data-test="node-shell-slot">节点内容</div>' },
  });
}

describe('NodeShell 外壳', () => {
  it('渲染标题与默认插槽，根节点带 data-node-id（画布靠它躲开平移手势）', () => {
    const wrapper = mountShell();

    expect(wrapper.attributes('data-node-id')).toBe('n-1');
    expect(wrapper.attributes('data-node-type')).toBe('image');
    expect(wrapper.find('[data-test="node-shell-title"]').text()).toBe('图片节点');
    expect(wrapper.find('[data-test="node-shell-slot"]').text()).toBe('节点内容');
  });

  it('标题为空时按类型给兜底名（八类各有默认标题）', () => {
    const wrapper = mountShell(makeNode({ type: 'text', title: '' }));
    expect(wrapper.find('[data-test="node-shell-title"]').text()).toBe('未命名文本');

    const group = mountShell(makeNode({ type: 'group', title: '' }));
    expect(group.find('[data-test="node-shell-title"]').text()).toBe('分组');
  });

  it('选中态：data-selected 与 is-selected 描边类同步', () => {
    const selected = mountShell(makeNode(), { selected: true });
    expect(selected.attributes('data-selected')).toBe('true');
    expect(selected.classes()).toContain('is-selected');

    const idle = mountShell();
    expect(idle.attributes('data-selected')).toBe('false');
    expect(idle.classes()).not.toContain('is-selected');
  });

  it('八向缩放手柄与左右连接端口都渲染出来（本任务只画控件）', () => {
    const wrapper = mountShell();

    const handles = wrapper.findAll('[data-test="node-resize-handle"]');
    expect(handles).toHaveLength(8);
    expect(handles.map((h) => h.attributes('data-handle'))).toEqual([
      'top-left', 'top', 'top-right', 'right',
      'bottom-right', 'bottom', 'bottom-left', 'left',
    ]);

    const ports = wrapper.findAll('[data-test="node-port"]');
    expect(ports).toHaveLength(2);
    expect(ports.map((p) => p.attributes('data-side'))).toEqual(['left', 'right']);
  });

  it('与参考仓一致：分组只有右侧端口，生成配置没有右侧 source 端口', () => {
    const group = mountShell(makeNode({ type: 'group', title: '分组' }));
    expect(group.findAll('[data-test="node-port"]').map((p) => p.attributes('data-side'))).toEqual(['right']);

    const config = mountShell(makeNode({ type: 'config', title: '生成配置' }));
    expect(config.findAll('[data-test="node-port"]').map((p) => p.attributes('data-side'))).toEqual(['left']);
  });

  it('loading 态铺遮罩并显示进度（metadata.status === loading）', () => {
    const wrapper = mountShell(makeNode({ metadata: { status: 'loading', progress: 42 } }));

    expect(wrapper.classes()).toContain('is-loading');
    const overlay = wrapper.find('[data-test="node-loading"]');
    expect(overlay.exists()).toBe(true);
    expect(overlay.text()).toContain('生成中 42%');

    // 非 loading 不渲染遮罩
    expect(mountShell().find('[data-test="node-loading"]').exists()).toBe(false);
  });

  it('loading 无进度时给「生成中…」，不编造百分比', () => {
    const wrapper = mountShell(makeNode({ metadata: { status: 'loading' } }));
    expect(wrapper.find('[data-test="node-loading"]').text()).toContain('生成中…');
  });

  it('error 态红边 + errorDetails 提示，无详情时兜底「生成失败」', () => {
    const wrapper = mountShell(makeNode({ metadata: { status: 'error', errorDetails: '模型超时，请重试' } }));

    expect(wrapper.classes()).toContain('is-error');
    const badge = wrapper.find('[data-test="node-error"]');
    expect(badge.text()).toContain('模型超时，请重试');
    expect(badge.attributes('title')).toBe('模型超时，请重试');

    const bare = mountShell(makeNode({ metadata: { status: 'error' } }));
    expect(bare.find('[data-test="node-error"]').text()).toBe('生成失败');
  });

  it('双击标题进入输入框，回车 emit action update-title', async () => {
    const wrapper = mountShell();

    await wrapper.find('[data-test="node-shell-title"]').trigger('dblclick');
    const input = wrapper.find('[data-test="node-shell-title-input"]');
    expect(input.exists()).toBe(true);

    await input.setValue('  新标题  ');
    await input.trigger('keydown', { key: 'Enter' });

    expect(wrapper.emitted('action')?.[0]?.[0]).toEqual({
      nodeId: 'n-1',
      action: 'update-title',
      args: { title: '新标题' },
    });
    expect(wrapper.find('[data-test="node-shell-title-input"]').exists()).toBe(false);
  });

  it('interactive=false 时不进编辑态、手柄与端口按 is-static 隐藏', async () => {
    const wrapper = mountShell(makeNode(), { interactive: false });

    expect(wrapper.classes()).toContain('is-static');
    await wrapper.find('[data-test="node-shell-title"]').trigger('dblclick');
    expect(wrapper.find('[data-test="node-shell-title-input"]').exists()).toBe(false);
  });
});

describe('八类节点内容', () => {
  it('ImageNode：有 metadata.content 出 img，无内容出占位', () => {
    const withImage = mount(ImageNode, { props: { node: makeNode({ metadata: { content: '/media/a.png' } }) } });
    expect(withImage.find('[data-test="node-image-el"]').attributes('src')).toBe('/media/a.png');

    const empty = mount(ImageNode, { props: { node: makeNode() } });
    expect(empty.find('[data-test="node-image-el"]').exists()).toBe(false);
    expect(empty.find('[data-test="node-content-placeholder"]').text()).toContain('空图片节点');
  });

  it('PanoramaNode：同样展示 media URL，占位文案区分全景', () => {
    const node = makeNode({ type: 'panorama', title: '全景图', metadata: { content: '/media/pano.jpg' } });
    const withImage = mount(PanoramaNode, { props: { node } });
    expect(withImage.find('[data-test="node-panorama-el"]').attributes('src')).toBe('/media/pano.jpg');
    expect(withImage.text()).toContain('全景 2:1');

    const empty = mount(PanoramaNode, { props: { node: makeNode({ type: 'panorama' }) } });
    expect(empty.find('[data-test="node-content-placeholder"]').text()).toContain('空全景图节点');
  });

  it('TextNode：展示文本，双击进 textarea，失焦 emit action update-text', async () => {
    const node = makeNode({ type: 'text', title: '文本', metadata: { content: '第一行' } });
    const wrapper = mount(TextNode, { props: { node } });
    expect(wrapper.find('[data-test="node-text-body"]').text()).toContain('第一行');

    await wrapper.find('[data-test="node-content-text"]').trigger('dblclick');
    const textarea = wrapper.find('[data-test="node-text-textarea"]');
    expect(textarea.exists()).toBe(true);
    expect((textarea.element as HTMLTextAreaElement).value).toBe('第一行');

    await textarea.setValue('改过的文本');
    await textarea.trigger('blur');

    expect(wrapper.emitted('action')?.[0]?.[0]).toEqual({
      nodeId: 'n-1',
      action: 'update-text',
      args: { content: '改过的文本' },
    });
    expect(wrapper.find('[data-test="node-text-textarea"]').exists()).toBe(false);
  });

  it('TextNode：内容没改就失焦不发事件（避免无意义落盘）', async () => {
    const node = makeNode({ type: 'text', metadata: { content: '原文本' } });
    const wrapper = mount(TextNode, { props: { node } });

    await wrapper.find('[data-test="node-content-text"]').trigger('dblclick');
    await wrapper.find('[data-test="node-text-textarea"]').trigger('blur');

    expect(wrapper.emitted('action')).toBeUndefined();
  });

  it('ConfigNode：摘要显示 mode/model/size/count，prompt 截断', () => {
    const prompt = '一只在雨夜霓虹街头奔跑的机械猫，'.repeat(8);
    const node = makeNode({
      type: 'config',
      title: '生成配置',
      metadata: { generationMode: 'image', model: 'wan2.7-image', size: '1024x1024', count: 2, prompt },
    });
    const wrapper = mount(ConfigNode, { props: { node } });

    expect(wrapper.find('[data-test="node-config-mode"]').text()).toBe('图片生成');
    expect(wrapper.find('[data-test="node-config-model"]').text()).toBe('wan2.7-image');
    expect(wrapper.find('[data-test="node-config-size"]').text()).toBe('1024x1024');
    expect(wrapper.find('[data-test="node-config-count"]').text()).toBe('2');

    const preview = wrapper.find('[data-test="node-config-prompt"]');
    expect(preview.attributes('title')).toBe(prompt);
    expect(preview.text().endsWith('…')).toBe(true);
    expect(preview.text().length).toBeLessThan(prompt.length);

    // 空提示词有明确占位，不显示空白
    const empty = mount(ConfigNode, { props: { node: makeNode({ type: 'config' }) } });
    expect(empty.find('[data-test="node-config-prompt"]').text()).toBe('（未填写提示词）');
  });

  it('VideoNode：原生 video 带 controls 与 src，无内容出占位', () => {
    const wrapper = mount(VideoNode, {
      props: { node: makeNode({ type: 'video', title: '视频', metadata: { content: '/media/a.mp4' } }) },
    });
    const video = wrapper.find('[data-test="node-video"]');
    expect(video.exists()).toBe(true);
    expect(video.attributes('src')).toBe('/media/a.mp4');
    expect(video.attributes('controls')).toBeDefined();

    const empty = mount(VideoNode, { props: { node: makeNode({ type: 'video' }) } });
    expect(empty.find('[data-test="node-content-placeholder"]').text()).toContain('空视频节点');
  });

  it('AudioNode：原生 audio 带 controls 与 src，时长按 mediaDurationMs 换算', () => {
    const wrapper = mount(AudioNode, {
      props: {
        node: makeNode({
          type: 'audio',
          title: '配乐',
          metadata: { content: '/media/a.mp3', mediaDurationMs: 83_000 },
        }),
      },
    });
    const audio = wrapper.find('[data-test="node-audio"]');
    expect(audio.exists()).toBe(true);
    expect(audio.attributes('src')).toBe('/media/a.mp3');
    expect(audio.attributes('controls')).toBeDefined();
    expect(wrapper.find('[data-test="node-audio-duration"]').text()).toBe('1:23');

    const empty = mount(AudioNode, { props: { node: makeNode({ type: 'audio' }) } });
    expect(empty.find('[data-test="node-content-placeholder"]').text()).toContain('空音频节点');
  });

  it('DirectorNode：点击按钮 emit action open-director', async () => {
    const wrapper = mount(DirectorNode, { props: { node: makeNode({ type: 'director', title: '导演台' }) } });

    expect(wrapper.find('[data-test="node-director-state"]').text()).toBe('未编排');
    const button = wrapper.find('[data-test="node-director-open"]');
    expect(button.text()).toContain('打开导演台');

    await button.trigger('click');
    expect(wrapper.emitted('action')?.[0]?.[0]).toEqual({ nodeId: 'n-1', action: 'open-director' });
  });

  it('GroupNode：半透明容器不渲染内容', () => {
    const wrapper = mount(GroupNode, { props: { node: makeNode({ type: 'group', title: '分组' }) } });
    const container = wrapper.find('[data-test="node-content-group"]');
    expect(container.exists()).toBe(true);
    expect(container.text()).toBe('');
  });
});

describe('节点映射表与默认尺寸', () => {
  it('NODE_COMPONENTS 覆盖八类节点，未知类型返回 null', () => {
    expect(Object.keys(NODE_COMPONENTS).sort()).toEqual([...ALL_TYPES].sort());
    for (const type of ALL_TYPES) {
      expect(getNodeComponent(type)).not.toBeNull();
    }
    expect(getNodeComponent('unknown')).toBeNull();
  });

  it('NODE_DEFAULT_SIZE 与参考仓 constants.ts 一致', () => {
    expect(Object.keys(NODE_DEFAULT_SIZE).sort()).toEqual([...ALL_TYPES].sort());
    expect(NODE_DEFAULT_SIZE.image).toEqual({ width: 340, height: 240, title: '图片' });
    expect(NODE_DEFAULT_SIZE.panorama).toEqual({ width: 340, height: 170, title: '全景图' });
    expect(NODE_DEFAULT_SIZE.text).toEqual({ width: 340, height: 240, title: '文本' });
    expect(NODE_DEFAULT_SIZE.config).toEqual({ width: 440, height: 240, title: '生成配置' });
    expect(NODE_DEFAULT_SIZE.video).toEqual({ width: 420, height: 236, title: '视频' });
    expect(NODE_DEFAULT_SIZE.audio).toEqual({ width: 360, height: 168, title: '音频' });
    expect(NODE_DEFAULT_SIZE.director).toEqual({ width: 360, height: 320, title: '导演台' });
    expect(NODE_DEFAULT_SIZE.group).toEqual({ width: 760, height: 480, title: '分组' });

    // 未知类型兜底到图片尺寸（M4 建节点时脏数据不至于建出 0×0）
    expect(getNodeDefaultSize('unknown')).toEqual(NODE_DEFAULT_SIZE.image);
  });
});

describe('CanvasPane 节点接线', () => {
  function makeProject(nodes: CanvasNodeData[]): CanvasProject {
    return {
      id: 'p-test',
      title: '节点接线',
      createdAt: 0,
      updatedAt: 0,
      nodes,
      connections: [],
      viewport: { x: 0, y: 0, k: 1 },
      backgroundMode: 'grid',
      sidePanel: { open: false, width: 320, nodeId: null },
      nodeCount: nodes.length,
      connectionCount: 0,
      schema_version: 2,
    };
  }

  it('按 project.nodes 渲染 NodeShell（world 坐标绝对定位），节点 action 汇到 CanvasPane', async () => {
    const pinia = createPinia();
    setActivePinia(pinia);
    const store = useCanvasStore();
    store.project = makeProject([
      makeNode({ id: 'n-1', position: { x: 120, y: -40 }, width: 340, height: 240 }),
      makeNode({ id: 'n-2', type: 'group', title: '分组', position: { x: 0, y: 0 }, width: 760, height: 480 }),
      makeNode({ id: 'n-3', type: 'director', title: '导演台', position: { x: 400, y: 0 } }),
    ]);
    store.selectedNodeIds = new Set(['n-1']);

    const debugSpy = vi.spyOn(console, 'debug').mockImplementation(() => {});
    const wrapper = mount(CanvasPane, { global: { plugins: [pinia] } });

    const shells = wrapper.findAll('[data-test="node-shell"]');
    expect(shells).toHaveLength(3);

    // 位置/尺寸走世界坐标 left/top/width/height，选中节点抬 z 序
    const first = shells[0]!;
    const style = first.attributes('style') ?? '';
    expect(style).toContain('left: 120px');
    expect(style).toContain('top: -40px');
    expect(style).toContain('width: 340px');
    expect(style).toContain('height: 240px');
    expect(style).toContain('z-index: 50');
    expect(first.attributes('data-selected')).toBe('true');
    expect(shells[1]!.attributes('data-selected')).toBe('false');

    // 内容组件按类型挂上（图片占位 + 分组容器 + 导演台按钮）
    expect(wrapper.find('[data-test="node-content-image"]').exists()).toBe(true);
    expect(wrapper.find('[data-test="node-content-group"]').exists()).toBe(true);
    expect(wrapper.find('[data-test="node-director-open"]').exists()).toBe(true);

    // 有节点时不再显示空画布占位文案
    expect(wrapper.find('[data-test="canvas-empty-hint"]').exists()).toBe(false);

    // 节点 emit 的 action 冒泡到 CanvasPane（M3-T01 只做 console 占位）
    await wrapper.find('[data-test="node-director-open"]').trigger('click');
    expect(debugSpy).toHaveBeenCalledTimes(1);
    expect(debugSpy.mock.calls[0]?.[1]).toEqual({ nodeId: 'n-3', action: 'open-director' });

    debugSpy.mockRestore();
    wrapper.unmount();
  });
});

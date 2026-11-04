// 节点类型 → 内容组件 的唯一映射表（M3-T01）。
//
// 规格来源：参考仓 web/src/app/(user)/canvas/components/canvas-node.tsx 的
// `nodeContentRenderers`（text/image/panorama/config/video/audio/director 各一个
// 渲染器，group 在 Shell 层直接渲染成容器）+ 同仓 constants.ts 的 NODE_DEFAULT_SIZE。
//
// 默认尺寸按「以参考仓为准」核正（计划稿注释里的旧值已过时）：
//   image     340×240（计划稿写 340×260）
//   panorama  340×170（PANORAMA_NODE_SIZE）
//   text      340×240（计划稿写 320×200）
//   config    440×240（计划稿写 360×280）
//   video     420×236（计划稿写 360×240）
//   audio     360×168 / director 360×320 / group 760×480（与计划稿一致）
import type { Component } from 'vue';

import type { CanvasNodeType } from '../types';
import AudioNode from './AudioNode.vue';
import ConfigNode from './ConfigNode.vue';
import DirectorNode from './DirectorNode.vue';
import GroupNode from './GroupNode.vue';
import ImageNode from './ImageNode.vue';
import PanoramaNode from './PanoramaNode.vue';
import TextNode from './TextNode.vue';
import VideoNode from './VideoNode.vue';

/** 八类节点的内容组件（CanvasPane 只按 node.type 取，不认识具体节点实现） */
export const NODE_COMPONENTS = {
  image: ImageNode,
  panorama: PanoramaNode,
  text: TextNode,
  config: ConfigNode,
  video: VideoNode,
  audio: AudioNode,
  director: DirectorNode,
  group: GroupNode,
} satisfies Record<CanvasNodeType, Component>;

/** 八类节点默认尺寸与默认标题（对齐参考仓 constants.ts 的 NODE_DEFAULT_SIZE） */
export const NODE_DEFAULT_SIZE = {
  image: { width: 340, height: 240, title: '图片' },
  panorama: { width: 340, height: 170, title: '全景图' },
  text: { width: 340, height: 240, title: '文本' },
  config: { width: 440, height: 240, title: '生成配置' },
  video: { width: 420, height: 236, title: '视频' },
  audio: { width: 360, height: 168, title: '音频' },
  director: { width: 360, height: 320, title: '导演台' },
  group: { width: 760, height: 480, title: '分组' },
} satisfies Record<CanvasNodeType, { width: number; height: number; title: string }>;

/**
 * 取类型对应组件。
 * 入参放宽成 string：project 来自磁盘，脏数据里的未知 type 不该让整张画布白屏，
 * 调用方拿到 null 直接不渲染即可（sanitize 层另有兜底）。
 */
export function getNodeComponent(type: string): Component | null {
  return (NODE_COMPONENTS as Record<string, Component>)[type] ?? null;
}

/** 取类型默认尺寸（M4 建节点用；未知类型给图片尺寸兜底） */
export function getNodeDefaultSize(type: string): { width: number; height: number; title: string } {
  return (
    (NODE_DEFAULT_SIZE as Record<string, { width: number; height: number; title: string }>)[type] ??
    NODE_DEFAULT_SIZE.image
  );
}

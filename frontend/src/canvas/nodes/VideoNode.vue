<template>
  <!-- 视频节点（视觉稿 .node[data-node=video]）：VID 徽章 + 时长，
       图区右上角播放按钮、底部进度条，左侧输入端口，底部「时间 + 配置镜头」 -->
  <div class="cv-node" :class="{ 'is-selected': selected }" data-test="canvas-node-video">
    <div class="cv-head">
      <span class="cv-kind cv-kind--vid">VID</span>
      <span class="cv-title" :title="title">{{ title }}</span>
      <span v-if="durationText" class="cv-size">{{ durationText }}</span>
    </div>

    <div class="cv-thumb">
      <!-- 有 url 就挂真视频（不自动播放、静音、只取元数据当首帧缩略图） -->
      <video
        v-if="node.url"
        class="cv-art"
        :src="node.url"
        muted
        playsinline
        preload="metadata"
        data-test="canvas-video-el"
      ></video>
      <div v-else class="cv-ph" data-test="canvas-video-placeholder">
        <svg class="cv-ph-ico" viewBox="0 0 24 24" aria-hidden="true">
          <rect x="3.5" y="6" width="12" height="12" rx="2" />
          <path d="M15.5 10.5l5-3v9l-5-3z" />
        </svg>
        <span class="cv-ph-name">{{ placeholderName }}</span>
        <span class="cv-ph-kind">video</span>
      </div>
      <div class="cv-grain"></div>

      <!-- 右上角播放按钮：只做视觉提示，真正的播放在右面板/播放器里 -->
      <button class="cv-play nodrag" data-test="canvas-video-play" title="播放" @click.stop>
        <svg viewBox="0 0 12 12" aria-hidden="true"><path d="M2 1l8 5-8 5z" /></svg>
      </button>

      <!-- 底部进度条：进度取 meta.progress（0~100） -->
      <div class="cv-timeline"><i :style="{ width: `${progressPercent}%` }"></i></div>
    </div>

    <div class="cv-foot">
      <span v-if="timeText" class="cv-from">{{ timeText }}</span>
      <span class="cv-spacer"></span>
      <button class="cv-act nodrag" data-test="canvas-config-shot" @click="onConfigShot">
        配置镜头
      </button>
    </div>

    <!-- 左侧输入端口：视频节点只消费上游（图片/提示词）结果 -->
    <Handle id="in" type="target" :position="Position.Left" :connectable="connectable" />
  </div>
</template>

<script setup lang="ts">
// 视频节点：左侧接入上游素材，图区展示首帧占位/真视频，底部展示播放进度。
import { computed } from 'vue';
import { Handle, Position, type NodeProps } from '@vue-flow/core';
import {
  clampPercent,
  fileBaseName,
  metaNumber,
  metaString,
  nodeTimeText,
  nodeTitle,
  type CanvasNodeData,
  type NodeActionPayload,
} from './types';

const props = withDefaults(
  defineProps<{
    id: string;
    selected?: boolean;
    connectable?: boolean;
    data: CanvasNodeData;
  }>(),
  { selected: false, connectable: undefined },
);

// 官方 NodeProps 形状对齐（见 ImageNode 里的说明：泛型实参编译器解析不了）
void (props as unknown as Pick<NodeProps<CanvasNodeData>, 'id' | 'selected' | 'connectable' | 'data'>);

const emit = defineEmits<{ (e: 'send-to-agent', payload: NodeActionPayload): void }>();

const node = computed(() => props.data.node);
const title = computed(() => nodeTitle(node.value, '未命名视频'));

/** 头部右侧时长：meta.duration 可以是 "4.0s" 这种文案，也可以是秒数 */
const durationText = computed(() => {
  const text = metaString(node.value, 'duration');
  if (text) return text;
  const seconds = metaNumber(node.value, 'duration_seconds', 0);
  return seconds > 0 ? `${seconds.toFixed(1)}s` : '';
});

const placeholderName = computed(
  () => fileBaseName(node.value.file) || title.value || '未生成视频',
);

/** 进度条宽度：默认 34%（视觉稿的静态占比），有 meta.progress 就用它 */
const progressPercent = computed(() => {
  const raw = metaNumber(node.value, 'progress', -1);
  return raw < 0 ? 34 : clampPercent(raw);
});

const timeText = computed(() => nodeTimeText(node.value));

function onConfigShot() {
  emit('send-to-agent', { id: props.id, action: 'config-shot' });
}
</script>

<style scoped src="./node-shell.css"></style>

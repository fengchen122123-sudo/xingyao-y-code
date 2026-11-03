<template>
  <!-- 视频节点（视觉稿 .node[data-node=video]）：VID 徽章 + 时长，
       图区右上角播放按钮、底部进度条，左侧输入端口，底部「时间 + 配置镜头」。
       生成链路激活后它不再只是展示壳：pending 态盖「生成中」遮罩、失败态把
       原因留在节点上、播放按钮真的会 play()/pause()。 -->
  <div class="cv-node" :class="{ 'is-selected': selected }" data-test="canvas-node-video">
    <div class="cv-head">
      <span class="cv-kind cv-kind--vid">VID</span>
      <span class="cv-title" :title="title">{{ title }}</span>
      <span v-if="durationText" class="cv-size">{{ durationText }}</span>
    </div>

    <div class="cv-thumb">
      <!-- 有 url 就挂真视频（不自动播放、静音、只取元数据当首帧缩略图）；
           播放/暂停状态由媒体事件回写，不用自己去猜 -->
      <video
        v-if="node.url"
        ref="videoRef"
        class="cv-art"
        :src="node.url"
        muted
        playsinline
        preload="metadata"
        data-test="canvas-video-el"
        @play="playing = true"
        @pause="playing = false"
        @ended="playing = false"
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

      <!-- 右上角播放按钮：有产物且不在生成中才出现，点击就地 play()/pause() -->
      <button
        v-if="node.url && !videoPending"
        class="cv-play nodrag"
        data-test="canvas-video-play"
        :title="playing ? '暂停' : '播放'"
        @click.stop="onPlayToggle"
      >
        <svg viewBox="0 0 12 12" aria-hidden="true">
          <path v-if="!playing" d="M2 1l8 5-8 5z" />
          <path v-else d="M2 1h3v10H2zM7 1h3v10H7z" />
        </svg>
      </button>

      <!-- 生成中：与改图同款遮罩。视频要跑 1~5 分钟，时长写进文案里，
           免得用户以为卡死去连点重试（连点只会被后端闸门挡掉） -->
      <div v-if="videoPending" class="cv-mask" data-test="canvas-video-pending">
        <span>视频生成中…通常 1~5 分钟</span>
      </div>
      <!-- 失败留痕：原因写在节点上而不是悄悄消失，用户能对照着改指令重试 -->
      <div
        v-else-if="errorText"
        class="cv-mask cv-mask--error"
        data-test="canvas-video-error"
        :title="errorText"
      >
        <span>生成失败：{{ errorText }}</span>
      </div>

      <!-- 底部进度条：pending 用完整宽度的动画条表达「在跑」，有产物后回落
           到 meta.progress（0~100） -->
      <div class="cv-timeline">
        <i
          :class="{ 'is-running': videoPending }"
          :style="{ width: `${videoPending ? 100 : progressPercent}%` }"
        ></i>
      </div>
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
// 只负责画：pending/失败数据由父级从 store 映射进来（videoPending），
// 失败原因从节点 meta 读（applyVideoResult 写入的 meta.error）。
import { computed, ref } from 'vue';
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
    /**
     * 是否正在生成视频：任务受理时画布层先落一个没有产物的 pending 节点，
     * 事件回来后由 applyVideoResult 清掉。由 CanvasPane 从
     * store.pendingVideos 映射，节点只画遮罩，不关心任务号。
     */
    videoPending?: boolean;
  }>(),
  { selected: false, connectable: undefined, videoPending: false },
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

/** 失败原因（applyVideoResult 写入 meta.error）；没有就是空串 */
const errorText = computed(() => metaString(node.value, 'error'));

const videoRef = ref<HTMLVideoElement | null>(null);
/** 播放状态由媒体事件维护：点按钮只做 play()/pause() 切换，不自己翻状态 */
const playing = ref(false);

function onPlayToggle(): void {
  const video = videoRef.value;
  if (!video) return;
  if (video.paused) {
    void video.play();
  } else {
    video.pause();
  }
}

function onConfigShot() {
  emit('send-to-agent', { id: props.id, action: 'config-shot' });
}
</script>

<style scoped src="./node-shell.css"></style>

<style scoped>
/* 失败遮罩：复用 node-shell.css 的 .cv-mask 布局，只叠一层警示色。
   视频失败大概率可重试（审核/超时/余额），红色只是提醒不是终局 */
.cv-mask--error {
  color: #ffb4a8;
  background: rgba(120, 30, 20, 0.62);
}
/* pending 时进度条常满并往复流动：视频轮询没有百分比可报，
   「满格+流动」比卡在 34% 更像在干活 */
.cv-timeline i.is-running {
  animation: cv-video-timeline 1.6s ease-in-out infinite;
}
@keyframes cv-video-timeline {
  0% {
    opacity: 1;
    transform: translateX(0);
  }
  50% {
    opacity: 0.45;
    transform: translateX(-8%);
  }
  100% {
    opacity: 1;
    transform: translateX(0);
  }
}
</style>

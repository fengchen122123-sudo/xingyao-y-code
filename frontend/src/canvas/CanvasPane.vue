<template>
  <div class="canvas-wrap" data-test="canvas-pane-root">
    <!-- 左列：项目列表。v1 旧数据照列但置灰（后端 supported=false），
         提示「旧版数据，不支持」而不隐藏——用户至少能看到并手动清理 -->
    <aside class="canvas-projects" data-test="canvas-project-list">
      <div class="cp-head">
        <span class="cp-title">画布项目</span>
        <button
          class="cp-new"
          data-test="canvas-new-project"
          :disabled="store.saving || creating"
          title="新建空白画布"
          @click="createProject()"
        >新建</button>
      </div>

      <div v-if="store.loadError" class="cp-error" data-test="canvas-error">{{ store.loadError }}</div>

      <div v-if="!store.projectList.length" class="cp-empty" data-test="canvas-project-empty">
        暂无画布项目
      </div>

      <button
        v-for="p in store.projectList"
        :key="p.id"
        class="cp-item"
        :class="{ active: store.project?.id === p.id, disabled: !p.supported }"
        :disabled="!p.supported"
        :title="p.supported ? p.title : '旧版数据，不支持'"
        :data-test="'canvas-project-' + p.id"
        @click="openProject(p)"
      >
        <span class="cp-name">{{ p.title }}</span>
        <span v-if="!p.supported" class="cp-badge" data-test="canvas-v1-badge">旧版数据，不支持</span>
        <span v-else class="cp-time">{{ formatTime(p.updatedAt) }}</span>
      </button>
    </aside>

    <!-- 右列：画布舞台。InfiniteCanvas 由 M2-T06 挂进 canvas-mount -->
    <section class="canvas-stage" data-test="canvas-stage">
      <div v-if="!store.project" class="canvas-hint" data-test="canvas-stage-empty">
        选择左侧项目，或新建一个画布开始
      </div>
      <div v-else class="canvas-mount" data-test="canvas-mount">
        <!-- M2-T06 在此挂入 InfiniteCanvas（受控 viewport + update:viewport） -->
      </div>
    </section>
  </div>
</template>

<script setup lang="ts">
import { ref, watch } from 'vue';

import { useCanvasStore } from '../stores/canvas';
import type { ProjectMeta } from '../canvas/bridge';

const props = withDefaults(defineProps<{ active?: boolean }>(), { active: false });

const store = useCanvasStore();
const creating = ref(false);

/** 只刷新列表、不动当前打开的项目：切页签回来不应把编辑中的画布重置掉 */
function refreshList(): void {
  void store.refreshProjectList();
}

async function createProject(): Promise<void> {
  if (creating.value) return;
  creating.value = true;
  try {
    await store.createProject();
  } finally {
    creating.value = false;
  }
}

function openProject(meta: ProjectMeta): void {
  if (!meta.supported || store.project?.id === meta.id) return;
  void store.openProject(meta.id);
}

function formatTime(ts: number): string {
  if (!ts) return '';
  try {
    return new Date(ts).toLocaleDateString('zh-CN', { month: '2-digit', day: '2-digit' });
  } catch {
    return '';
  }
}

// 面板是 v-show 常驻挂载：只在「首次激活」与「再次激活」时拉列表，
// 未激活时不打桥（启动阶段右栏默认不显示画布，不给桥加无谓调用）
watch(
  () => props.active,
  (active) => {
    if (active) refreshList();
  },
  { immediate: true },
);
</script>

<style scoped>
.canvas-wrap {
  display: flex;
  height: 100%;
  min-height: 0;
  background: var(--bg-primary, #1b1b1f);
  color: var(--text-primary, #ddd);
  font-size: 12px;
}

.canvas-projects {
  width: 176px;
  flex: 0 0 176px;
  display: flex;
  flex-direction: column;
  border-right: 1px solid var(--border-color, #333);
  padding: 8px 6px;
  gap: 6px;
  overflow-y: auto;
}

.cp-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 2px 4px;
}

.cp-title {
  font-weight: 600;
  color: var(--text-secondary, #aaa);
}

.cp-new {
  padding: 3px 8px;
  border: 1px solid var(--border-color, #3a3a3a);
  border-radius: 6px;
  background: var(--bg-secondary, #26262b);
  color: inherit;
  cursor: pointer;
}

.cp-new:disabled {
  opacity: 0.5;
  cursor: default;
}

.cp-error {
  padding: 4px 6px;
  border-radius: 6px;
  color: #e08a8a;
  background: rgba(224, 138, 138, 0.12);
  word-break: break-all;
}

.cp-empty {
  color: var(--text-secondary, #888);
  padding: 6px 2px;
}

.cp-item {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 2px;
  width: 100%;
  padding: 6px 8px;
  border: 1px solid transparent;
  border-radius: 8px;
  background: var(--bg-secondary, #26262b);
  color: inherit;
  text-align: left;
  cursor: pointer;
}

.cp-item.active {
  border-color: var(--accent, #4b8bf5);
}

.cp-item.disabled {
  opacity: 0.55;
  cursor: not-allowed;
}

.cp-name {
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.cp-badge {
  font-size: 11px;
  color: #c9a24a;
}

.cp-time {
  font-size: 11px;
  color: var(--text-secondary, #888);
}

.canvas-stage {
  position: relative;
  flex: 1 1 auto;
  min-width: 0;
  overflow: hidden;
  background: var(--bg-secondary, #202024);
}

.canvas-mount {
  position: absolute;
  inset: 0;
}

.canvas-hint {
  display: flex;
  align-items: center;
  justify-content: center;
  height: 100%;
  color: var(--text-secondary, #888);
}
</style>

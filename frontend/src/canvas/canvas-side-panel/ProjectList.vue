<template>
  <!-- 收起态：只留一条窄轨 + 展开按钮，画布区拿回横向空间（M3-T07 可折叠侧栏） -->
  <aside v-if="collapsed" class="cpp-rail" data-test="canvas-project-rail">
    <button
      type="button"
      class="cpp-rail-btn"
      data-test="canvas-projects-expand"
      title="展开画布项目"
      @click="collapsed = false"
    >»</button>
  </aside>

  <!-- 展开态：项目列表 + 新建/导入/导出入口。
       v1 旧数据照列但置灰（后端 supported=false），提示「旧版数据，不支持」而不隐藏——
       用户至少能看到并手动清理。 -->
  <aside v-else class="canvas-projects" data-test="canvas-project-list">
    <div class="cp-head">
      <span class="cp-title">画布项目</span>
      <div class="cp-head-actions">
        <button
          type="button"
          class="cp-btn"
          data-test="canvas-new-project"
          :disabled="creating"
          title="新建空白画布"
          @click="createProject"
        >新建</button>
        <button
          type="button"
          class="cp-btn"
          data-test="canvas-import-project"
          title="从 .canvas.json 文件导入为新项目"
          @click="emit('import')"
        >导入</button>
        <button
          type="button"
          class="cp-btn"
          data-test="canvas-export-project"
          :disabled="!store.project"
          :title="store.project ? '导出当前项目' : '先打开或新建一个画布'"
          @click="emit('export')"
        >导出</button>
        <button
          type="button"
          class="cp-btn cp-collapse"
          data-test="canvas-projects-collapse"
          title="收起项目栏"
          @click="collapsed = true"
        >«</button>
      </div>
    </div>

    <!-- 桥回的错误（读列表/删除失败）与本地重命名失败共用这块提示区 -->
    <div v-if="notice || store.loadError" class="cp-error" data-test="canvas-error">
      {{ notice || store.loadError }}
    </div>

    <div v-if="!store.projectList.length" class="cp-empty" data-test="canvas-project-empty">
      暂无画布项目
    </div>

    <div
      v-for="p in store.projectList"
      :key="p.id"
      class="cp-row"
      :class="{ active: store.project?.id === p.id, disabled: !p.supported }"
    >
      <!-- 重命名中：原地换成输入框，Enter 提交 / Esc 取消 / 失焦也提交 -->
      <input
        v-if="renamingId === p.id"
        v-model="renameDraft"
        class="cp-rename"
        data-test="canvas-rename-input"
        :aria-label="'重命名 ' + p.title"
        @keydown.enter.prevent="commitRename(p)"
        @keydown.esc.prevent="cancelRename"
        @blur="commitRename(p)"
      />
      <button
        v-else
        type="button"
        class="cp-item"
        :disabled="!p.supported"
        :title="p.supported ? p.title : '旧版数据，不支持'"
        :data-test="'canvas-project-' + p.id"
        @click="openProject(p)"
      >
        <span class="cp-name">{{ p.title }}</span>
        <span v-if="!p.supported" class="cp-badge" data-test="canvas-v1-badge">旧版数据，不支持</span>
        <span v-else class="cp-time">{{ formatTime(p.updatedAt) }}</span>
      </button>

      <!-- 行内小按钮：v1 项目不给改名的入口（旧结构改成新标题没有意义，也免得误改） -->
      <div v-if="renamingId !== p.id" class="cp-row-actions">
        <button
          type="button"
          class="cp-mini"
          data-test="canvas-rename-project"
          :disabled="!p.supported"
          title="重命名"
          @click.stop="startRename(p)"
        >改名</button>
        <button
          type="button"
          class="cp-mini is-danger"
          data-test="canvas-delete-project"
          title="删除这个画布"
          @click.stop="removeProject(p)"
        >删除</button>
      </div>
    </div>
  </aside>
</template>

<script setup lang="ts">
// M3-T07 项目侧栏。列表/新建/打开/删除一律走 store 的 M2-T04 桥 action（store 负责状态与列表同步）；
// 重命名没有对应的 store action，按计划「侧栏调 M2 桥」直接 loadProject + saveProject：
// 只读写目标那一份存档，不把用户正在编辑的画布切走。
import { ref } from 'vue';

import { canvasBridge, type ProjectMeta } from '../bridge';
import { useCanvasStore } from '../../stores/canvas';

defineOptions({ name: 'CanvasProjectList' });

const emit = defineEmits<{ import: []; export: [] }>();

const store = useCanvasStore();

/** 侧栏折叠态：纯 UI，不落盘（项目里的 sidePanel 是「节点详情栏」，与这里无关） */
const collapsed = ref(false);
/** 新建的并发闸门：桥往返期间再点一次会建出两个同名空项目 */
const creating = ref(false);
/** 正在重命名的项目 id（空串 = 没有在改名） */
const renamingId = ref('');
const renameDraft = ref('');
/** 本地提示（重命名失败等），与 store.loadError 共用同一块展示区 */
const notice = ref('');

function formatTime(ts: number): string {
  if (!ts) return '';
  try {
    return new Date(ts).toLocaleDateString('zh-CN', { month: '2-digit', day: '2-digit' });
  } catch {
    return '';
  }
}

async function createProject(): Promise<void> {
  if (creating.value) return;
  creating.value = true;
  notice.value = '';
  try {
    await store.createProject();
  } finally {
    creating.value = false;
  }
}

function openProject(meta: ProjectMeta): void {
  if (!meta.supported || store.project?.id === meta.id) return;
  notice.value = '';
  void store.openProject(meta.id);
}

function startRename(meta: ProjectMeta): void {
  if (!meta.supported) return;
  notice.value = '';
  renamingId.value = meta.id;
  renameDraft.value = meta.title;
}

function cancelRename(): void {
  renamingId.value = '';
  renameDraft.value = '';
}

/**
 * 提交重命名。
 * - 当前打开的项目：改内存标题后立刻 saveNow（列表项由 store 自己回写 updatedAt）；
 * - 其它项目：load → 只替换 title → save，不切换正在编辑的画布。
 * 空标题/未改动直接放弃（不给用户造出无标题项目的机会）。
 */
async function commitRename(meta: ProjectMeta): Promise<void> {
  if (renamingId.value !== meta.id) return;
  const id = meta.id;
  const title = renameDraft.value.trim();
  cancelRename();
  if (!title || title === meta.title) return;

  if (store.project?.id === id) {
    store.project.title = title;
    await store.saveNow();
    if (store.loadError) notice.value = store.loadError;
    return;
  }

  const loaded = await canvasBridge.loadProject(id);
  if (!loaded.ok || !loaded.project) {
    notice.value = loaded.msg || '重命名失败';
    return;
  }
  const saved = await canvasBridge.saveProject({ ...loaded.project, title });
  if (!saved.ok) {
    notice.value = saved.msg || '重命名失败';
    return;
  }
  meta.title = title;
  if (typeof saved.updatedAt === 'number') meta.updatedAt = saved.updatedAt;
}

async function removeProject(meta: ProjectMeta): Promise<void> {
  notice.value = '';
  // 删除是不可撤销的磁盘操作，必须让用户明确确认一次
  if (!window.confirm(`删除画布「${meta.title}」？此操作不可撤销。`)) return;
  await store.deleteProject(meta.id);
  if (store.loadError) notice.value = store.loadError;
}
</script>

<style scoped>
.canvas-projects {
  width: 184px;
  flex: 0 0 184px;
  display: flex;
  flex-direction: column;
  border-right: 1px solid var(--border-color, #333);
  padding: 8px 6px;
  gap: 6px;
  overflow-y: auto;
}

.cpp-rail {
  width: 24px;
  flex: 0 0 24px;
  display: flex;
  justify-content: center;
  border-right: 1px solid var(--border-color, #333);
  padding-top: 8px;
  background: var(--bg-primary, #1b1b1f);
}

.cpp-rail-btn {
  width: 18px;
  height: 24px;
  border: 1px solid var(--border-color, #3a3a3a);
  border-radius: 6px;
  background: var(--bg-secondary, #26262b);
  color: inherit;
  cursor: pointer;
  line-height: 1;
}

.cp-head {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 0 2px 4px;
}

.cp-title {
  font-weight: 600;
  color: var(--text-secondary, #aaa);
}

.cp-head-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}

.cp-btn {
  padding: 3px 7px;
  border: 1px solid var(--border-color, #3a3a3a);
  border-radius: 6px;
  background: var(--bg-secondary, #26262b);
  color: inherit;
  font-size: 11px;
  cursor: pointer;
}

.cp-btn:disabled {
  opacity: 0.5;
  cursor: default;
}

.cp-collapse {
  margin-left: auto;
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

.cp-row {
  display: flex;
  flex-direction: column;
  gap: 2px;
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

.cp-row.active .cp-item {
  border-color: var(--accent, #4b8bf5);
}

.cp-item:disabled {
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

.cp-rename {
  width: 100%;
  box-sizing: border-box;
  padding: 6px 8px;
  border: 1px solid var(--accent, #4b8bf5);
  border-radius: 8px;
  background: var(--bg-secondary, #26262b);
  color: inherit;
  font-size: 12px;
}

.cp-row-actions {
  display: flex;
  gap: 4px;
  padding-left: 2px;
}

.cp-mini {
  padding: 1px 6px;
  border: 1px solid transparent;
  border-radius: 5px;
  background: transparent;
  color: var(--text-secondary, #999);
  font-size: 11px;
  cursor: pointer;
}

.cp-mini:hover:not(:disabled) {
  background: rgba(255, 255, 255, 0.08);
  color: inherit;
}

.cp-mini:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}

.cp-mini.is-danger:hover {
  color: #e08a8a;
}
</style>
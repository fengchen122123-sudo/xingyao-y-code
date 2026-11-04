<template>
  <!-- 导出对话框（M3-T07）：纯前端下载，不引 JSZip 等新依赖。
       项目 JSON 走 Blob + a[download]；选中图片逐张触发下载。 -->
  <div v-if="open" class="export-mask" data-test="canvas-export-dialog" @click.self="emit('close')">
    <div class="export-panel" role="dialog" aria-label="导出画布">
      <div class="export-head">
        <span class="export-title">导出画布</span>
        <button type="button" class="export-close" data-test="canvas-export-close" @click="emit('close')">×</button>
      </div>

      <p class="export-desc">
        {{ store.project?.title || '未命名画布' }}：{{ nodeCount }} 个节点 / {{ connectionCount }} 条连线
      </p>

      <div class="export-actions">
        <button
          type="button"
          class="export-btn primary"
          data-test="canvas-export-json"
          :disabled="!store.project"
          @click="exportProject"
        >导出项目 JSON</button>
        <button
          type="button"
          class="export-btn"
          data-test="canvas-export-images"
          :disabled="!imageSources.length"
          :title="imageSources.length ? '逐张下载选中的图片节点' : '先选中图片节点'"
          @click="exportImages"
        >导出选中图片（{{ imageSources.length }}）</button>
      </div>

      <p class="export-hint">
        文件名 <code>{{ fileName }}</code>；文件可经侧栏「导入」重新载入为新项目。
        导出会剥掉运行期字段（jobId / 进度 / 开始时间，loading 归 idle），导入时节点与连线 id 全部重生成。
      </p>

      <p v-if="message" class="export-msg" data-test="canvas-export-message">{{ message }}</p>
    </div>
  </div>
</template>

<script setup lang="ts">
// M3-T07 导出弹窗。只读 store、只往磁盘写：导出不改 project、不落盘、不碰历史栈。
import { computed, ref, watch } from 'vue';

import { CanvasNodeType } from '../types';
import { useCanvasStore } from '../../stores/canvas';

defineOptions({ name: 'CanvasExportDialog' });

const props = defineProps<{ open: boolean }>();
const emit = defineEmits<{ close: [] }>();

const store = useCanvasStore();
/** 最近一次导出的结果提示（成功/失败都落这里，不弹全局 toast） */
const message = ref('');

const nodeCount = computed(() => store.project?.nodes.length ?? 0);
const connectionCount = computed(() => store.project?.connections.length ?? 0);

/** 文件名清洗：去掉路径分隔符与 Windows 非法字符，空标题回落 canvas */
function fileBaseName(raw: string): string {
  const cleaned = (raw || '').replace(/[\\/:*?"<>|]/g, '').trim();
  return cleaned || 'canvas';
}

const fileName = computed(() => `${fileBaseName(store.project?.title || '')}.canvas.json`);

/** 选中里的图片节点（预览地址在 metadata.content；没有地址的跳过，导不出空文件） */
const imageSources = computed(() => {
  const project = store.project;
  if (!project) return [];
  return project.nodes
    .filter((node) => store.selectedNodeIds.has(node.id) && node.type === CanvasNodeType.Image)
    .map((node) => ({ name: fileBaseName(node.title || 'image'), url: node.metadata?.content || '' }))
    .filter((item) => !!item.url);
});

watch(
  () => props.open,
  (open) => {
    if (open) message.value = '';
  },
);

/** a[download] 触发下载：一次性链路，用完摘掉，别在 DOM 里留垃圾 */
function triggerDownload(href: string, filename: string): void {
  const link = document.createElement('a');
  link.href = href;
  link.download = filename;
  link.rel = 'noopener';
  document.body.appendChild(link);
  link.click();
  link.remove();
}

function exportProject(): void {
  const text = store.exportProjectJson();
  if (!text) {
    message.value = '导出失败：没有可导出的画布';
    return;
  }
  const url = URL.createObjectURL(new Blob([text], { type: 'application/json' }));
  triggerDownload(url, fileName.value);
  // 延后回收：个别浏览器在同一 tick 里 revoke 会让下载落空
  setTimeout(() => URL.revokeObjectURL(url), 1000);
  message.value = `已导出 ${fileName.value}`;
}

/** http(s) 资源先抓成 blob 再下载：跨源时 a[download] 会被忽略，只有 blob URL 稳 */
async function toBlobUrl(url: string): Promise<string> {
  try {
    const res = await fetch(url);
    if (!res.ok) throw new Error(String(res.status));
    return URL.createObjectURL(await res.blob());
  } catch {
    // 抓不到（离线/跨源被拒）就退回原链接，用户至少还能手动另存
    return url;
  }
}

/** 选中图片逐张下载（计划明确不引 JSZip，打包成 zip 属于额外依赖） */
async function exportImages(): Promise<void> {
  const items = imageSources.value;
  if (!items.length) {
    message.value = '没有选中可导出的图片';
    return;
  }
  for (const item of items) {
    const isDataUrl = item.url.startsWith('data:');
    const href = isDataUrl ? item.url : await toBlobUrl(item.url);
    triggerDownload(href, `${item.name}.png`);
    if (!isDataUrl && href !== item.url) setTimeout(() => URL.revokeObjectURL(href), 1000);
  }
  message.value = `已导出 ${items.length} 张图片`;
}
</script>

<style scoped>
.export-mask {
  position: fixed;
  inset: 0;
  z-index: 400;
  display: flex;
  align-items: center;
  justify-content: center;
  background: rgba(0, 0, 0, 0.45);
}

.export-panel {
  width: 380px;
  max-width: calc(100vw - 32px);
  padding: 14px 16px 12px;
  border: 1px solid var(--border-color, #3a3a3a);
  border-radius: 12px;
  background: var(--bg-elevated, #26262b);
  box-shadow: 0 18px 48px rgba(0, 0, 0, 0.45);
  color: inherit;
  font-size: 12px;
}

.export-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 8px;
}

.export-title {
  font-weight: 600;
  font-size: 13px;
}

.export-close {
  width: 22px;
  height: 22px;
  border: none;
  border-radius: 6px;
  background: transparent;
  color: inherit;
  cursor: pointer;
  line-height: 1;
}

.export-close:hover {
  background: rgba(255, 255, 255, 0.1);
}

.export-desc {
  margin: 0 0 10px;
  color: var(--text-secondary, #aaa);
}

.export-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 10px;
}

.export-btn {
  padding: 5px 12px;
  border: 1px solid var(--border-color, #3a3a3a);
  border-radius: 8px;
  background: var(--bg-secondary, #26262b);
  color: inherit;
  font-size: 12px;
  cursor: pointer;
}

.export-btn.primary {
  border-color: var(--accent, #4b8bf5);
}

.export-btn:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}

.export-hint {
  margin: 0;
  color: var(--text-secondary, #888);
  line-height: 1.6;
  font-size: 11px;
}

.export-hint code {
  color: var(--text-primary, #ddd);
}

.export-msg {
  margin: 8px 0 0;
  color: #7fc08a;
}
</style>
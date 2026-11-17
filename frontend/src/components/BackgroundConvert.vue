<template>
  <div class="bg-convert" data-test="bg-convert">
    <header class="bg-header">
      <div>
        <h1>背景图转换</h1>
        <p>将视频转为动画 WebP 用作主题动态背景。</p>
      </div>
      <span class="limit-pill" title="主题动态背景的体积与尺寸硬上限">
        上限 ≤ {{ limitMiB }} MiB · {{ maxDimension }} px
      </span>
    </header>

    <section class="setting-group">
      <div class="group-title">基础配置</div>
      <div class="setting-card">
        <!-- 视频转换组件（ffmpeg）：状态一律读 Store 快照 -->
        <div class="setting-row" data-test="ffmpeg-component">
          <div class="setting-copy">
            <h2>视频转换组件</h2>
            <p v-if="ffmpegComponent.error" class="setting-error" data-test="ffmpeg-component-error">
              {{ ffmpegComponent.error }}
            </p>
            <p v-else-if="ffmpegComponent.installed">ffmpeg 已就绪</p>
            <p v-else-if="ffmpegComponent.downloading">正在下载 ffmpeg 组件…</p>
            <p v-else>首次使用需下载 ffmpeg 组件</p>
          </div>
          <div class="setting-control">
            <span
              v-if="ffmpegComponent.installed"
              class="status-pill ready"
              data-test="ffmpeg-component-ready"
            >已就绪 · v{{ ffmpegComponent.version || '未知' }}</span>
            <template v-else-if="ffmpegComponent.downloading">
              <div
                class="component-progress"
                role="progressbar"
                aria-valuemin="0"
                aria-valuemax="100"
                :aria-valuenow="ffmpegPercent"
                data-test="ffmpeg-component-progress"
              >
                <div class="component-progress-bar" :style="{ width: ffmpegPercent + '%' }" />
              </div>
              <span class="component-size" data-test="ffmpeg-component-size">{{ ffmpegSizeText }}</span>
            </template>
            <span v-else class="status-pill missing" data-test="ffmpeg-component-missing">未安装</span>

            <button
              v-if="!ffmpegComponent.installed && !ffmpegComponent.downloading"
              class="btn primary"
              type="button"
              data-test="ffmpeg-component-download"
              :disabled="ffmpegBusy"
              @click="downloadFfmpegComponent"
            >下载组件</button>
            <button
              v-if="ffmpegComponent.downloading"
              class="btn ghost danger"
              type="button"
              data-test="ffmpeg-component-cancel"
              :disabled="ffmpegBusy"
              @click="cancelFfmpegComponentDownload"
            >取消</button>
            <button
              v-if="ffmpegComponent.installed && video && !video.can_convert"
              class="btn"
              type="button"
              data-test="ffmpeg-component-recheck"
              :disabled="ffmpegBusy"
              @click="recheckVideo"
            >重新检测</button>
          </div>
        </div>

        <!-- 源视频 -->
        <div class="setting-row setting-row--stackable">
          <div class="setting-copy">
            <h2>源视频</h2>
            <p v-if="!video">支持 mp4 / mov / mkv 等视频格式，建议 4~6 秒循环片段。</p>
            <template v-else>
              <p class="path mono" :title="video.path">{{ video.path }}</p>
              <div class="meta-chips">
                <span class="meta-chip">{{ fmtBytes(video.bytes) }}</span>
                <span class="meta-chip">{{ video.width && video.height ? `${video.width} × ${video.height}` : '未知分辨率' }}</span>
                <span class="meta-chip">{{ video.duration ? `${video.duration.toFixed(1)}s` : '未知时长' }}</span>
                <span class="meta-chip">预计 {{ estimatedFrames }} 帧</span>
              </div>
            </template>
            <div v-if="video && video.blockers.length" class="alert bad" data-test="video-blockers">
              <div v-for="(item, i) in video.blockers" :key="`b${i}`">{{ item }}</div>
            </div>
            <div v-if="video && video.warnings.length" class="alert warn" data-test="video-warnings">
              <div v-for="(item, i) in video.warnings" :key="`w${i}`">{{ item }}</div>
            </div>
            <div v-if="video && video.bytes > video.max_bytes" class="alert warn" data-test="source-too-big">
              源文件 {{ fmtBytes(video.bytes) }} 超过 {{ limitMiB }} MiB，若输出超限请调低帧率、质量或先裁剪片段。
            </div>
          </div>
          <div class="setting-control">
            <button
              class="btn"
              :class="{ primary: !video }"
              type="button"
              data-test="pick-video"
              @click="store.pickBackgroundVideo()"
            >{{ video ? '更换视频…' : '选择视频…' }}</button>
          </div>
        </div>

        <!-- 输出目录 -->
        <div class="setting-row">
          <div class="setting-copy">
            <h2>输出目录</h2>
            <p class="path mono" :title="store.bgConvert.outputDir || '与源视频同目录'">
              {{ store.bgConvert.outputDir || '默认与源视频同目录' }}
            </p>
          </div>
          <div class="setting-control">
            <button
              class="btn"
              type="button"
              data-test="pick-output-dir"
              @click="store.pickBackgroundOutputDir()"
            >选择文件夹…</button>
          </div>
        </div>
      </div>
    </section>

    <section class="setting-group">
      <div class="group-header">
        <span class="group-title">输出参数</span>
        <div class="preset-group">
          <button
            class="preset-chip"
            :class="{ active: isLightPreset }"
            type="button"
            data-test="preset-light"
            @click="applyPreset('light')"
          >省体积</button>
          <button
            class="preset-chip"
            :class="{ active: isQualityPreset }"
            type="button"
            data-test="preset-quality"
            @click="applyPreset('quality')"
          >高画质</button>
        </div>
      </div>
      <div class="setting-card">
        <label class="setting-row field-row">
          <div class="setting-copy">
            <h2>帧率 (fps)</h2>
          </div>
          <div class="range-control">
            <input
              data-test="param-fps"
              type="range"
              min="6"
              max="24"
              step="1"
              v-model.number="store.bgConvert.fps"
            />
            <span class="range-val mono">{{ store.bgConvert.fps }}</span>
          </div>
        </label>

        <label class="setting-row field-row">
          <div class="setting-copy">
            <h2>质量</h2>
          </div>
          <div class="range-control">
            <input
              data-test="param-quality"
              type="range"
              min="40"
              max="95"
              step="1"
              v-model.number="store.bgConvert.quality"
            />
            <span class="range-val mono">{{ store.bgConvert.quality }}</span>
          </div>
        </label>

        <label class="setting-row field-row switch-field">
          <div class="setting-copy">
            <h2>乒乓循环</h2>
            <p>正放 + 倒放拼接，实现首尾无缝循环</p>
          </div>
          <div class="setting-control">
            <input
              class="toggle-checkbox"
              data-test="param-pingpong"
              type="checkbox"
              v-model="store.bgConvert.pingpong"
            />
          </div>
        </label>
      </div>
    </section>

    <section class="setting-group">
      <div class="setting-card action-card">
        <div class="action-row">
          <div class="progress-wrap" :class="{ indeterminate: store.bgConvert.running && !hasDuration }">
            <div class="progress-meta">
              <span v-if="store.bgConvert.running" class="status-active">
                {{ stageLabel }}{{ hasDuration ? ` ${store.bgConvert.percent}%` : '' }}
              </span>
              <span v-else-if="store.bgConvert.percent >= 100" class="status-done">已完成</span>
              <span v-else>就绪</span>
              <span v-if="store.bgConvert.running && store.bgConvert.etaSeconds != null" class="eta mono">
                剩余约 {{ store.bgConvert.etaSeconds }}s
              </span>
            </div>
            <div class="progress-bg">
              <div
                class="progress-fill"
                data-test="convert-progress-fill"
                :style="{ width: (store.bgConvert.running && !hasDuration ? 30 : store.bgConvert.percent) + '%' }"
              ></div>
            </div>
          </div>

          <div class="action-buttons">
            <button
              v-if="store.bgConvert.running"
              class="btn ghost danger"
              type="button"
              data-test="cancel-convert"
              @click="store.cancelBackgroundConvert()"
            >中止</button>
            <button
              class="btn primary"
              type="button"
              data-test="start-convert"
              :disabled="!canStart"
              @click="store.startBackgroundConvert()"
            >开始转换</button>
          </div>
        </div>

        <div v-if="store.bgConvert.error" class="alert bad" data-test="convert-error">
          {{ store.bgConvert.error }}
        </div>
        <div v-if="store.bgConvert.message" class="alert ok" data-test="convert-message">
          {{ store.bgConvert.message }}
        </div>

        <div v-if="store.bgConvert.resultPath && !store.bgConvert.running" class="result-row">
          <div class="result-info">
            <span class="path mono" :title="store.bgConvert.resultPath">{{ store.bgConvert.resultPath }}</span>
            <span class="meta-chip">{{ fmtBytes(store.bgConvert.resultBytes) }} / {{ limitMiB }} MiB</span>
          </div>
          <div class="action-buttons">
            <button
              class="btn ghost"
              type="button"
              data-test="copy-result-path"
              @click="copyPath"
            >复制路径</button>
            <button
              class="btn primary"
              type="button"
              data-test="install-bg"
              @click="store.installConvertedBackground()"
            >设为当前主题背景</button>
          </div>
        </div>
      </div>
    </section>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue';
import { useAppStore } from '../stores/appStore';

const store = useAppStore();

// 上限用后端返回的真实值（与 core/gui_theme.py 同源），拿不到时退回 10 MiB。
const limitMiB = computed(() => {
  const bytes = store.bgConvert.video?.max_bytes;
  return bytes ? Math.round(bytes / 1048576) : 10;
});
const maxDimension = computed(() => store.bgConvert.video?.max_dimension ?? 8192);

const video = computed(() => store.bgConvert.video);
const hasDuration = computed(() => !!video.value?.duration);

const estimatedFrames = computed(() => {
  const v = video.value;
  if (!v?.duration) return '—';
  const one = Math.round(v.duration * store.bgConvert.fps);
  return store.bgConvert.pingpong ? one * 2 : one;
});

const canStart = computed(() => {
  const v = video.value;
  return !!v && v.can_convert && !store.bgConvert.running && !!store.bgConvert.outputDir;
});

const isLightPreset = computed(() => store.bgConvert.fps === 8 && store.bgConvert.quality === 65);
const isQualityPreset = computed(() => store.bgConvert.fps === 15 && store.bgConvert.quality === 85);

// 阶段文案：压缩动画阶段 ffmpeg 拿不到细分进度，进度会停在 88% 一小会儿，简明提示仍在处理。
const STAGE_LABELS: Record<string, string> = {
  starting: '正在启动…',
  extracting: '正在提取画面…',
  assembling: '正在组装循环…',
  encoding: '正在压缩动画（封装中，请稍候）…',
  converting: '正在转换…',
  done: '已完成',
};
const stageLabel = computed(() => STAGE_LABELS[store.bgConvert.stage] || '转换中…');

function applyPreset(kind: 'light' | 'quality') {
  if (kind === 'light') {
    store.bgConvert.fps = 8;
    store.bgConvert.quality = 65;
  } else {
    store.bgConvert.fps = 15;
    store.bgConvert.quality = 85;
  }
}

async function copyPath() {
  try {
    await navigator.clipboard.writeText(store.bgConvert.resultPath);
    store.bgConvert.message = '路径已复制';
  } catch {
    store.bgConvert.message = '复制失败，请手动选择路径';
  }
}

function fmtBytes(bytes: number) {
  if (!bytes) return '—';
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1048576) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1048576).toFixed(2)} MiB`;
}

// ── 视频转换组件（ffmpeg）──
// 展示状态一律读 Store 快照，桥事件只更新 Store；组件内不维护副本
const ffmpegComponent = computed(() => store.ffmpegComponent);
const ffmpegPercent = computed(() => {
  const value = Number(store.ffmpegComponent.percent) || 0;
  return Math.min(100, Math.max(0, Math.round(value)));
});
const ffmpegSizeText = computed(() => {
  const format = (bytes: number) => `${(Math.max(0, Number(bytes) || 0) / (1024 * 1024)).toFixed(1)} MB`;
  const total = Number(store.ffmpegComponent.total_bytes) || 0;
  const done = Number(store.ffmpegComponent.downloaded_bytes) || 0;
  return total > 0 ? `${format(done)} / ${format(total)}` : format(done);
});
const ffmpegBusy = ref(false);

async function withFfmpegBusy(task: () => Promise<unknown>) {
  if (ffmpegBusy.value) return;
  ffmpegBusy.value = true;
  try {
    await task();
  } finally {
    ffmpegBusy.value = false;
  }
}

function downloadFfmpegComponent() {
  void withFfmpegBusy(() => store.downloadFfmpegComponent());
}

function cancelFfmpegComponentDownload() {
  void withFfmpegBusy(() => store.cancelFfmpegComponentDownload());
}

function recheckVideo() {
  void withFfmpegBusy(() => store.redescribeBackgroundVideo());
}

onMounted(async () => {
  await store.refreshFfmpegComponentStatus();
  const current = video.value;
  if (
    ffmpegComponent.value.installed
    && current
    && !current.can_convert
    && current.ffmpeg_available === false
  ) {
    void recheckVideo();
  }
});
</script>

<style scoped>
.bg-convert {
  max-width: 860px;
  margin: 0 auto;
  display: flex;
  flex-direction: column;
  gap: 22px;
}

.bg-header {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 16px;
  flex-wrap: wrap;
}
.bg-header h1 {
  margin: 0 0 6px;
  font-size: 25px;
  color: var(--text);
}
.bg-header p {
  margin: 0;
  color: var(--text-faint);
  font-size: 13px;
}
.limit-pill {
  padding: 4px 10px;
  border: 1px solid var(--border);
  border-radius: 999px;
  background: var(--bg1);
  color: var(--text-dim);
  font-size: 11.5px;
  white-space: nowrap;
}

.setting-group {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.group-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}
.group-title {
  margin: 0 0 0 2px;
  font-size: 13.5px;
  font-weight: 600;
  color: var(--text-dim);
}

.preset-group {
  display: inline-flex;
  gap: 6px;
}
.preset-chip {
  padding: 4px 11px;
  border: 1px solid var(--border);
  border-radius: 999px;
  background: var(--bg1);
  color: var(--text-dim);
  font-size: 12px;
  cursor: pointer;
  transition: border-color 0.15s, color 0.15s, background 0.15s;
}
.preset-chip:hover {
  border-color: var(--accent);
  color: var(--accent);
}
.preset-chip.active {
  background: var(--accent-soft);
  border-color: var(--accent);
  color: var(--accent);
  font-weight: 600;
}

.setting-card {
  border: 1px solid var(--border);
  border-radius: 12px;
  background: var(--bg1);
  overflow: hidden;
}
.setting-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 20px;
  padding: 15px 18px;
  border-bottom: 1px solid var(--border);
}
.setting-row:last-child {
  border-bottom: 0;
}
.setting-row--stackable {
  align-items: flex-start;
}
.setting-copy {
  flex: 1 1 auto;
  min-width: 0;
}
.setting-copy h2 {
  margin: 0;
  font-size: 14px;
  font-weight: 600;
  color: var(--text);
}
.setting-copy p {
  margin: 4px 0 0;
  color: var(--text-faint);
  font-size: 12.5px;
  line-height: 1.5;
}
.setting-error {
  color: var(--red) !important;
}
.setting-control {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-shrink: 0;
}

.mono {
  font-family: var(--mono);
}
.path {
  font-size: 12px;
  color: var(--text-dim);
  word-break: break-all;
}
.path-line {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.meta-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 8px;
}
.meta-chip {
  padding: 2px 8px;
  border-radius: 6px;
  background: var(--bg2);
  border: 1px solid var(--border);
  color: var(--text-dim);
  font-size: 11.5px;
  white-space: nowrap;
}

.status-pill {
  padding: 3px 9px;
  border-radius: 999px;
  font-size: 12px;
  white-space: nowrap;
}
.status-pill.ready {
  background: color-mix(in srgb, var(--green) 12%, transparent);
  color: var(--green);
}
.status-pill.missing {
  background: var(--bg2);
  color: var(--text-faint);
}

.component-progress {
  width: 160px;
  height: 6px;
  border-radius: 999px;
  background: var(--bg3);
  overflow: hidden;
}
.component-progress-bar {
  height: 100%;
  background: var(--accent);
  transition: width 0.2s ease;
}
.component-size {
  color: var(--text-faint);
  font-size: 11.5px;
  white-space: nowrap;
}

.field-row {
  cursor: default;
}
.range-control {
  display: flex;
  align-items: center;
  gap: 12px;
  flex: 0 0 260px;
  max-width: 55%;
}
.range-control input[type='range'] {
  flex: 1;
  accent-color: var(--accent);
  cursor: pointer;
}
.range-val {
  min-width: 28px;
  text-align: right;
  font-size: 13px;
  font-weight: 600;
  color: var(--text);
}

.switch-field {
  cursor: pointer;
}
.toggle-checkbox {
  width: 18px;
  height: 18px;
  accent-color: var(--accent);
  cursor: pointer;
}

.btn {
  height: 34px;
  padding: 0 14px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--bg2);
  color: var(--text);
  font: inherit;
  font-size: 12.5px;
  font-weight: 500;
  cursor: pointer;
  white-space: nowrap;
  transition: background 0.15s, border-color 0.15s, color 0.15s;
}
.btn:hover:not(:disabled) {
  background: var(--bg3);
  border-color: var(--accent);
  color: var(--accent);
}
.btn:disabled {
  opacity: 0.48;
  cursor: not-allowed;
}
.btn.primary {
  background: var(--accent);
  border-color: var(--accent);
  color: #fff;
}
.btn.primary:hover:not(:disabled) {
  color: #fff;
  filter: brightness(1.08);
}
.btn.ghost {
  background: transparent;
}
.btn.danger {
  border-color: var(--red);
  color: var(--red);
}
.btn.danger:hover:not(:disabled) {
  background: color-mix(in srgb, var(--red) 10%, transparent);
  border-color: var(--red);
  color: var(--red);
}

.alert {
  margin-top: 8px;
  padding: 8px 12px;
  border-radius: 8px;
  font-size: 12px;
  line-height: 1.55;
}
.alert.bad {
  background: color-mix(in srgb, var(--red) 10%, transparent);
  color: var(--red);
}
.alert.warn {
  background: color-mix(in srgb, var(--orange) 10%, transparent);
  color: var(--orange);
}
.alert.ok {
  background: color-mix(in srgb, var(--green) 10%, transparent);
  color: var(--green);
}

.action-card {
  padding: 16px 18px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.action-card .alert {
  margin-top: 0;
}
.action-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 18px;
}
.progress-wrap {
  flex: 1 1 auto;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 7px;
}
.progress-meta {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  font-size: 12px;
  color: var(--text-faint);
}
.status-active {
  color: var(--accent);
  font-weight: 500;
}
.status-done {
  color: var(--green);
  font-weight: 500;
}
.progress-bg {
  height: 7px;
  border-radius: 999px;
  background: var(--bg3);
  overflow: hidden;
}
.progress-fill {
  height: 100%;
  background: var(--accent);
  transition: width 0.25s ease;
}
.progress-wrap.indeterminate .progress-fill {
  animation: bg-convert-slide 1.2s ease-in-out infinite alternate;
}
@keyframes bg-convert-slide {
  from { opacity: 0.35; }
  to { opacity: 1; }
}

.action-buttons {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
}

.result-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 14px;
  padding-top: 12px;
  border-top: 1px solid var(--border);
  flex-wrap: wrap;
}
.result-info {
  flex: 1 1 240px;
  min-width: 0;
  display: flex;
  align-items: center;
  gap: 10px;
}
.result-info .path {
  min-width: 0;
}

@media (max-width: 640px) {
  .setting-row,
  .action-row {
    flex-direction: column;
    align-items: stretch;
    gap: 12px;
  }
  .setting-control,
  .action-buttons {
    justify-content: flex-end;
    flex-wrap: wrap;
  }
  .range-control {
    flex: none;
    max-width: none;
    width: 100%;
  }
}
</style>

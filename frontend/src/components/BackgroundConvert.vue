<template>
  <div class="bg-convert" data-test="bg-convert">
    <header class="settings-header">
      <div>
        <h1>背景图转换</h1>
        <p>
          把 mp4 / mov / mkv 等视频转成<strong>动画 WebP</strong>，用作 GUI 主题的动态背景图。
        </p>
      </div>
    </header>

    <section class="card note-card">
      <h2>先看清楚限制</h2>
      <ul class="limits">
        <li>
          转换结果（不是源视频）必须 ≤ <b>{{ limitMiB }} MiB</b>，
          边长 ≤ <b>{{ maxDimension }} px</b> —— 这是主题背景图的硬上限，超了无法使用。
        </li>
        <li>
          超过上限时，转换会被判定失败并<strong>自动删除半成品</strong>，并提示你降低帧率或质量。
        </li>
        <li>
          为什么不转成 mp4 / GIF：背景图走 CSS <code>background-image</code>，承载不了视频；
          GIF 不在资源白名单内且画质差。<strong>动画 WebP 是唯一零改动可用的格式。</strong>
        </li>
        <li>
          建议先截取 <b>4~6 秒</b>、fps <b>8~15</b> 的循环片段：体积小、内存占用低、观感足够。
        </li>
        <li>
          转换使用<strong>随包内置的 ffmpeg</strong>，无需自行安装；
          仅在内置副本缺失时才回退到系统 <code>ffmpeg</code>。
          <span v-if="ffmpegChecked && !ffmpegOk" class="bad">当前未检测到 ffmpeg —— 无法转换。</span>
        </li>
      </ul>
    </section>

    <!-- ① 选择视频 -->
    <section class="card">
      <h2>1. 选择视频</h2>
      <div class="row">
        <button class="btn primary" data-test="pick-video" @click="store.pickBackgroundVideo()">选择视频文件…</button>
        <span v-if="video" class="path mono" :title="video.path">{{ video.path }}</span>
      </div>

      <div v-if="video" class="info-grid">
        <div><span class="k">文件</span><span class="v">{{ video.name }}</span></div>
        <div><span class="k">大小</span><span class="v">{{ fmtBytes(video.bytes) }}</span></div>
        <div><span class="k">分辨率</span><span class="v">{{ video.width && video.height ? `${video.width} × ${video.height}` : '未知' }}</span></div>
        <div><span class="k">时长</span><span class="v">{{ video.duration ? `${video.duration.toFixed(1)} 秒` : '未知' }}</span></div>
        <div><span class="k">预计输出帧数</span><span class="v">{{ estimatedFrames }} 帧（{{ store.bgConvert.pingpong ? '乒乓循环，正放+倒放' : '直线循环' }}）</span></div>
      </div>

      <div v-if="video && video.blockers.length" class="alert bad" data-test="video-blockers">
        <b>不能转换：</b>
        <div v-for="(item, i) in video.blockers" :key="`b${i}`">{{ item }}</div>
      </div>
      <div v-if="video && video.warnings.length" class="alert warn" data-test="video-warnings">
        <div v-for="(item, i) in video.warnings" :key="`w${i}`">{{ item }}</div>
      </div>
      <!-- 源文件超过上限时的显式提示（上限本身作用在"输出"上，这里提醒体量风险） -->
      <div v-if="video && video.bytes > video.max_bytes" class="alert warn" data-test="source-too-big">
        源视频 <b>{{ fmtBytes(video.bytes) }}</b> 已超过 {{ limitMiB }} MiB 上限。
        注意：<b>上限卡的是"转换结果"，不是源文件</b> —— 源文件大不代表结果一定超标，
        但视频越长／画面越复杂，结果超标的可能性越大。若转换后被判定超标，请按提示降低帧率或质量，
        或先裁剪出 4~6 秒的循环片段。
      </div>
    </section>

    <!-- ② 输出目录 -->
    <section class="card">
      <h2>2. 输出文件夹</h2>
      <div class="row">
        <button class="btn" data-test="pick-output-dir" @click="store.pickBackgroundOutputDir()">选择输出文件夹…</button>
        <span class="path mono" :title="store.bgConvert.outputDir">{{ store.bgConvert.outputDir || '（默认：与源视频同目录）' }}</span>
      </div>
      <p class="hint">输出文件名固定为 <code>源文件名-动态背景.webp</code>。</p>
    </section>

    <!-- ③ 参数 -->
    <section class="card">
      <h2>3. 参数</h2>
      <div class="params">
        <label class="field">
          <span>帧率 fps</span>
          <input data-test="param-fps" type="range" min="6" max="24" step="1" v-model.number="store.bgConvert.fps" />
          <b>{{ store.bgConvert.fps }}</b>
        </label>
        <label class="field">
          <span>质量</span>
          <input data-test="param-quality" type="range" min="40" max="95" step="1" v-model.number="store.bgConvert.quality" />
          <b>{{ store.bgConvert.quality }}</b>
        </label>
        <label class="field checkbox">
          <input data-test="param-pingpong" type="checkbox" v-model="store.bgConvert.pingpong" />
          <span>
            乒乓循环（正放+倒放）
            <em>源视频首尾不吻合时，只有乒乓循环能做到无缝；呼吸/晃动类动作尤其合适。</em>
          </span>
        </label>
      </div>
      <div class="row">
        <button class="btn ghost" data-test="preset-light" @click="applyPreset('light')">用省体积参数</button>
        <button class="btn ghost" data-test="preset-quality" @click="applyPreset('quality')">用高画质参数</button>
      </div>
    </section>

    <!-- ④ 转换 -->
    <section class="card">
      <h2>4. 开始转换</h2>
      <div class="row">
        <button
          class="btn primary"
          data-test="start-convert"
          :disabled="!canStart"
          @click="store.startBackgroundConvert()"
        >开始转换</button>
        <button
          v-if="store.bgConvert.running"
          class="btn ghost danger"
          data-test="cancel-convert"
          @click="store.cancelBackgroundConvert()"
        >中止</button>
        <span v-if="store.bgConvert.running" class="mono">{{ store.bgConvert.percent }}%</span>
        <span v-if="store.bgConvert.running && store.bgConvert.etaSeconds != null" class="hint">
          剩余约 {{ store.bgConvert.etaSeconds }} 秒
        </span>
      </div>

      <div class="progress-wrap" :class="{ indeterminate: store.bgConvert.running && !hasDuration }">
        <div class="progress-bg">
          <div
            class="progress-fill"
            data-test="convert-progress-fill"
            :style="{ width: (store.bgConvert.running && !hasDuration ? 30 : store.bgConvert.percent) + '%' }"
          ></div>
        </div>
        <div class="progress-meta">
          <span v-if="store.bgConvert.running">{{ stageLabel }} {{ hasDuration ? store.bgConvert.percent + '%' : '' }}</span>
          <span v-else-if="store.bgConvert.percent >= 100">已完成</span>
          <span v-else>尚未开始</span>
        </div>
      </div>

      <div v-if="store.bgConvert.error" class="alert bad" data-test="convert-error">{{ store.bgConvert.error }}</div>
      <div v-if="store.bgConvert.message" class="alert ok" data-test="convert-message">{{ store.bgConvert.message }}</div>

      <div v-if="store.bgConvert.resultPath && !store.bgConvert.running" class="result">
        <div class="info-grid">
          <div><span class="k">输出文件</span><span class="v mono">{{ store.bgConvert.resultPath }}</span></div>
          <div><span class="k">体积</span><span class="v">{{ fmtBytes(store.bgConvert.resultBytes) }} / 上限 {{ limitMiB }} MiB</span></div>
        </div>
        <div class="row">
          <button class="btn primary" data-test="install-bg" @click="store.installConvertedBackground()">
            直接设为当前主题的背景图
          </button>
          <button class="btn ghost" data-test="copy-result-path" @click="copyPath">复制路径</button>
        </div>
        <p class="hint">
          也可以手动使用：外观与主题 →「全局颜色」→ 全局背景图片 → 选择背景图片…
        </p>
      </div>
    </section>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue';
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
const ffmpegChecked = computed(() => !!video.value);
const ffmpegOk = computed(() => !!video.value?.ffmpeg_available);

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

// 阶段文案：压缩动画阶段 ffmpeg 拿不到细分进度（动画 WebP 封装器要收齐全部帧才写盘），
// 进度会停在 88% 一小会儿，这里明确告知还没卡死。
const STAGE_LABELS: Record<string, string> = {
  starting: '正在启动…',
  extracting: '正在提取画面…',
  assembling: '正在组装循环序列…',
  encoding: '正在压缩动画（此阶段读取不到细分进度，请稍候）…',
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
</script>

<style scoped>
.bg-convert { display: flex; flex-direction: column; gap: 14px; padding: 4px 0 24px; }
/* SettingsPage 的 .settings-header 是 scoped 样式，作用不到子组件内部，
   这里按同一套排版自己声明一份，保证与本页其它分区视觉一致 */
.settings-header { max-width: 1120px; margin: 0 auto 26px; }
.settings-header h1 { margin: 0 0 10px; font-size: 32px; letter-spacing: -1px; }
.settings-header p { margin: 0; color: var(--text-faint); font-size: 13px; }
.card {
  background: var(--bg2);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 16px 18px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.card h2 { font-size: 13.5px; margin: 0; color: var(--text); }
.note-card { background: var(--accent-soft); }
.limits { margin: 0; padding-left: 18px; font-size: 12.5px; line-height: 1.75; color: var(--text-dim); }
.limits b { color: var(--text); }
.limits code, .hint code { background: var(--bg3); padding: 1px 5px; border-radius: 4px; font-size: 11.5px; }
.row { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.path { font-size: 11.5px; color: var(--text-faint); word-break: break-all; }
.info-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 6px 16px; }
.info-grid > div { display: flex; gap: 8px; font-size: 12.5px; }
.info-grid .k { color: var(--text-faint); min-width: 84px; }
.info-grid .v { color: var(--text); word-break: break-all; }
.hint { font-size: 11.5px; color: var(--text-faint); margin: 0; }
.alert { font-size: 12.5px; line-height: 1.7; padding: 9px 12px; border-radius: 9px; }
.alert.bad { background: color-mix(in srgb, var(--red) 12%, transparent); color: var(--red); }
.alert.warn { background: color-mix(in srgb, var(--orange) 12%, transparent); color: var(--orange); }
.alert.ok { background: color-mix(in srgb, var(--green) 12%, transparent); color: var(--green); }
.alert b { color: inherit; }
.bad { color: var(--red); }
.btn {
  border: 1px solid var(--border);
  background: var(--bg3);
  color: var(--text);
  border-radius: 8px;
  padding: 6px 14px;
  font-size: 12.5px;
  cursor: pointer;
  transition: 0.15s;
}
.btn:hover:not(:disabled) { border-color: var(--accent); color: var(--accent); }
.btn:disabled { opacity: 0.5; cursor: not-allowed; }
.btn.primary { background: var(--accent); border-color: var(--accent); color: #fff; }
.btn.primary:hover:not(:disabled) { color: #fff; filter: brightness(1.08); }
.btn.ghost { background: transparent; }
.btn.danger { border-color: var(--red); color: var(--red); }
.params { display: flex; flex-direction: column; gap: 10px; }
.field { display: flex; align-items: center; gap: 10px; font-size: 12.5px; color: var(--text-dim); }
.field > span:first-child { min-width: 74px; }
.field b { color: var(--text); min-width: 26px; }
.field input[type='range'] { flex: 1; max-width: 280px; }
.field.checkbox { align-items: flex-start; }
.field.checkbox em { display: block; font-style: normal; font-size: 11.5px; color: var(--text-faint); margin-top: 2px; }
.progress-wrap { display: flex; flex-direction: column; gap: 6px; }
.progress-bg { height: 8px; border-radius: 999px; background: var(--bg3); overflow: hidden; }
.progress-fill { height: 100%; background: var(--accent); transition: width 0.25s ease; }
.progress-wrap.indeterminate .progress-fill { animation: bg-convert-slide 1.2s ease-in-out infinite alternate; }
@keyframes bg-convert-slide { from { opacity: 0.35; } to { opacity: 1; } }
.progress-meta { font-size: 11.5px; color: var(--text-faint); }
.result { display: flex; flex-direction: column; gap: 10px; border-top: 1px solid var(--border); padding-top: 10px; }
</style>

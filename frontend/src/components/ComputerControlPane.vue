<template>
  <div class="computer-pane">
    <!-- run 状态与目标 + 控制操作（设计 16.2） -->
    <div class="cp-head">
      <span class="cp-dot" :class="dotClass"></span>
      <span class="cp-status">{{ statusText }}</span>
      <span class="cp-target">{{ targetLabel }}</span>
      <span class="cp-actions">
        <button
          v-if="store.computerControlPausable !== 'none'"
          class="chip-btn"
          data-test="computer-pause"
          :title="store.computerControlPausable === 'pause' ? '暂停：阻止下一个动作开始' : '恢复当前操控'"
          @click="togglePause"
        >
          <AppIcon :name="store.computerControlPausable === 'pause' ? 'pause' : 'play'" :size="12" :stroke-width="2" />
          {{ store.computerControlPausable === 'pause' ? '暂停' : '恢复' }}
        </button>
        <button
          v-if="store.computerRun.run_id && !store.computerRunTerminal"
          class="chip-btn danger"
          data-test="computer-stop"
          title="立即停止当前操控（快捷键 Ctrl+Alt+Esc）"
          @click="stop"
        >
          <AppIcon name="square" :size="12" :stroke-width="2" /> 立即停止
        </button>
      </span>
    </div>

    <template v-if="store.computerRun.run_id">
      <!-- 最新截图预览：loopback token URL，绝不经 evaluate_js 传 base64 -->
      <div v-if="store.computerRun.preview_url" class="cp-preview">
        <img data-test="computer-preview-image" :src="store.computerRun.preview_url" alt="电脑操控最新截图" />
      </div>

      <!-- run 摘要（mini-list，不塞整表） -->
      <ul class="cp-summary" data-test="computer-run-summary">
        <li><AppIcon name="chart" :size="12" /> 已执行 {{ store.computerRun.action_count }} 个动作</li>
        <li v-if="store.computerRun.model_name"><AppIcon name="brain" :size="12" /> {{ store.computerRun.model_name }}</li>
        <li v-if="surface"><AppIcon name="eye" :size="12" /> {{ surface }}</li>
      </ul>

      <!-- 登录/接管/Provider safety/失败原因（设计 16.2 第 4 项）：
           Provider safety 确认契约验证前只显示状态与停止，无“继续”入口 -->
      <div v-if="reasonText" class="cp-reason" :class="{ bad: reasonIsFailure }" data-test="computer-run-reason">
        <AppIcon name="triangle-alert" :size="13" />
        <span>{{ reasonText }}</span>
      </div>

      <!-- 动作明细不再在这里堆列表：每个动作已经以「点击 · 目标 · 耗时」的
           紧凑行长在对话区，点开还能看当帧截图。面板只保留控制与最新一张图，
           避免同一批动作在两处重复展示、列表越滚越长。 -->
    </template>

    <p v-else class="cp-empty" data-test="computer-empty-state">
      暂无电脑操控会话。{{ store.agentName }}开始操控浏览器或桌面时，这里会显示最新画面与运行控制，
      每个动作的明细与当帧截图在对话区逐行展示。
    </p>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue';
import { useAppStore } from '../stores/appStore';
import AppIcon from './AppIcon.vue';

const store = useAppStore();

const dotClass = computed(() => {
  const status = store.computerRun.status;
  if (status === 'running' || status === 'starting') return 'ok';
  if (status === 'failed') return 'bad';
  return 'warn';
});

const STATUS_TEXTS: Record<string, string> = {
  idle: '空闲',
  starting: '正在启动',
  running: '正在操控',
  paused: '已暂停',
  waiting_for_user: '等待用户',
  provider_safety_required: '等待安全确认',
  completing: '正在收尾',
  completed: '已完成',
  stopping: '正在停止',
  stopped: '已停止',
  failed: '操控失败',
};

const statusText = computed(() => STATUS_TEXTS[store.computerRun.status] || store.computerRun.status || '未知');

const targetLabel = computed(() => {
  const run = store.computerRun;
  if (run.target === 'desktop') return '桌面';
  const browser = String(run.browser || '').toLowerCase();
  if (browser === 'edge') return 'Edge';
  if (browser === 'chrome') return 'Chrome';
  return run.target === 'browser' ? '浏览器' : '';
});

const surface = computed(() => String(store.computerRun.current_surface || ''));

// 登录/接管/失败原因：paused 显示暂停原因，stopped 显示停止原因，
// failed / provider_safety_required 显示错误码
const reasonText = computed(() => {
  const run = store.computerRun;
  if (run.status === 'paused') return run.pause_reason ? `暂停原因：${run.pause_reason}` : '已暂停，等待恢复';
  // 接管态（waiting_for_user）是 2026-10-01 修复的一部分：回合正常收尾后已自动交还租约与浏览器连接，
  // 下一回合星瑶会重新接管同一页面继续操作，用户不必点“立即停止”，只需在对话里说一声即可。
  // 因此即使 pause_reason 为空也要给出这段交还指引，避免用户误以为必须手动停止。
  if (run.status === 'waiting_for_user') {
    const reasonPart = run.pause_reason ? `：${run.pause_reason}` : '';
    return `等待你接管${reasonPart} · 在右侧面板操作完成后，直接在对话里说一声，星瑶会重新接管继续。`;
  }
  if (run.status === 'stopped') return run.stop_reason ? `停止原因：${run.stop_reason}` : '本 run 已停止';
  if (run.status === 'provider_safety_required') {
    return run.error_code
      ? `Provider 强制安全检查（${run.error_code}）：确认契约验证前只可查看状态与停止`
      : 'Provider 强制安全检查：确认契约验证前只可查看状态与停止';
  }
  if (run.status === 'failed') {
    return run.error_code ? `失败原因：${run.error_code}` : '本 run 已失败';
  }
  return '';
});

const reasonIsFailure = computed(() => store.computerRun.status === 'failed');

async function togglePause() {
  if (store.computerControlPausable === 'resume') {
    await store.resumeComputerControl();
  } else if (store.computerControlPausable === 'pause') {
    await store.pauseComputerControl();
  }
}

async function stop() {
  await store.stopComputerControl();
}
</script>

<style scoped>
.computer-pane {
  display: flex;
  flex-direction: column;
  gap: 10px;
  font-size: 12.5px;
}
.cp-head {
  display: flex;
  align-items: center;
  gap: 8px;
  padding-bottom: 8px;
  border-bottom: 1px solid var(--border);
}
.cp-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  flex-shrink: 0;
  background: var(--yellow);
}
.cp-dot.ok {
  background: var(--green);
}
.cp-dot.bad {
  background: var(--red);
}
.cp-status {
  font-weight: 600;
  color: var(--text);
  white-space: nowrap;
}
.cp-target {
  padding: 1px 8px;
  border: 1px solid var(--border);
  border-radius: var(--theme-radius-small);
  background: var(--bg3);
  color: var(--text);
  white-space: nowrap;
  font-size: 11.5px;
}
.cp-actions {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-left: auto;
}
.cp-preview {
  border: 1px solid var(--border);
  border-radius: var(--theme-radius-medium);
  background: var(--code-bg);
  padding: 8px;
  overflow: hidden;
}
.cp-preview img {
  display: block;
  width: 100%;
  height: auto;
  border-radius: var(--theme-radius-small);
}
.cp-summary {
  margin: 0;
  padding: 0;
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: 6px;
  color: var(--text-dim);
  font-size: 12px;
}
.cp-summary li {
  display: flex;
  align-items: center;
  gap: 7px;
}
.cp-summary svg {
  color: var(--text-faint);
  flex-shrink: 0;
}
.cp-reason {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding: 10px 12px;
  border: 1px solid color-mix(in srgb, var(--yellow) 40%, transparent);
  border-radius: var(--theme-radius-medium);
  background: color-mix(in srgb, var(--yellow) 8%, transparent);
  color: var(--text-dim);
  font-size: 12px;
  line-height: 1.6;
}
.cp-reason.bad {
  border-color: color-mix(in srgb, var(--red) 40%, transparent);
  background: color-mix(in srgb, var(--red) 8%, transparent);
}
.cp-reason svg {
  flex-shrink: 0;
  margin-top: 2px;
  color: var(--yellow);
}
.cp-reason.bad svg {
  color: var(--red);
}
.cp-trace-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  padding-top: 2px;
}
.cp-trace-title {
  font-size: 12px;
  font-weight: 600;
  color: var(--text-dim);
}
.cp-trace-note {
  font-size: 11px;
  color: var(--text-faint);
}
.cp-trace {
  margin: 0;
  padding: 0;
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.cp-trace-row {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding: 7px 10px;
  border-radius: var(--theme-radius-medium);
  background: var(--bg2);
  border: 1px solid var(--border);
  font-size: 12px;
  line-height: 1.5;
}
.cp-trace-row.done .cp-row-ico {
  color: var(--green);
}
.cp-trace-row.failed {
  border-color: color-mix(in srgb, var(--red) 35%, transparent);
}
.cp-trace-row.failed .cp-row-ico {
  color: var(--red);
}
.cp-trace-row.running .cp-row-ico {
  color: var(--accent);
}
.cp-row-ico {
  flex-shrink: 0;
  display: inline-flex;
  margin-top: 1px;
}
.cp-row-text {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.cp-row-summary {
  color: var(--text);
  word-break: break-word;
}
.cp-row-error {
  font-family: var(--mono);
  font-size: 11px;
  color: var(--red);
}
.cp-row-duration {
  flex-shrink: 0;
  font-family: var(--mono);
  font-size: 11px;
  color: var(--text-faint);
}
.cp-trace-empty,
.cp-empty {
  margin: 0;
  padding: 12px 0;
  color: var(--text-faint);
  font-size: 12px;
  line-height: 1.7;
}

.chip-btn {
  background: var(--bg3);
  border: 1px solid var(--border);
  color: var(--text-dim);
  border-radius: 6px;
  padding: 4px 10px;
  font-size: 12px;
  cursor: pointer;
  transition: 0.15s;
}
.chip-btn:hover {
  border-color: var(--text-faint);
  color: var(--text);
}
.chip-btn.danger {
  color: var(--red);
}
.chip-btn.danger:hover {
  background: rgba(248, 81, 73, 0.15);
  border-color: var(--red);
}
</style>

<template>
  <div class="pet-settings" data-test="pet-settings-page">
    <header class="pet-header">
      <div>
        <h1>桌面宠物</h1>
        <p>桌宠是本机 Electron 应用：这里的开关会真正在桌面显示或隐藏它，星瑶退出时会一并关闭。</p>
      </div>
      <button class="refresh-button" type="button" title="重新读取桌宠状态" @click="refresh">
        <AppIcon name="refresh" :size="14" />
      </button>
    </header>

    <section class="pet-card">
      <label class="pet-toggle">
        <input
          data-test="pet-enabled"
          type="checkbox"
          :checked="enabled"
          :disabled="busy || loading || installing || !bridgeReady"
          @change="onToggleChange"
        />
        <span class="pet-toggle-text">显示桌宠</span>
        <span class="pet-toggle-state">{{ enabled ? '已开启' : '已关闭' }}</span>
      </label>

      <label class="pet-toggle pet-toggle-secondary">
        <input
          data-test="pet-notify-enabled"
          type="checkbox"
          :checked="notifyEnabled"
          :disabled="notifyBusy || loading || !bridgeReady"
          @change="onNotifyChange"
        />
        <span class="pet-toggle-text">任务提示</span>
        <span class="pet-toggle-state">{{ notifyEnabled ? '已开启' : '已关闭' }}</span>
      </label>
      <p class="pet-note pet-note-inline" data-test="pet-notify-note">
        星瑶执行任务时会冒泡，比如开工、翻文件、敲命令、搞定；关掉就只保留桌宠自己的日常台词。
      </p>

      <p class="pet-status" data-test="pet-status">
        <span class="pet-dot" :class="{ on: running && bridgeReady }"></span>
        <span :class="running && bridgeReady ? 'pet-state-on' : 'pet-state-off'">{{ statusText }}</span>
      </p>

      <p v-if="bridgeReady && !available" class="pet-warn" data-test="pet-unavailable">
        未找到 Electron 运行时或桌宠资源：运行时不再随安装包分发，第一次启用时要下载约 151 MB。
        <br />
        <span class="pet-note pet-warn-sub">
          运行时装在用户目录 ~/.ycode/pet-runtime，不占用安装包体积，装一次以后都不再下载。
          也可以在下方手动填写已有的 electron.exe 路径。
        </span>
        <button
          class="secondary-button pet-install-button"
          data-test="pet-install-runtime"
          type="button"
          :disabled="installing || loading || !bridgeReady"
          @click="ensureRuntimeAndEnable"
        >{{ installing ? '正在准备运行时…' : '下载并启用（约 151 MB）' }}</button>
      </p>

      <!-- 运行时安装进度：开关与上方按钮共用这一份状态，失败时原地给重试 -->
      <div
        v-if="bridgeReady && (installing || runtimeFailed)"
        class="pet-runtime"
        :class="{ 'pet-runtime-failed': runtimeFailed }"
        data-test="pet-runtime-progress"
      >
        <p class="pet-runtime-text" data-test="pet-runtime-text">{{ runtimeText || '正在准备运行时…' }}</p>
        <div
          v-if="runtimeShowProgress"
          class="pet-progress"
          role="progressbar"
          aria-label="Electron 运行时下载进度"
          aria-valuemin="0"
          aria-valuemax="100"
          :aria-valuenow="runtimePercent"
        >
          <span class="pet-progress-fill" :style="{ width: `${runtimePercent}%` }"></span>
        </div>
        <button
          v-if="runtimeFailed"
          class="secondary-button pet-runtime-retry"
          data-test="pet-runtime-retry"
          type="button"
          :disabled="installing"
          @click="ensureRuntimeAndEnable"
        >重试</button>
      </div>

      <label class="field-label" for="pet-electron-path">Electron 路径</label>
      <div class="pet-path-row">
        <input
          id="pet-electron-path"
          v-model="electronPath"
          data-test="pet-electron-path"
          class="text-field mono"
          type="text"
          placeholder="留空使用内置：pets/xingyao_pet/node_modules/electron/dist/electron.exe"
          :disabled="savingPath"
        />
        <button
          data-test="pet-path-save"
          class="secondary-button"
          type="button"
          :disabled="savingPath || !bridgeReady"
          @click="savePath"
        >{{ savingPath ? '保存中…' : '保存' }}</button>
      </div>

      <div class="pet-actions">
        <button
          data-test="pet-restart"
          class="primary-button"
          type="button"
          :disabled="busy || loading || !bridgeReady || !running"
          @click="restart"
        >重新打开桌宠</button>
      </div>

      <p v-if="message" data-test="pet-message" class="pet-message" :class="messageOk ? 'pet-ok' : 'pet-error'">{{ message }}</p>
      <p v-if="!bridgeReady" data-test="pet-native-only" class="pet-note">仅在桌面应用内可用：浏览器里打开设置页时无法控制桌宠。</p>
      <p class="pet-note">拖动宠物可以移动它；双击让它睡觉或唤醒；右键弹出菜单，菜单里可以退出桌宠。</p>
    </section>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue';

import AppIcon from './AppIcon.vue';
import type { PetMutationResult, PetRuntimeStatus, PetStatusDto } from '../types/pywebview';

// 桌宠状态：运行中（running/pid）、是否启用（enabled）、运行时是否可用（available）
const bridgeReady = ref(true);
const loading = ref(false);
const busy = ref(false);
const savingPath = ref(false);
const running = ref(false);
const pid = ref<number | null>(null);
const available = ref(true);
const enabled = ref(false);
// 任务提示：与「显示桌宠」互不影响，只存配置（桌宠没运行时也可切换）
const notifyEnabled = ref(true);
const notifyBusy = ref(false);
const electronPath = ref('');
const pendingText = ref('');
const message = ref('');
const messageOk = ref(true);

// Electron 运行时按需安装（运行时不再随安装包分发）：缺运行时时点开关或上方按钮
// 都走同一条支路，状态集中在这组 ref 上，绝不另造一套并行逻辑
const runtimeInstalled = ref(true); // 状态未读到时按「已装」处理，避免误判成需要安装
const runtimeState = ref('idle');
const runtimeMsg = ref('');
const runtimeDownloaded = ref(0);
const runtimeTotal = ref(0);
const runtimePercent = ref(0);
// installing：从「点下去」到「启用成功/失败」的整段（下载 / 校验 / 解压 / 启用）
const installing = ref(false);
const runtimeFailed = ref(false);
const RUNTIME_POLL_MS = 2000;
let runtimeTimer: ReturnType<typeof setInterval> | null = null;
// 组件卸载标志：卸载后轮询一律不再发请求（clearInterval 之外的兜底）
let disposed = false;

function formatMb(bytes: number): string {
  if (!bytes || bytes <= 0) return '0';
  return String(Math.max(1, Math.round(bytes / (1024 * 1024))));
}

// 运行时文案：state → 中文进度（failed 时直接显示后端 msg）
const runtimeText = computed(() => {
  if (runtimeFailed.value) return runtimeMsg.value || '运行时安装失败，请重试';
  if (!installing.value) return '';
  switch (runtimeState.value) {
    case 'downloading': {
      // total 还没拿到（刚起线程、等第一块字节）：先给「准备中」，避免刷出 0%（0 / 未知）
      if (runtimeTotal.value <= 0) return '正在准备运行时…';
      return `正在下载 Electron 运行时 ${runtimePercent.value}%（${formatMb(runtimeDownloaded.value)} / ${formatMb(runtimeTotal.value)} MB）`;
    }
    case 'verifying':
      return '校验安装包…';
    case 'extracting':
      return '正在解压…';
    case 'done':
      return '运行时已就绪，正在启用桌宠…';
    case 'failed':
      return runtimeMsg.value || '运行时安装失败，请重试';
    default:
      return '正在准备运行时…';
  }
});

// 进度条只在真的有字节进度时出现（idle / 解压阶段给文案即可）
const runtimeShowProgress = computed(() => {
  if (!installing.value || runtimeFailed.value) return false;
  return runtimeState.value === 'downloading';
});

// 缺运行时只看 available：后端 _resolve_electron 已经把「桌宠包内自带的运行时」和
// 「用户目录自动下载的那份」都算进去了。再叠一条 runtimeInstalled 判断，会让
// "包内已有运行时、只是没下载用户级副本"的机器白白触发一次 151MB 下载。
const needsRuntime = computed(() => !available.value);

const statusText = computed(() => {
  if (!bridgeReady.value) return '仅在桌面应用内可用';
  // 安装流程优先占住状态行，用户能一眼看到「正在准备运行时…」并跟随进度
  if (installing.value) return runtimeText.value || '正在准备运行时…';
  if (pendingText.value) return pendingText.value;
  if (loading.value) return '读取中…';
  if (running.value) return `运行中（PID ${pid.value ?? '未知'}）`;
  return '未运行';
});

// 桥方法缺失（浏览器环境或后端未就绪）时统一降级为「仅桌面应用内可用」，不抛错
function getApi() {
  return typeof window === 'undefined' ? undefined : window.pywebview?.api;
}

function report(ok: boolean, text: string) {
  messageOk.value = ok;
  message.value = text;
}

// 运行时快照：pet_get_status.runtime 与 pet_get_runtime_status.runtime 同一结构，
// 两处共用这一个入口，保证状态行与进度条永远来自同一份数据
function applyRuntime(runtime?: PetRuntimeStatus) {
  if (!runtime) return;
  if (typeof runtime.installed === 'boolean') runtimeInstalled.value = runtime.installed;
  if (typeof runtime.state === 'string' && runtime.state) runtimeState.value = runtime.state;
  runtimeMsg.value = typeof runtime.msg === 'string' ? runtime.msg : '';
  if (typeof runtime.downloaded === 'number') runtimeDownloaded.value = runtime.downloaded;
  if (typeof runtime.total === 'number') runtimeTotal.value = runtime.total;
  if (typeof runtime.percent === 'number') {
    runtimePercent.value = Math.max(0, Math.min(100, Math.round(runtime.percent)));
  }
  // 已经就绪时清掉上一轮的失败态（例如用户在别处装好了再刷新状态）
  if (!installing.value && (runtimeInstalled.value || runtimeState.value === 'done')) {
    runtimeFailed.value = false;
  }
}

// syncPath：只在读取状态时回填输入框，避免用户正在输入时被状态刷新覆盖
function applyStatus(status?: PetStatusDto, syncPath = false) {
  if (!status) return;
  running.value = Boolean(status.running);
  pid.value = typeof status.pid === 'number' ? status.pid : null;
  available.value = status.available !== false;
  // 安装流程进行中不动开关：此时 enabled 表达的是「用户意图」，别被中间态快照回滚
  if (!installing.value) {
    enabled.value = typeof status.enabled === 'boolean' ? status.enabled : running.value;
  }
  // pet_get_status 也带 runtime 字段：顺手同步，省一次额外请求
  applyRuntime(status.runtime);
  // 旧后端不带该字段时按开启处理，保持桌宠原有冒泡行为
  notifyEnabled.value = typeof status.notify_enabled === 'boolean' ? status.notify_enabled : true;
  if (syncPath && typeof status.electron_path === 'string') electronPath.value = status.electron_path;
}

async function refresh() {
  const api = getApi();
  if (!api || typeof api.pet_get_status !== 'function') {
    bridgeReady.value = false;
    return;
  }
  bridgeReady.value = true;
  loading.value = true;
  try {
    const status = await api.pet_get_status();
    applyStatus(status, true);
    if (status && status.ok === false && status.msg) report(false, status.msg);
  } catch {
    report(false, '读取桌宠状态失败');
  } finally {
    loading.value = false;
  }
}

function onToggleChange(event: Event) {
  const target = event.target as HTMLInputElement | null;
  const next = Boolean(target?.checked);
  // 缺运行时时直接调 pet_set_enabled 必然失败（后端只会回「未找到运行时」），
  // 所以把「打开」改成先装运行时、装好后自动启用；关闭仍走原来的路径
  if (next && needsRuntime.value) {
    void ensureRuntimeAndEnable();
    return;
  }
  void toggleEnabled(next);
}

async function toggleEnabled(next: boolean) {
  const api = getApi();
  if (!api || typeof api.pet_set_enabled !== 'function') {
    bridgeReady.value = false;
    return;
  }
  const previous = enabled.value;
  enabled.value = next;
  busy.value = true;
  pendingText.value = next ? '正在启动桌宠…' : '正在关闭桌宠…';
  message.value = '';
  try {
    const res: PetMutationResult | undefined = await api.pet_set_enabled(next);
    if (res?.status) applyStatus(res.status);
    else enabled.value = Boolean(res?.ok) ? next : previous;
    report(Boolean(res?.ok), res?.msg || (res?.ok ? (next ? '桌宠已启动' : '桌宠已关闭') : '操作失败'));
  } catch {
    enabled.value = previous;
    report(false, '操作失败：桌面桥接不可用');
  } finally {
    busy.value = false;
    pendingText.value = '';
  }
}

// ---------------------------------------------------------------------------
// 缺运行时 → 后台安装 → 自动启用（开关与「下载并启用」按钮共用这一条路径）
// ---------------------------------------------------------------------------

function stopPolling() {
  // 完成、失败、组件卸载三条路径都必须经过这里，保证定时器不泄漏
  if (runtimeTimer !== null) {
    clearInterval(runtimeTimer);
    runtimeTimer = null;
  }
}

function startPolling() {
  stopPolling();
  runtimeTimer = setInterval(() => {
    void pollRuntime();
  }, RUNTIME_POLL_MS);
}

// 安装失败统一出口：开关回到关闭，保留错误文案与重试按钮（下次点击重走安装）
function failRuntime(text: string) {
  stopPolling();
  installing.value = false;
  pendingText.value = '';
  enabled.value = false;
  runtimeFailed.value = true;
  runtimeMsg.value = text;
  // 错误统一显示在运行时提示块，避免和底部 message 区重复两遍
  message.value = '';
}

async function ensureRuntimeAndEnable() {
  if (installing.value) return; // 双击/重复点击直接吞掉
  const api = getApi();
  if (!api || typeof api.pet_install_runtime !== 'function') {
    if (!api) bridgeReady.value = false;
    else report(false, '当前版本不支持自动安装 Electron 运行时，请手动填写下方 electron.exe 路径。');
    return;
  }
  // 用户意图先行：开关视觉置开，失败再回落
  enabled.value = true;
  installing.value = true;
  runtimeFailed.value = false;
  runtimeState.value = 'idle';
  runtimeMsg.value = '';
  runtimePercent.value = 0;
  runtimeDownloaded.value = 0;
  runtimeTotal.value = 0;
  message.value = '';
  // pet_install_runtime 立刻返回、下载在后台线程，所以先起轮询再调用
  startPolling();
  try {
    const res = await api.pet_install_runtime();
    if (disposed) return;
    applyRuntime(res?.runtime);
    if (res && res.ok === false) {
      failRuntime(res.msg || '启动运行时安装失败');
      return;
    }
    // 幂等命中（之前就装好、或本次瞬间完成）：不必等轮询，直接启用
    if (runtimeInstalled.value) void enableAfterRuntime();
  } catch {
    if (disposed) return;
    failRuntime('启动运行时安装失败：桌面桥接不可用');
  }
}

async function pollRuntime() {
  // 卸载后一律不再发请求（onUnmounted 已 clearInterval，这里兜底正在飞行中的一次）
  if (disposed) {
    stopPolling();
    return;
  }
  const api = getApi();
  if (!api || typeof api.pet_get_runtime_status !== 'function') {
    failRuntime('当前版本不支持查询运行时状态，请手动填写 electron.exe 路径。');
    return;
  }
  try {
    const res = await api.pet_get_runtime_status();
    if (disposed) return;
    applyRuntime(res?.runtime);
    if (res && res.ok === false) {
      failRuntime(res.msg || '查询运行时安装进度失败');
      return;
    }
    if (!res?.runtime) return;
    if (runtimeInstalled.value || runtimeState.value === 'done') {
      void enableAfterRuntime();
      return;
    }
    if (runtimeState.value === 'failed') {
      failRuntime(runtimeMsg.value || '运行时安装失败，请重试');
    }
    // downloading / verifying / extracting / idle → 保持轮询，文案由 runtimeText 跟随
  } catch {
    if (disposed) return;
    failRuntime('查询运行时安装进度失败：桌面桥接不可用');
  }
}

// 运行时就绪后真正拉起桌宠；成功才算整段流程完成
async function enableAfterRuntime() {
  if (!installing.value) return; // 已经在启用阶段或已结束，避免重复调用
  stopPolling();
  runtimeState.value = 'done';
  pendingText.value = '运行时已就绪，正在启用桌宠…';
  const api = getApi();
  if (!api || typeof api.pet_set_enabled !== 'function') {
    if (!api) bridgeReady.value = false;
    failRuntime('启用桌宠失败：桌面桥接不可用');
    return;
  }
  try {
    const res: PetMutationResult | undefined = await api.pet_set_enabled(true);
    if (disposed) return;
    if (res?.status) applyStatus(res.status);
    else enabled.value = Boolean(res?.ok);
    if (res?.ok) {
      installing.value = false;
      pendingText.value = '';
      runtimeFailed.value = false;
      report(true, '桌宠已启动');
      // 运行时刚落盘：重新读一次真实快照，把 available / running 校准回来
      await refresh();
      return;
    }
    failRuntime(res?.msg || '运行时已就绪，但启用桌宠失败');
  } catch {
    if (disposed) return;
    failRuntime('启用桌宠失败：桌面桥接不可用');
  }
}

function onNotifyChange(event: Event) {
  const target = event.target as HTMLInputElement | null;
  void toggleNotify(Boolean(target?.checked));
}

// 任务提示开关：独立 busy（notifyBusy），不与「显示桌宠」的 busy 互相锁死
async function toggleNotify(next: boolean) {
  const api = getApi();
  if (!api || typeof api.pet_set_notify_enabled !== 'function') {
    bridgeReady.value = false;
    return;
  }
  const previous = notifyEnabled.value;
  notifyEnabled.value = next;
  notifyBusy.value = true;
  message.value = '';
  try {
    const res: PetMutationResult | undefined = await api.pet_set_notify_enabled(next);
    if (res?.status) applyStatus(res.status);
    else if (!res?.ok) notifyEnabled.value = previous;
    report(Boolean(res?.ok), res?.msg || (res?.ok ? (next ? '任务提示已开启' : '任务提示已关闭') : '操作失败'));
  } catch {
    notifyEnabled.value = previous;
    report(false, '操作失败：桌面桥接不可用');
  } finally {
    notifyBusy.value = false;
  }
}

async function savePath() {
  const api = getApi();
  if (!api || typeof api.pet_set_electron_path !== 'function') {
    bridgeReady.value = false;
    return;
  }
  savingPath.value = true;
  message.value = '';
  try {
    const res: PetMutationResult | undefined = await api.pet_set_electron_path(electronPath.value.trim());
    if (res?.status) applyStatus(res.status);
    report(Boolean(res?.ok), res?.msg || (res?.ok ? '已保存' : '保存失败'));
  } catch {
    report(false, '保存失败：桌面桥接不可用');
  } finally {
    savingPath.value = false;
  }
}

async function restart() {
  const api = getApi();
  if (!api || typeof api.pet_restart !== 'function') {
    bridgeReady.value = false;
    return;
  }
  busy.value = true;
  pendingText.value = '正在重新打开桌宠…';
  message.value = '';
  try {
    const res: PetMutationResult | undefined = await api.pet_restart();
    if (res?.status) applyStatus(res.status);
    report(Boolean(res?.ok), res?.msg || (res?.ok ? '桌宠已重新打开' : '重新打开失败'));
  } catch {
    report(false, '重新打开失败：桌面桥接不可用');
  } finally {
    busy.value = false;
    pendingText.value = '';
  }
}

onMounted(refresh);

// 组件卸载：停掉运行时轮询并置卸载标志，之后不再发任何请求
onUnmounted(() => {
  disposed = true;
  stopPolling();
});
</script>

<style scoped>
.pet-settings { max-width: 920px; margin: 0 auto; }
.pet-header { display: flex; align-items: flex-start; justify-content: space-between; gap: 18px; margin-bottom: 22px; }
.pet-header h1 { margin: 0 0 7px; font-size: 25px; }
.pet-header p { margin: 0; color: var(--text-faint); font-size: 13px; line-height: 1.7; }
.refresh-button { width: 32px; height: 32px; display: grid; place-items: center; border: 1px solid var(--border); border-radius: 8px; background: var(--bg2); color: var(--text-dim); flex: none; }
.refresh-button:hover { color: var(--text); background: var(--bg3); }
.pet-card { border-top: 1px solid var(--border); padding-top: 16px; }
.pet-toggle { display: flex; align-items: center; gap: 10px; color: var(--text); font-size: 13px; cursor: pointer; }
.pet-toggle input { width: 16px; height: 16px; accent-color: var(--accent); cursor: pointer; }
.pet-toggle input:disabled { cursor: not-allowed; opacity: .6; }
.pet-toggle-state { color: var(--text-faint); font-size: 12px; }
/* 任务提示开关：紧跟「显示桌宠」，与状态行之间留出更紧的说明间距 */
.pet-toggle-secondary { margin-top: 12px; }
.pet-note-inline { margin: 6px 0 0; }
.pet-status { display: flex; align-items: center; gap: 8px; margin: 14px 0 0; font-size: 12px; }
.pet-dot { width: 7px; height: 7px; border-radius: 50%; background: var(--text-faint); flex: none; }
.pet-dot.on { background: var(--green); }
.pet-state-on { color: var(--green); }
.pet-state-off { color: var(--text-faint); }
.pet-warn {
  margin: 12px 0 0;
  padding: 9px 13px;
  border: 1px solid color-mix(in srgb, var(--theme-warning) 35%, var(--border));
  border-radius: 9px;
  background: color-mix(in srgb, var(--theme-warning) 8%, var(--bg1));
  color: var(--theme-warning);
  font-size: 12px;
  line-height: 1.6;
}
.field-label { display: block; margin: 20px 0 7px; color: var(--text-dim); font-size: 12px; }
/* 运行时一键安装按钮：嵌在警告块里，比普通按钮更紧一号但仍显眼 */
.pet-install-button { display: inline-flex; align-items: center; min-height: 32px; margin: 9px 0 0; padding: 0 14px; font-size: 12px; font-weight: 600; color: var(--theme-warning); border-color: currentColor; background: var(--bg2); }
.pet-install-button:hover:not(:disabled) { background: var(--bg3); }
/* 运行时安装进度块：与警告块同一套视觉语言，失败时整块转危险色 */
.pet-runtime { margin: 12px 0 0; padding: 9px 13px; border: 1px solid var(--border); border-radius: 9px; background: var(--bg2); color: var(--text-dim); font-size: 12px; }
.pet-runtime-failed { border-color: color-mix(in srgb, var(--theme-danger) 35%, var(--border)); background: color-mix(in srgb, var(--theme-danger) 8%, var(--bg1)); color: var(--theme-danger); }
.pet-runtime-text { margin: 0; line-height: 1.6; }
.pet-progress { margin: 8px 0 0; height: 4px; border-radius: 999px; background: var(--bg3); overflow: hidden; }
.pet-progress-fill { display: block; height: 100%; border-radius: 999px; background: var(--accent); transition: width .2s linear; }
.pet-runtime-retry { min-height: 30px; margin: 9px 0 0; padding: 0 14px; font-size: 12px; }
.pet-path-row { display: flex; align-items: center; gap: 10px; }
.text-field { flex: 1; min-width: 0; min-height: 42px; padding: 0 13px; box-sizing: border-box; border: 1px solid var(--border); border-radius: 9px; background: var(--bg2); color: var(--text); font-size: 13px; }
.text-field:focus { outline: none; border-color: var(--accent); box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent) 15%, transparent); }
.text-field:disabled { cursor: wait; opacity: .7; }
.mono { font-family: var(--mono); }
.primary-button, .secondary-button { min-height: 38px; margin-top: 0; padding: 0 18px; border-radius: 9px; border: 1px solid var(--border); font-weight: 600; cursor: pointer; flex: none; }
.primary-button { background: var(--text); color: var(--bg0); }
.primary-button:disabled, .secondary-button:disabled { opacity: .5; cursor: not-allowed; }
.secondary-button { background: var(--bg3); color: var(--text); }
.pet-actions { display: flex; gap: 10px; margin-top: 14px; }
.pet-message { margin: 14px 0 0; font-size: 12px; }
.pet-ok { color: var(--green); }
.pet-error { color: var(--theme-danger); }
.pet-note { margin: 14px 0 0; color: var(--text-faint); font-size: 12px; line-height: 1.7; }

@media (max-width: 768px) {
  .pet-header { flex-direction: column; }
  .pet-path-row { flex-wrap: wrap; }
  .pet-path-row .secondary-button { margin-top: 8px; }
  .pet-actions { flex-wrap: wrap; }
}
</style>

<template>
  <div class="relay-settings" data-test="cloud-relay-settings">
    <header class="relay-header">
      <div class="relay-head-text">
        <h1>云中转</h1>
        <p>
          手机借你自己的服务器操作这台电脑。默认关闭；打开后本机主动外连，
          <b>不需要公网 IP，也不用做端口映射</b>。
        </p>
      </div>
      <button
        class="relay-icon-btn"
        type="button"
        title="重新读取状态"
        data-test="cloud-relay-refresh"
        @click="load"
      >
        <AppIcon name="refresh" :size="14" />
      </button>
    </header>

    <section class="relay-status" :class="`is-${state}`" data-test="cloud-relay-status">
      <span class="dot" :class="{ pending: isPending }" />
      <span class="label">{{ stateLabel }}</span>
      <span class="hint">{{ stateHint }}</span>
      <span v-if="showMetrics" class="metrics mono">
        排队 {{ stats.queued }} · 丢弃 {{ stats.dropped }} · 丢失 {{ stats.lost }}
      </span>
    </section>

    <section class="relay-card">
      <label class="switch-row">
        <input
          v-model="enabled"
          data-test="cloud-relay-enabled"
          type="checkbox"
          :disabled="busy"
        />
        <span>
          <b>启用云中转</b>
          <small>关闭时这台电脑不建立任何外连，和没配过完全一样。</small>
        </span>
      </label>

      <div class="relay-divider" />

      <label class="field-label" for="relay-url">网关地址</label>
      <input
        id="relay-url"
        v-model="url"
        data-test="cloud-relay-url"
        class="text-field mono"
        type="text"
        placeholder="wss://example.com/relay/device"
        :disabled="busy"
        spellcheck="false"
      />
      <p class="field-hint">
        必须带 <code>wss://</code> 或 <code>ws://</code>；走公网用 <code>wss://</code>，
        明文 <code>ws://</code> 只适合本机隧道。
      </p>

      <label class="field-label" for="relay-device">设备编号</label>
      <input
        id="relay-device"
        v-model="deviceId"
        data-test="cloud-relay-device-id"
        class="text-field mono"
        type="text"
        placeholder="服务器创建这台设备时给出的编号"
        :disabled="busy"
        spellcheck="false"
      />

      <label class="field-label" for="relay-token">设备令牌</label>
      <div class="secret-field">
        <input
          id="relay-token"
          v-model="token"
          data-test="cloud-relay-token"
          class="text-field mono"
          :type="showToken ? 'text' : 'password'"
          :placeholder="hasToken ? '已配置；留空表示不修改' : '粘贴服务器给出的设备令牌'"
          :disabled="busy"
          spellcheck="false"
        />
        <button
          class="relay-btn ghost"
          type="button"
          data-test="cloud-relay-toggle-token"
          :disabled="busy"
          @click="showToken = !showToken"
        >{{ showToken ? '隐藏' : '显示' }}</button>
      </div>
      <p class="field-hint">
        <span class="token-state" :class="{ ok: hasToken }">{{ hasToken ? '已配置' : '未配置' }}</span>
        令牌加密存在本机（Windows 用 DPAPI），界面和日志都不回显明文。
        三项要和服务器上同一台设备对得上，对不上只会一直连不通。
      </p>

      <div class="relay-actions">
        <button
          class="relay-btn primary"
          type="button"
          data-test="cloud-relay-save"
          :disabled="busy"
          @click="save"
        >{{ busy ? '处理中…' : '保存并连接' }}</button>
        <button
          class="relay-btn secondary"
          type="button"
          data-test="cloud-relay-reconnect"
          :disabled="busy || !enabled"
          title="不改配置，丢掉当前连接按原配置重拨"
          @click="reconnect"
        >重拨一次</button>
        <button
          v-if="isLive"
          class="relay-btn danger"
          type="button"
          data-test="cloud-relay-disconnect"
          :disabled="busy"
          @click="disconnect"
        >断开</button>
      </div>

      <p
        v-if="message"
        class="relay-message"
        :class="{ bad: messageBad }"
        data-test="cloud-relay-message"
      >{{ message }}</p>
    </section>

    <section class="relay-steps" data-test="cloud-relay-checklist">
      <h2>配置检查</h2>
      <ul>
        <li v-for="item in checklist" :key="item.label" :class="{ ok: item.ok }">
          <span class="mark" />
          <span class="what">{{ item.label }}</span>
          <span class="how">{{ item.hint }}</span>
        </li>
      </ul>
    </section>

    <section class="relay-help">
      <button
        class="help-toggle"
        type="button"
        data-test="cloud-relay-guide-toggle"
        @click="showGuide = !showGuide"
      >
        <span>连不上时按这个顺序查</span>
        <span class="chev" :class="{ open: showGuide }">›</span>
      </button>
      <div v-show="showGuide" class="help-body" data-test="cloud-relay-guide">
        <ol>
          <li>
            <b>状态条停在「未启用」</b>：开关没勾上，或者地址 / 编号 / 令牌缺一项。
            配置不完整时本机不会拿半个配置去连，状态就是 disabled。
          </li>
          <li>
            <b>一直是「连接中」</b>：先确认服务器端 relay 服务在跑、端口通。
            本机可先用浏览器或 <code>curl</code> 试一下网关的 health 路径。
          </li>
          <li>
            <b>「连接异常」</b>：多半是设备编号和令牌不是同一台设备的。
            到服务器端这台设备上重新取一次，两项一起换。
          </li>
          <li>
            <b>连上了但手机没反应</b>：手机端绑的是同一个设备编号吗？
            云中转只把入口转过来，权限仍由本机「权限与沙盒」控制。
          </li>
          <li>
            <b>换令牌</b>：令牌框留空表示不改；要换就粘贴新的再保存一次，旧令牌立即失效。
          </li>
        </ol>
        <p class="help-note">
          这里只加外连通道，不放宽任何权限：手机能做的事，仍然受本机权限档位和审批限制。
        </p>
      </div>
    </section>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue';
import AppIcon from './AppIcon.vue';

type RelaySettings = {
  enabled?: boolean;
  url?: string;
  device_id?: string;
  has_token?: boolean;
  state?: string;
  connected?: boolean;
  generation?: number;
  queued?: number;
  dropped?: number;
  lost?: number;
};

type RelayResult = { ok?: boolean; msg?: string; settings?: RelaySettings };

type BridgeApi = {
  get_cloud_relay_settings?: () => Promise<RelayResult>;
  save_cloud_relay_settings?: (payload: {
    enabled: boolean;
    url: string;
    device_id: string;
    token: string;
  }) => Promise<RelayResult>;
  cloud_relay_connect?: () => Promise<RelayResult>;
  cloud_relay_disconnect?: () => Promise<RelayResult>;
};

const enabled = ref(false);
const url = ref('');
const deviceId = ref('');
const token = ref('');
const hasToken = ref(false);
const state = ref('disabled');
const stats = ref({ queued: 0, dropped: 0, lost: 0 });
const showToken = ref(false);
const showGuide = ref(false);
const busy = ref(false);
const message = ref('');
const messageBad = ref(false);

const STATE_LABELS: Record<string, string> = {
  disabled: '未启用',
  connecting: '连接中…',
  reconnecting: '重连中…',
  connected: '已连接',
  error: '连接异常',
  stopped: '已断开',
};

const STATE_HINTS: Record<string, string> = {
  disabled: '开关关着，或配置不完整，本机没有外连',
  connecting: '正在和网关握手，稍等几秒',
  reconnecting: '连接掉了，正在退避重试',
  connected: '网关已接上，手机可以接管',
  error: '上次连接失败，看下面的排障',
  stopped: '已主动断开，配置还在，下次启动会重连',
};

const stateLabel = computed(() => STATE_LABELS[state.value] || state.value);
const stateHint = computed(() => STATE_HINTS[state.value] || '');
const isPending = computed(() => state.value === 'connecting' || state.value === 'reconnecting');
const isLive = computed(() => isPending.value || state.value === 'connected');
const showMetrics = computed(
  () => isLive.value || stats.value.dropped > 0 || stats.value.lost > 0,
);
const urlLooksValid = computed(() => /^wss?:\/\//i.test(url.value.trim()));

/** 配置检查清单：五步走完才算真的接通，避免「填了但没启用」这种半成品状态 */
const checklist = computed(() => [
  {
    label: '网关地址已填写',
    ok: !!url.value.trim() && urlLooksValid.value,
    hint: !url.value.trim()
      ? '来自服务器部署时给你的地址'
      : urlLooksValid.value
        ? ''
        : '要以 ws:// 或 wss:// 开头',
  },
  {
    label: '设备编号已填写',
    ok: !!deviceId.value.trim(),
    hint: deviceId.value.trim() ? '' : '服务器创建这台设备时给出',
  },
  {
    label: '设备令牌已配置',
    ok: hasToken.value,
    hint: hasToken.value ? '已加密保存，不回显明文' : '粘贴令牌后点「保存并连接」',
  },
  {
    label: '云中转已启用',
    ok: enabled.value,
    hint: enabled.value ? '' : '勾选上面的开关',
  },
  {
    label: '已连上网关',
    ok: state.value === 'connected',
    hint: state.value === 'connected' ? '' : `当前：${stateLabel.value}`,
  },
]);

function api(): BridgeApi | undefined {
  return (window as unknown as { pywebview?: { api?: BridgeApi } }).pywebview?.api;
}

/** 只同步连接状态与统计，不动用户正在编辑的输入框（轮询时用） */
function applyStatus(settings?: RelaySettings | null) {
  if (!settings) return;
  hasToken.value = Boolean(settings.has_token);
  state.value = settings.state || 'disabled';
  stats.value = {
    queued: Number(settings.queued || 0),
    dropped: Number(settings.dropped || 0),
    lost: Number(settings.lost || 0),
  };
}

async function load() {
  const result = await api()?.get_cloud_relay_settings?.();
  if (!result?.ok) {
    messageBad.value = true;
    message.value = `读取失败：${result?.msg || '桌面桥接不可用'}`;
    return;
  }
  applyStatus(result.settings);
  const settings = result.settings;
  if (!settings) return;
  enabled.value = Boolean(settings.enabled);
  url.value = settings.url || '';
  deviceId.value = settings.device_id || '';
  token.value = '';
}

async function refreshStatus() {
  if (document.visibilityState === 'hidden') return;
  const result = await api()?.get_cloud_relay_settings?.();
  if (result?.ok) applyStatus(result.settings);
}

function report(result: RelayResult | undefined, okText: string) {
  if (!result) {
    messageBad.value = true;
    message.value = '桌面桥接不可用，这次操作没生效';
    return;
  }
  applyStatus(result.settings);
  if (!result.ok) {
    messageBad.value = true;
    message.value = `操作失败：${result.msg || '未知原因'}`;
    return;
  }
  const incomplete = enabled.value && state.value === 'disabled';
  messageBad.value = incomplete;
  message.value = incomplete
    ? '已保存。但配置不完整（地址 / 编号 / 令牌缺一项），本机没有建立连接。'
    : okText;
}

async function save() {
  busy.value = true;
  message.value = '';
  try {
    const result = await api()?.save_cloud_relay_settings?.({
      enabled: enabled.value,
      url: url.value.trim(),
      device_id: deviceId.value.trim(),
      token: token.value.trim(),
    });
    token.value = '';
    report(
      result,
      enabled.value ? '已保存，正在按新配置连接。' : '已保存。开关关闭，这台电脑不会外连。',
    );
  } finally {
    busy.value = false;
  }
}

async function reconnect() {
  busy.value = true;
  message.value = '';
  try {
    report(await api()?.cloud_relay_connect?.(), '已按当前配置重拨。');
  } finally {
    busy.value = false;
  }
}

async function disconnect() {
  busy.value = true;
  message.value = '';
  try {
    report(await api()?.cloud_relay_disconnect?.(), '已断开。配置保留，下次启动会重连。');
  } finally {
    busy.value = false;
  }
}

let timer: ReturnType<typeof setInterval> | undefined;

onMounted(() => {
  load();
  timer = setInterval(refreshStatus, 4000);
});

onUnmounted(() => {
  if (timer) clearInterval(timer);
});
</script>

<style scoped>
.relay-settings {
  display: flex;
  flex-direction: column;
  gap: 14px;
  max-width: 720px;
}

/* ---------- 头部 ---------- */
.relay-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
}
.relay-head-text h1 {
  margin: 0 0 5px;
  font-size: 17px;
  font-weight: 600;
}
.relay-head-text p {
  margin: 0;
  max-width: 62ch;
  font-size: 12.5px;
  line-height: 1.7;
  color: var(--text-dim);
}
.relay-head-text b {
  color: var(--text);
  font-weight: 600;
}
.relay-icon-btn {
  flex: none;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 30px;
  height: 30px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--bg2);
  color: var(--text-dim);
  cursor: pointer;
  transition: color 0.15s, border-color 0.15s;
}
.relay-icon-btn:hover {
  color: var(--text);
  border-color: var(--accent);
}

/* ---------- 状态条 ---------- */
.relay-status {
  display: flex;
  align-items: center;
  gap: 9px;
  padding: 10px 13px;
  border: 1px solid var(--border);
  border-radius: 10px;
  background: var(--bg2);
  font-size: 12.5px;
  transition: border-color 0.2s, background 0.2s;
}
.relay-status .dot {
  flex: none;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--text-faint);
}
.relay-status .label {
  flex: none;
  font-weight: 600;
  color: var(--text);
}
.relay-status .hint {
  color: var(--text-dim);
  font-size: 12px;
}
.relay-status .metrics {
  margin-left: auto;
  flex: none;
  color: var(--text-faint);
  font-size: 11.5px;
}
.relay-status.is-connected {
  border-color: color-mix(in srgb, var(--green) 42%, var(--border));
  background: color-mix(in srgb, var(--green) 7%, var(--bg2));
}
.relay-status.is-connected .dot {
  background: var(--green);
}
.relay-status.is-connecting,
.relay-status.is-reconnecting {
  border-color: color-mix(in srgb, var(--yellow) 42%, var(--border));
}
.relay-status.is-connecting .dot,
.relay-status.is-reconnecting .dot {
  background: var(--yellow);
}
.relay-status.is-error {
  border-color: color-mix(in srgb, var(--red) 45%, var(--border));
  background: color-mix(in srgb, var(--red) 7%, var(--bg2));
}
.relay-status.is-error .dot {
  background: var(--red);
}
.dot.pending {
  animation: relay-pulse 1.6s ease-in-out infinite;
}
@keyframes relay-pulse {
  0%,
  100% {
    opacity: 1;
    transform: scale(1);
  }
  50% {
    opacity: 0.3;
    transform: scale(0.78);
  }
}

/* ---------- 主卡片 ---------- */
.relay-card {
  display: flex;
  flex-direction: column;
  padding: 15px;
  border: 1px solid var(--border);
  border-radius: 12px;
  background: var(--bg2);
}
.switch-row {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  cursor: pointer;
}
.switch-row input {
  flex: none;
  margin-top: 2px;
  width: 15px;
  height: 15px;
  accent-color: var(--accent);
  cursor: pointer;
}
.switch-row b {
  display: block;
  font-size: 13px;
  font-weight: 600;
  color: var(--text);
}
.switch-row small {
  display: block;
  margin-top: 3px;
  font-size: 11.5px;
  line-height: 1.55;
  color: var(--text-faint);
}
.relay-divider {
  height: 1px;
  margin: 14px 0 2px;
  background: var(--border);
}

.field-label {
  margin-top: 12px;
  font-size: 12px;
  font-weight: 500;
  color: var(--text-dim);
}
.text-field {
  min-height: 38px;
  margin-top: 6px;
  padding: 0 12px;
  border: 1px solid var(--border);
  border-radius: 9px;
  background: var(--bg0);
  color: var(--text);
  font-size: 13px;
  transition: border-color 0.15s, box-shadow 0.15s;
}
.text-field:focus {
  outline: none;
  border-color: var(--accent);
  box-shadow: 0 0 0 3px var(--accent-soft);
}
.text-field::placeholder {
  color: var(--text-faint);
}
.text-field:disabled {
  opacity: 0.55;
}
.mono {
  font-family: var(--mono);
}
.secret-field {
  display: flex;
  align-items: center;
  gap: 8px;
}
.secret-field .text-field {
  flex: 1;
  min-width: 0;
}

.field-hint {
  margin: 6px 0 0;
  font-size: 11.5px;
  line-height: 1.65;
  color: var(--text-faint);
}
.field-hint code {
  padding: 1px 5px;
  border-radius: 4px;
  background: var(--bg1);
  font-family: var(--mono);
  font-size: 11px;
  color: var(--text-dim);
}
.token-state {
  display: inline-block;
  margin-right: 6px;
  padding: 1px 7px;
  border: 1px solid var(--border);
  border-radius: 999px;
  font-size: 11px;
  color: var(--text-faint);
}
.token-state.ok {
  border-color: color-mix(in srgb, var(--green) 45%, var(--border));
  color: var(--green);
}

/* ---------- 按钮 ---------- */
.relay-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 16px;
}
.relay-btn {
  min-height: 36px;
  padding: 0 16px;
  border-radius: 9px;
  font-size: 12.5px;
  font-weight: 500;
  cursor: pointer;
  transition: background 0.15s, border-color 0.15s, color 0.15s, opacity 0.15s;
}
.relay-btn:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}
.relay-btn.primary {
  border: none;
  background: var(--accent);
  color: #fff;
  font-weight: 600;
}
.relay-btn.primary:not(:disabled):hover {
  background: color-mix(in srgb, var(--accent) 86%, #fff);
}
.relay-btn.secondary {
  border: 1px solid var(--border);
  background: var(--bg1);
  color: var(--text-dim);
}
.relay-btn.secondary:not(:disabled):hover {
  border-color: var(--accent);
  color: var(--text);
}
.relay-btn.ghost {
  flex: none;
  min-height: 38px;
  padding: 0 12px;
  border: 1px solid var(--border);
  background: transparent;
  color: var(--text-dim);
}
.relay-btn.ghost:not(:disabled):hover {
  color: var(--text);
}
.relay-btn.danger {
  border: 1px solid color-mix(in srgb, var(--red) 40%, var(--border));
  background: transparent;
  color: var(--red);
}
.relay-btn.danger:not(:disabled):hover {
  background: color-mix(in srgb, var(--red) 10%, transparent);
}

.relay-message {
  margin: 10px 0 0;
  font-size: 12px;
  line-height: 1.6;
  color: var(--green);
}
.relay-message.bad {
  color: var(--red);
}

/* ---------- 检查清单 ---------- */
.relay-steps {
  padding: 13px 15px;
  border: 1px solid var(--border);
  border-radius: 12px;
  background: var(--bg2);
}
.relay-steps h2 {
  margin: 0 0 9px;
  font-size: 12.5px;
  font-weight: 600;
  color: var(--text);
}
.relay-steps ul {
  display: flex;
  flex-direction: column;
  gap: 7px;
  margin: 0;
  padding: 0;
  list-style: none;
}
.relay-steps li {
  display: flex;
  align-items: baseline;
  gap: 8px;
  font-size: 12.5px;
  color: var(--text-dim);
}
.relay-steps li .mark {
  position: relative;
  flex: none;
  top: 1px;
  width: 13px;
  height: 13px;
  border: 1px solid var(--border);
  border-radius: 4px;
}
.relay-steps li.ok .mark {
  border-color: var(--green);
  background: color-mix(in srgb, var(--green) 18%, transparent);
}
.relay-steps li.ok .mark::after {
  content: '';
  position: absolute;
  left: 4px;
  top: 1px;
  width: 3px;
  height: 7px;
  border: solid var(--green);
  border-width: 0 1.5px 1.5px 0;
  transform: rotate(40deg);
}
.relay-steps li.ok .what {
  color: var(--text);
}
.relay-steps li .how {
  margin-left: auto;
  max-width: 58%;
  text-align: right;
  font-size: 11.5px;
  color: var(--text-faint);
}

/* ---------- 排障 ---------- */
.relay-help {
  padding: 0 15px 14px;
  border: 1px solid var(--border);
  border-radius: 12px;
  background: var(--bg2);
}
.help-toggle {
  display: flex;
  align-items: center;
  justify-content: space-between;
  width: 100%;
  padding: 13px 0;
  border: none;
  background: none;
  color: var(--text);
  font-size: 13px;
  font-weight: 600;
  text-align: left;
  cursor: pointer;
}
.help-toggle .chev {
  color: var(--text-faint);
  font-size: 16px;
  transition: transform 0.15s;
}
.help-toggle .chev.open {
  transform: rotate(90deg);
}
.help-body ol {
  margin: 0;
  padding-left: 18px;
}
.help-body li {
  margin-bottom: 8px;
  font-size: 12.5px;
  line-height: 1.75;
  color: var(--text-dim);
}
.help-body li b {
  color: var(--text);
  font-weight: 600;
}
.help-body code {
  padding: 1px 5px;
  border-radius: 4px;
  background: var(--bg1);
  font-family: var(--mono);
  font-size: 11.5px;
}
.help-note {
  margin: 10px 0 0;
  padding-top: 10px;
  border-top: 1px solid var(--border);
  font-size: 11.5px;
  line-height: 1.65;
  color: var(--text-faint);
}
</style>

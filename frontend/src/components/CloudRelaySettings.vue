<template>
  <div class="relay-settings" data-test="cloud-relay-settings">
    <header class="relay-header">
      <div class="relay-head-text">
        <h1>云中转</h1>
        <p>通过中转服务器远程连接手机端（主动外连，无需公网 IP）。</p>
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
          @change="onToggleEnabled"
        />
        <span>
          <b>连接远程（云中转）</b>
          <small>开启后主动连接中转服务器供手机端接入。</small>
        </span>
      </label>

      <!-- 一键让星瑶自动部署到服务器 -->
      <div class="deploy-box" data-test="cloud-relay-deploy-card">
        <div class="deploy-head">
          <div>
            <b>还没有配置服务器？让星瑶自动部署</b>
            <p>填写服务器信息即可让星瑶自动部署（也可留空在对话中确认）。</p>
          </div>
        </div>
        <div class="deploy-grid">
          <input
            v-model="deployHost"
            data-test="deploy-host"
            class="text-field mono"
            type="text"
            placeholder="服务器 IP 或域名（可选，如 129.204.50.227）"
            :disabled="busy"
            spellcheck="false"
          />
          <input
            v-model="deployUser"
            data-test="deploy-user"
            class="text-field mono"
            type="text"
            placeholder="SSH 用户（默认 ubuntu / root）"
            :disabled="busy"
            spellcheck="false"
          />
          <input
            v-model="deployDomain"
            data-test="deploy-domain"
            class="text-field mono"
            type="text"
            placeholder="公网域名（可选，如 example.com）"
            :disabled="busy"
            spellcheck="false"
          />
        </div>
        <div class="deploy-actions">
          <button
            class="relay-btn primary"
            type="button"
            data-test="cloud-relay-auto-deploy"
            :disabled="busy"
            @click="startAutoDeploy"
          >星瑶自动部署到服务器</button>
          <button
            class="relay-btn secondary"
            type="button"
            data-test="cloud-relay-copy-prompt"
            :disabled="busy"
            @click="copyDeployPrompt"
          >复制部署提示词</button>
        </div>
      </div>

      <div class="relay-divider" />

      <button
        class="manual-toggle"
        type="button"
        data-test="cloud-relay-manual-toggle"
        @click="showManual = !showManual"
      >
        <span>手动配置 / 高级设置</span>
        <span class="chev" :class="{ open: showManual }">›</span>
      </button>

      <div v-show="showManual" class="manual-body">
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
        <p class="field-hint">公网建议使用 <code>wss://</code>。</p>

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
          令牌加密保存；需与服务端设备配置一致。
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
import { computed, nextTick, onMounted, onUnmounted, ref } from 'vue';
import AppIcon from './AppIcon.vue';
import { useAppStore } from '../stores/appStore';

const store = useAppStore();

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
const showManual = ref(false);
const deployHost = ref('');
const deployUser = ref('');
const deployDomain = ref('');
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
const hasConfig = computed(
  () => Boolean(url.value.trim() && urlLooksValid.value && deviceId.value.trim() && (hasToken.value || token.value.trim())),
);

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
  showManual.value = Boolean(settings.url || settings.device_id || settings.has_token);
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

async function onToggleEnabled() {
  if (enabled.value) {
    if (!hasConfig.value) {
      messageBad.value = true;
      message.value = '尚未配置远程服务器：请点击下方「星瑶自动部署到服务器」，或展开「手动配置」填写网关信息。';
      await nextTick();
      enabled.value = false;
      return;
    }
    await save();
    return;
  }
  await save();
}

function buildDeployPrompt(): string {
  const host = deployHost.value.trim() || '（待确认：请先向我询问服务器公网 IP）';
  const user = deployUser.value.trim() || 'ubuntu（若不同请向我确认）';
  const domain = deployDomain.value.trim() || '（可选：若有域名请配置 HTTPS/WSS，无域名则先向我确认）';
  return [
    '请帮我把当前项目的云中转服务（relay_server）自动部署到我的 Linux 服务器，并完成本机 Y-code 的远程连接配置，让我能直接手机公网扫码使用。',
    '',
    '服务器信息：',
    `- 服务器地址：${host}`,
    `- SSH 用户：${user}`,
    `- 公网域名：${domain}`,
    '',
    '请按以下步骤执行（注意：用户可能是通过安装包运行，本机不一定有源码或 Node/npm 环境）：',
    '1. 如果缺少服务器 IP、SSH 登录方式（密钥或密码）或域名信息，先向我确认后再动手；',
    '2. 定位服务端代码与静态资源（优先从当前程序运行目录 / 安装包内 `_internal/` 下的 `relay_server/`、`requirements-relay.txt`、`frontend-mobile/dist`、`frontend/dist`、`frontend/mobile-shim.js` 直接读取，无需源码或重新构建；若在源码目录且存在 `relay_server/deploy/README.md` 也可按其构建）；',
    '3. 通过 SSH/SCP 将 `relay_server/`、`requirements-relay.txt`、移动端/桌面端静态资源及 `mobile-shim.js` 上传到服务器 `/srv/relay`；',
    '4. 在服务器配置 Python venv、安装依赖、生成 `/etc/relay/jwt_secret` 与 `/etc/relay/relay.env`（监听 `127.0.0.1:18080`），安装并启动 systemd 服务 `relay.service`；',
    '5. 配置 Nginx 反向代理（包含 `/m/`、`/desktop/`、`/relay/` 的 WebSocket Upgrade 与资源路径映射）和 HTTPS/WSS 证书；',
    '6. 在服务器执行 `python -m relay_server create-device` 创建本机设备，拿到 `device_id` 与 `token`；',
    '7. 把生成的 `wss://.../relay/device`、`device_id` 和 `token` 写入本机 Y-code 配置（`~/.ycode/config.json`，`token` 需按 `core.credentials.protect_secret` 加密或调用 `save_cloud_relay_settings`）并启用云中转，确认连接状态为 `connected` 后告诉我结果。',
  ].join('\n');
}

function startAutoDeploy() {
  const prompt = buildDeployPrompt();
  store.settingsOpen = false;
  void store.sendMessage(prompt);
}

async function copyDeployPrompt() {
  const prompt = buildDeployPrompt();
  try {
    await navigator.clipboard.writeText(prompt);
    messageBad.value = false;
    message.value = '已复制部署提示词，粘贴发送给星瑶即可开始自动部署。';
  } catch {
    messageBad.value = true;
    message.value = '复制失败，请直接点击「星瑶自动部署到服务器」。';
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
  gap: 16px;
  max-width: 860px;
  margin: 0 auto;
}

/* ---------- 头部 ---------- */
.relay-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 4px;
}
.relay-head-text h1 {
  margin: 0 0 6px;
  font-size: 25px;
}
.relay-head-text p {
  margin: 0;
  font-size: 13px;
  line-height: 1.5;
  color: var(--text-faint);
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

/* ---------- 一键部署引导 & 折叠手动配置 ---------- */
.deploy-box {
  margin-top: 12px;
  padding: 13px 14px;
  border-radius: 10px;
  border: 1px solid var(--border);
  background: var(--bg1);
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.deploy-head b {
  display: block;
  font-size: 13.5px;
  margin-bottom: 3px;
}
.deploy-head p {
  margin: 0;
  font-size: 12px;
  line-height: 1.55;
  color: var(--text-dim);
}
.deploy-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 8px;
}
@media (max-width: 640px) {
  .deploy-grid {
    grid-template-columns: 1fr;
  }
}
.deploy-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
.manual-toggle {
  display: flex;
  align-items: center;
  justify-content: space-between;
  width: 100%;
  padding: 6px 0;
  border: none;
  background: none;
  color: var(--text-dim);
  font-size: 12.5px;
  font-weight: 600;
  cursor: pointer;
}
.manual-toggle:hover {
  color: var(--text);
}
.manual-toggle .chev {
  color: var(--text-faint);
  font-size: 16px;
  transition: transform 0.15s;
}
.manual-toggle .chev.open {
  transform: rotate(90deg);
}
.manual-body {
  display: flex;
  flex-direction: column;
  padding-top: 6px;
}
</style>

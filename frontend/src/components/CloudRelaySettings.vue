<template>
  <div class="relay-settings" data-test="cloud-relay-settings">
    <header class="page-header">
      <div class="relay-head-text">
        <h1>云中转</h1>
        <p>通过中转服务器远程连接手机端（主动外连，无需公网 IP）。</p>
      </div>
      <div class="header-actions">
        <button
          class="refresh-button"
          type="button"
          title="重新读取状态"
          data-test="cloud-relay-refresh"
          @click="load"
        >
          <AppIcon name="refresh" :size="14" />
        </button>
      </div>
    </header>

    <!-- Hero 总控与连接状态卡片 -->
    <section class="setting-card hero-card">
      <div class="hero-master-row">
        <div class="hero-copy">
          <div class="hero-icon" :class="{ on: enabled, connected: state === 'connected' }">
            <AppIcon name="globe" :size="18" :stroke-width="1.9" />
          </div>
          <div class="hero-text">
            <div class="hero-title-line">
              <h2>连接远程（云中转）</h2>
              <span class="state-pill" :class="{ on: enabled }">
                {{ enabled ? '已开启' : '已关闭' }}
              </span>
            </div>
            <p>开启后主动连接中转服务器供手机端接入；仅转接入口，权限仍受本机控制。</p>
          </div>
        </div>
        <button
          type="button"
          role="switch"
          class="setting-switch"
          data-test="cloud-relay-enabled"
          aria-label="连接远程（云中转）"
          :aria-checked="enabled ? 'true' : 'false'"
          :disabled="busy"
          @click="toggleEnabled"
        >
          <span class="switch-knob" />
        </button>
      </div>

      <div class="relay-status" :class="`is-${state}`" data-test="cloud-relay-status">
        <span class="dot" :class="{ pending: isPending }" />
        <span class="label">{{ stateLabel }}</span>
        <span class="hint">{{ stateHint }}</span>
        <span v-if="showMetrics" class="metrics mono">
          排队 {{ stats.queued }} · 丢弃 {{ stats.dropped }} · 丢失 {{ stats.lost }}
        </span>
      </div>

      <div v-if="message" class="hero-message-bar" :class="{ bad: messageBad }">
        <p class="relay-message" :class="{ bad: messageBad }" data-test="cloud-relay-message">
          {{ message }}
        </p>
      </div>
    </section>

    <!-- 一键自动部署 -->
    <section class="setting-group" data-test="cloud-relay-deploy-card">
      <div class="group-title">
        <AppIcon name="zap" :size="14" />
        <span>还没有配置服务器？让星瑶自动部署</span>
      </div>
      <div class="setting-card">
        <div class="deploy-body">
          <p class="deploy-desc">填写服务器信息即可让星瑶自动部署 relay_server 并写回本机配置（也可留空在对话中确认）。</p>
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
        </div>
        <div class="card-footer deploy-footer">
          <span class="muted-hint">部署完成后会自动启用并热加载连线</span>
          <div class="deploy-actions">
            <button
              class="secondary-button"
              type="button"
              data-test="cloud-relay-copy-prompt"
              :disabled="busy"
              @click="copyDeployPrompt"
            >复制部署提示词</button>
            <button
              class="primary-button"
              type="button"
              data-test="cloud-relay-auto-deploy"
              :disabled="busy"
              @click="startAutoDeploy"
            >星瑶自动部署到服务器</button>
          </div>
        </div>
      </div>
    </section>

    <!-- 手动配置 / 高级设置 -->
    <section class="setting-group">
      <div class="setting-card">
        <button
          class="manual-toggle"
          type="button"
          data-test="cloud-relay-manual-toggle"
          @click="showManual = !showManual"
        >
          <span class="toggle-title">
            <AppIcon name="settings" :size="14" />
            <span>手动配置 / 高级设置</span>
          </span>
          <AppIcon name="chevron-right" :size="15" class="chev" :class="{ open: showManual }" />
        </button>

        <div v-show="showManual" class="manual-body">
          <div class="setting-row setting-row--top">
            <div class="setting-copy">
              <h3>网关地址</h3>
              <p>服务器中转服务的 WebSocket 地址，公网建议使用 <code>wss://</code>。</p>
            </div>
            <div class="setting-control--wide">
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
            </div>
          </div>

          <div class="setting-row">
            <div class="setting-copy">
              <h3>设备编号</h3>
              <p>服务器创建这台设备时生成的唯一编号。</p>
            </div>
            <div class="setting-control--wide">
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
            </div>
          </div>

          <div class="setting-row setting-row--top">
            <div class="setting-copy">
              <h3>设备令牌</h3>
              <p>加密保存在本地，需与服务端设备配置保持一致。</p>
            </div>
            <div class="setting-control--wide">
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
                  class="secondary-button compact"
                  type="button"
                  data-test="cloud-relay-toggle-token"
                  :disabled="busy"
                  @click="showToken = !showToken"
                >{{ showToken ? '隐藏' : '显示' }}</button>
              </div>
              <p class="field-hint">
                <span class="token-state" :class="{ ok: hasToken }">{{ hasToken ? '已配置' : '未配置' }}</span>
                留空保存将保留原令牌。
              </p>
            </div>
          </div>

          <div class="card-footer">
            <span class="muted-hint">修改地址或令牌后需保存并重连</span>
            <div class="relay-actions">
              <button
                v-if="isLive"
                class="secondary-button danger-soft"
                type="button"
                data-test="cloud-relay-disconnect"
                :disabled="busy"
                @click="disconnect"
              >断开</button>
              <button
                class="secondary-button"
                type="button"
                data-test="cloud-relay-reconnect"
                :disabled="busy || !enabled"
                title="不改配置，丢掉当前连接按原配置重拨"
                @click="reconnect"
              >重拨一次</button>
              <button
                class="primary-button"
                type="button"
                data-test="cloud-relay-save"
                :disabled="busy"
                @click="save"
              >{{ busy ? '处理中…' : '保存并连接' }}</button>
            </div>
          </div>
        </div>
      </div>
    </section>

    <!-- 配置检查 -->
    <section class="setting-group" data-test="cloud-relay-checklist">
      <div class="group-title-row">
        <div class="group-title">
          <AppIcon name="check-circle" :size="14" />
          <span>配置检查</span>
        </div>
        <span class="group-meta">{{ readyCount }} / {{ checklist.length }} 项就绪</span>
      </div>
      <div class="setting-card relay-steps">
        <ul>
          <li v-for="item in checklist" :key="item.label" :class="{ ok: item.ok }">
            <span class="mark" aria-hidden="true">
              <AppIcon v-if="item.ok" name="check" :size="11" :stroke-width="2.6" />
            </span>
            <span class="what">{{ item.label }}</span>
            <span v-if="item.hint" class="how">{{ item.hint }}</span>
          </li>
        </ul>
      </div>
    </section>

    <!-- 排障指南 -->
    <section class="setting-group">
      <div class="setting-card relay-help">
        <button
          class="help-toggle"
          type="button"
          data-test="cloud-relay-guide-toggle"
          @click="showGuide = !showGuide"
        >
          <span class="toggle-title">
            <AppIcon name="info" :size="14" />
            <span>连不上时按这个顺序查</span>
          </span>
          <AppIcon name="chevron-right" :size="15" class="chev" :class="{ open: showGuide }" />
        </button>
        <div v-show="showGuide" class="help-body" data-test="cloud-relay-guide">
          <ol>
            <li>
              <b>状态条停在「未启用」</b>：开关没打开，或者地址 / 编号 / 令牌缺一项。
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
      </div>
    </section>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue';
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
    hint: enabled.value ? '' : '开启上方的开关',
  },
  {
    label: '已连上网关',
    ok: state.value === 'connected',
    hint: state.value === 'connected' ? '' : `当前：${stateLabel.value}`,
  },
]);

const readyCount = computed(() => checklist.value.filter((item) => item.ok).length);

function api(): BridgeApi | undefined {
  return (window as unknown as { pywebview?: { api?: BridgeApi } }).pywebview?.api;
}

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

async function toggleEnabled() {
  if (busy.value) return;
  const next = !enabled.value;
  if (next) {
    if (!hasConfig.value) {
      messageBad.value = true;
      message.value = '尚未配置远程服务器：请点击下方「星瑶自动部署到服务器」，或展开「手动配置」填写网关信息。';
      return;
    }
    enabled.value = true;
    await save();
    return;
  }
  enabled.value = false;
  await save();
}

function buildDeployPrompt(): string {
  const host = deployHost.value.trim() || '（待确认：请先向我询问服务器公网 IP）';
  const user = deployUser.value.trim() || 'ubuntu（若不同请向我确认）';
  const domain = deployDomain.value.trim() || '（可选：若有域名请配置 HTTPS/WSS；无域名则确认是否直接走 ws://<IP>/relay/device）';
  return [
    '请帮我把当前项目的云中转服务（relay_server）自动部署到我的 Linux 服务器，并完成本机 Y-code 的远程连接配置，让我能直接手机公网扫码使用。',
    '',
    '服务器信息：',
    `- 服务器地址：${host}`,
    `- SSH 用户：${user}`,
    `- 公网域名：${domain}`,
    '',
    '请按以下步骤执行（注意：用户可能是通过安装包运行，本机不一定有源码、系统 Python 或 Node/npm 环境）：',
    '1. 如果缺少服务器 IP、SSH 登录方式或域名确认，先向我确认后再动手；Windows 下优先使用 SSH 密钥（`ssh -i`），若只有密码且非交互命令无法输密码，可引导我先配好 SSH 公钥免密或临时使用 `SSH_ASKPASS`（用完即删，切勿落盘保存密码）；',
    '2. 定位服务端代码与静态资源（先检查当前工作目录或正在运行的 `Y-code.exe` / `ycode.exe` 安装目录旁的 `_internal/`）：直接读取 `relay_server/`、`requirements-relay.txt`、`frontend-mobile/dist`、`frontend/dist`、`frontend/mobile-shim.js`（服务端已原生兼容 `frontend/dist` 的 `/assets/` 路径，无需源码或 Node 重新构建；若处于源码目录也可参考 `relay_server/deploy/README.md`）；',
    '3. 通过 `scp` 将 `relay_server/`、`requirements-relay.txt`、移动端 `frontend-mobile/dist`（上传到 `/srv/relay/static/mobile`）、桌面端 `frontend/dist`（上传到 `/srv/relay/static/desktop`）及 `mobile-shim.js`（上传到 `/srv/relay/static/mobile-shim.js`）上传到服务器 `/srv/relay`；',
    '4. 在服务器配置 Python venv、安装 `requirements-relay.txt`、生成 `/etc/relay/jwt_secret` 与 `/etc/relay/relay.env`（监听 `127.0.0.1:18080`），安装并启动 systemd 服务 `relay.service`（`relay_server/deploy/relay.service`）；',
    '5. 参考 `relay_server/deploy/nginx-standalone.conf` 配置独立 Nginx 反向代理（必须覆盖 `/health`、`/api/`、`/relay/` 的 WebSocket Upgrade、`/m/`、`/desktop/`、`/assets/`、`/desktop-assets/`、`/desktop-shim.js`、`/icon.png` 与 JS/CSS gzip）：有域名则配 HTTPS/WSS（如 `certbot --nginx`），无域名仅 IP 则走 80 端口 HTTP/WS 并提醒放行云安全组端口；',
    '6. 在服务器执行 `python -m relay_server create-device` 创建本机设备，拿到 `device_id` 与 `token`；',
    '7. 优先调用本机安装包自带命令 `ycode.exe --ycode-relay-configure --url "<ws(s)://.../relay/device>" --device-id "<ID>" --token "<TOKEN>"`（或 `Y-code.exe --ycode-relay-configure ...`，源码模式可用 `python gui_main.py --ycode-relay-configure ...`）自动加密写入 `~/.ycode/config.json`；等待 2~3 秒让运行中的 GUI 自动热加载连线后，执行 `ycode.exe --ycode-relay-status`（或查看 `~/.ycode/cloud_relay_status.json`）确认 `state` 为 `connected` 再告诉我结果。',
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
  max-width: 880px;
  margin: 0 auto;
  padding-bottom: 24px;
}

.page-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 18px;
  margin-bottom: 22px;
}

.relay-head-text h1 {
  margin: 0 0 6px;
  font-size: 22px;
  font-weight: 600;
  letter-spacing: -0.02em;
  color: var(--text);
}

.relay-head-text p {
  margin: 0;
  font-size: 13px;
  line-height: 1.5;
  color: var(--text-faint);
}

.header-actions {
  display: flex;
  align-items: center;
  gap: 10px;
  flex: none;
}

.refresh-button {
  width: 32px;
  height: 32px;
  display: grid;
  place-items: center;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--bg2);
  color: var(--text-dim);
  cursor: pointer;
  transition: background .15s ease, color .15s ease;
}

.refresh-button:hover {
  color: var(--text);
  background: var(--bg3);
}

.setting-group {
  margin-top: 24px;
}

.group-title-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin: 0 0 10px 2px;
}

.group-title {
  display: flex;
  align-items: center;
  gap: 7px;
  margin: 0 0 10px 2px;
  font-size: 13.5px;
  font-weight: 600;
  color: var(--text-dim);
}

.group-title-row .group-title {
  margin: 0;
}

.group-meta {
  font-size: 11.5px;
  color: var(--text-faint);
}

.setting-card {
  border: 1px solid var(--border);
  border-radius: 12px;
  background: var(--bg1);
  overflow: hidden;
}

/* Hero 总控卡片 */
.hero-master-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 20px;
  padding: 18px 20px;
}

.hero-copy {
  display: flex;
  align-items: flex-start;
  gap: 14px;
  min-width: 0;
  flex: 1 1 auto;
}

.hero-icon {
  width: 38px;
  height: 38px;
  border-radius: 10px;
  display: grid;
  place-items: center;
  flex: none;
  border: 1px solid var(--border);
  background: var(--bg2);
  color: var(--text-faint);
  transition: background .18s ease, color .18s ease, border-color .18s ease;
}

.hero-icon.on {
  background: var(--accent-soft);
  border-color: color-mix(in srgb, var(--accent) 32%, transparent);
  color: var(--accent);
}

.hero-icon.connected {
  background: color-mix(in srgb, var(--green) 12%, transparent);
  border-color: color-mix(in srgb, var(--green) 34%, transparent);
  color: var(--green);
}

.hero-text {
  min-width: 0;
}

.hero-title-line {
  display: flex;
  align-items: center;
  gap: 9px;
  flex-wrap: wrap;
  margin-bottom: 4px;
}

.hero-title-line h2 {
  margin: 0;
  font-size: 14.5px;
  font-weight: 600;
  color: var(--text);
}

.hero-text p {
  margin: 0;
  color: var(--text-faint);
  font-size: 12.5px;
  line-height: 1.55;
}

.state-pill {
  padding: 1px 8px;
  border-radius: 999px;
  font-size: 11px;
  font-weight: 500;
  border: 1px solid var(--border);
  background: var(--bg2);
  color: var(--text-faint);
}

.state-pill.on {
  color: var(--green);
  border-color: color-mix(in srgb, var(--green) 35%, transparent);
  background: color-mix(in srgb, var(--green) 10%, transparent);
}

/* 统一滑动开关按钮 */
.setting-switch {
  position: relative;
  flex: 0 0 auto;
  width: 44px;
  height: 24px;
  padding: 0;
  border: 1px solid var(--xy-border-strong, var(--border));
  border-radius: 999px;
  background: color-mix(in srgb, var(--text) 13%, var(--bg2));
  cursor: pointer;
  transition: background .18s ease, border-color .18s ease, opacity .18s ease;
}

.setting-switch:hover:not(:disabled):not([aria-checked='true']) {
  background: color-mix(in srgb, var(--text) 20%, var(--bg2));
}

.setting-switch[aria-checked='true'] {
  background: var(--accent);
  border-color: var(--accent);
}

.setting-switch .switch-knob {
  position: absolute;
  top: 2px;
  left: 2px;
  width: 18px;
  height: 18px;
  border-radius: 999px;
  background: #ffffff;
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.24);
  transition: transform .18s cubic-bezier(.22, 1, .36, 1);
}

.setting-switch[aria-checked='true'] .switch-knob {
  transform: translateX(20px);
}

.setting-switch:focus-visible {
  outline: none;
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent) 24%, transparent);
}

.setting-switch:disabled {
  cursor: not-allowed;
  opacity: .48;
}

/* 状态条 */
.relay-status {
  display: flex;
  align-items: center;
  gap: 9px;
  flex-wrap: wrap;
  padding: 11px 20px;
  border-top: 1px solid var(--border);
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
  background: color-mix(in srgb, var(--green) 7%, var(--bg2));
}

.relay-status.is-connected .dot {
  background: var(--green);
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--green) 18%, transparent);
}

.relay-status.is-connecting .dot,
.relay-status.is-reconnecting .dot {
  background: var(--warning, #d97706);
}

.relay-status.is-error {
  background: color-mix(in srgb, var(--red) 7%, var(--bg2));
}

.relay-status.is-error .dot {
  background: var(--red);
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--red) 18%, transparent);
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

.hero-message-bar {
  padding: 10px 20px;
  border-top: 1px solid var(--border);
  background: color-mix(in srgb, var(--green) 7%, var(--bg1));
}

.hero-message-bar.bad {
  background: color-mix(in srgb, var(--red) 7%, var(--bg1));
}

.relay-message {
  margin: 0;
  font-size: 12.5px;
  line-height: 1.55;
  color: var(--green);
}

.relay-message.bad {
  color: var(--red);
}

/* 自动部署 */
.deploy-body {
  padding: 16px 20px;
}

.deploy-desc {
  margin: 0 0 12px;
  font-size: 12.5px;
  line-height: 1.55;
  color: var(--text-dim);
}

.deploy-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 10px;
}

.card-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 13px 20px;
  border-top: 1px solid var(--border);
  background: var(--bg2);
  flex-wrap: wrap;
}

.muted-hint {
  font-size: 12px;
  color: var(--text-faint);
}

.deploy-actions,
.relay-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
}

/* 通用按钮 */
.primary-button,
.secondary-button {
  min-height: 34px;
  padding: 0 16px;
  border-radius: 8px;
  border: 1px solid var(--border);
  font-weight: 600;
  cursor: pointer;
  font-size: 12.5px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  transition: opacity .15s ease, background .15s ease, border-color .15s ease, color .15s ease;
}

.primary-button {
  background: var(--text);
  color: var(--bg0);
  border-color: transparent;
}

.primary-button:hover:not(:disabled) {
  opacity: .88;
}

.primary-button:disabled,
.secondary-button:disabled {
  opacity: .48;
  cursor: not-allowed;
}

.secondary-button {
  background: var(--bg1);
  color: var(--text);
}

.secondary-button:hover:not(:disabled) {
  background: var(--bg3);
}

.secondary-button.compact {
  min-height: 36px;
  padding: 0 12px;
  font-size: 12px;
  font-weight: 500;
}

.secondary-button.danger-soft {
  color: var(--red);
  border-color: color-mix(in srgb, var(--red) 35%, var(--border));
}

.secondary-button.danger-soft:hover:not(:disabled) {
  background: color-mix(in srgb, var(--red) 9%, var(--bg1));
}

/* 手动配置折叠面板 */
.manual-toggle,
.help-toggle {
  display: flex;
  align-items: center;
  justify-content: space-between;
  width: 100%;
  padding: 14px 20px;
  border: none;
  background: none;
  color: var(--text);
  font-size: 13.5px;
  font-weight: 600;
  text-align: left;
  cursor: pointer;
}

.manual-toggle:hover,
.help-toggle:hover {
  background: var(--bg2);
}

.toggle-title {
  display: inline-flex;
  align-items: center;
  gap: 8px;
}

.chev {
  color: var(--text-faint);
  transition: transform .15s ease;
}

.chev.open {
  transform: rotate(90deg);
}

.manual-body {
  border-top: 1px solid var(--border);
}

.setting-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 24px;
  padding: 15px 20px;
  border-bottom: 1px solid var(--border);
}

.setting-row--top {
  align-items: flex-start;
}

.setting-copy {
  flex: 1 1 auto;
  min-width: 0;
}

.setting-copy h3 {
  margin: 0 0 3px;
  font-size: 13.5px;
  font-weight: 600;
  color: var(--text);
}

.setting-copy p {
  margin: 0;
  color: var(--text-faint);
  font-size: 12px;
  line-height: 1.55;
}

.setting-copy code {
  padding: 1px 5px;
  border-radius: 4px;
  background: var(--bg2);
  font-family: var(--mono);
  font-size: 11px;
  color: var(--text-dim);
}

.setting-control--wide {
  flex: 0 1 430px;
  width: 430px;
  max-width: 100%;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.text-field {
  width: 100%;
  box-sizing: border-box;
  min-height: 36px;
  padding: 0 12px;
  border: 1px solid var(--border);
  border-radius: 9px;
  background: var(--bg2);
  color: var(--text);
  font-size: 13px;
  transition: border-color .15s ease, box-shadow .15s ease, background .15s ease;
}

.text-field:focus {
  outline: none;
  border-color: var(--accent);
  background: var(--bg1);
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent) 15%, transparent);
}

.text-field::placeholder {
  color: var(--text-faint);
}

.text-field:disabled {
  opacity: .55;
  cursor: not-allowed;
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
  margin: 2px 0 0;
  font-size: 11.5px;
  line-height: 1.55;
  color: var(--text-faint);
}

.token-state {
  display: inline-block;
  margin-right: 6px;
  padding: 1px 7px;
  border: 1px solid var(--border);
  border-radius: 999px;
  font-size: 11px;
  color: var(--text-faint);
  background: var(--bg2);
}

.token-state.ok {
  border-color: color-mix(in srgb, var(--green) 35%, transparent);
  background: color-mix(in srgb, var(--green) 10%, transparent);
  color: var(--green);
}

/* 配置检查清单 */
.relay-steps ul {
  display: flex;
  flex-direction: column;
  margin: 0;
  padding: 0;
  list-style: none;
}

.relay-steps li {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 12px 20px;
  border-bottom: 1px solid var(--border);
  font-size: 12.5px;
  color: var(--text-dim);
}

.relay-steps li:last-child {
  border-bottom: 0;
}

.relay-steps li .mark {
  width: 18px;
  height: 18px;
  border-radius: 50%;
  display: grid;
  place-items: center;
  flex: none;
  border: 1px solid var(--xy-border-strong, var(--border));
  background: var(--bg2);
  color: transparent;
}

.relay-steps li.ok .mark {
  border-color: var(--green);
  background: color-mix(in srgb, var(--green) 14%, transparent);
  color: var(--green);
}

.relay-steps li.ok .what {
  color: var(--text);
  font-weight: 500;
}

.relay-steps li .how {
  margin-left: auto;
  max-width: 58%;
  text-align: right;
  font-size: 11.5px;
  color: var(--text-faint);
}

/* 排障指南 */
.help-body {
  padding: 14px 20px 18px;
  border-top: 1px solid var(--border);
  background: var(--bg2);
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
  border: 1px solid var(--border);
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

@media (max-width: 768px) {
  .page-header {
    flex-direction: column;
  }
  .hero-master-row {
    align-items: flex-start;
  }
  .deploy-grid {
    grid-template-columns: 1fr;
  }
  .setting-row {
    flex-direction: column;
    align-items: stretch;
    gap: 12px;
    padding: 14px 16px;
  }
  .setting-control--wide {
    width: 100%;
  }
  .relay-steps li {
    flex-wrap: wrap;
    padding: 12px 16px;
  }
  .relay-steps li .how {
    margin-left: 28px;
    text-align: left;
    max-width: none;
  }
}
</style>

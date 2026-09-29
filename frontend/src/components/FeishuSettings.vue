<template>
  <div class="feishu-settings" data-test="feishu-settings-page">
    <header class="feishu-header">
      <div>
        <h1>飞书集成</h1>
        <p>在飞书里和星瑶对话：发消息驱动一轮任务，执行结果、权限审批与提问都在飞书闭环。</p>
      </div>
      <button class="refresh-button" type="button" title="重新加载" @click="load">
        <AppIcon name="refresh" :size="14" />
      </button>
    </header>

    <section class="feishu-status" :class="statusClass" data-test="feishu-status">
      <span class="dot" />
      <span class="label">{{ statusLabel }}</span>
      <span v-if="settings.status.received_events" class="meta mono">
        已收事件 {{ settings.status.received_events }}
        <template v-if="settings.status.duplicate_events">（去重 {{ settings.status.duplicate_events }}）</template>
      </span>
      <span v-if="settings.status.last_error" class="err">{{ settings.status.last_error }}</span>
    </section>

    <section class="feishu-card">
      <label class="switch-row">
        <input
          v-model="enabledDraft"
          data-test="feishu-enabled"
          type="checkbox"
          :disabled="loading || saving"
        />
        <span>
          <b>启用飞书通道</b>
          <small>关闭后星瑶与现在完全一致：不建立连接、不接收任何飞书消息。</small>
        </span>
      </label>

      <label class="field-label" for="feishu-app-id">App ID</label>
      <input
        id="feishu-app-id"
        v-model="appIdDraft"
        data-test="feishu-app-id"
        class="text-field mono"
        type="text"
        placeholder="cli_xxxxxxxx"
        :disabled="loading || saving"
      />

      <label class="field-label" for="feishu-app-secret">App Secret</label>
      <div class="secret-field">
        <input
          id="feishu-app-secret"
          v-model="secretDraft"
          data-test="feishu-app-secret"
          class="text-field mono"
          :type="showSecret ? 'text' : 'password'"
          :placeholder="settings.has_secret ? '已配置；留空表示不修改' : '输入 App Secret'"
          :disabled="loading || saving"
        />
        <button
          data-test="feishu-toggle-secret"
          class="secondary-button"
          type="button"
          :disabled="loading || saving"
          @click="toggleSecret"
        >{{ showSecret ? '隐藏' : '显示' }}</button>
      </div>
      <p class="field-hint">密钥加密保存（Windows 用 DPAPI），界面与快照都不回显明文。</p>

      <label class="field-label" for="feishu-approvers">审批白名单（open_id，逗号分隔）</label>
      <input
        id="feishu-approvers"
        v-model="approversDraft"
        data-test="feishu-approvers"
        class="text-field mono"
        type="text"
        placeholder="ou_xxx, ou_yyy"
        :disabled="loading || saving"
      />
      <p class="field-hint">
        只有名单内的人能在飞书点「批准 / 拒绝」；<b>留空则飞书侧不可审批</b>（fail-closed），
        审批卡只做提醒，仍需回桌面端处理。
      </p>

      <label class="field-label" for="feishu-notify-chat">固定推送会话 ID（可选）</label>
      <input
        id="feishu-notify-chat"
        v-model="notifyChatDraft"
        data-test="feishu-notify-chat"
        class="text-field mono"
        type="text"
        placeholder="oc_xxxxxxxx（留空则推给最后说话的会话）"
        :disabled="loading || saving"
      />

      <div class="feishu-actions">
        <button
          data-test="feishu-test"
          class="secondary-button"
          type="button"
          :disabled="loading || saving || testing"
          @click="testConnection"
        >{{ testing ? '测试中…' : '测试连接' }}</button>
        <button
          data-test="feishu-save"
          class="primary-button"
          type="button"
          :disabled="loading || saving"
          @click="save"
        >{{ saving ? '保存中…' : '保存' }}</button>
      </div>

      <p v-if="message" data-test="feishu-message" class="feishu-message" :class="{ bad: messageBad }">
        {{ message }}
      </p>
    </section>

    <section class="feishu-help">
      <h2>飞书侧怎么配（三步）</h2>
      <ol>
        <li>在飞书开放平台建一个<b>企业自建应用</b>，拿到 App ID 与 App Secret 填到上面。</li>
        <li>给应用开通权限：<code>im:message</code>（读消息）、<code>im:message:send_as_bot</code>（发消息）；
          需要在群里 @ 机器人时再加 <code>im:message.group_at_msg</code>。</li>
        <li>在「事件与回调」里把订阅方式切到<b>使用长连接接收事件</b>，并订阅
          <code>im.message.receive_v1</code>（收消息）与 <code>card.action.trigger</code>（卡片按钮回传）。</li>
      </ol>
      <p class="field-hint">
        长连接由星瑶主动连飞书，<b>不需要公网 IP、也不需要内网穿透</b>；保存后立即生效。
        配好后在飞书里给机器人发一句话试试，或发 <code>/help</code> 看指令表。
      </p>
    </section>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue';
import { useAppStore } from '../stores/appStore';
import AppIcon from './AppIcon.vue';

const store = useAppStore();

const loading = ref(false);
const saving = ref(false);
const testing = ref(false);
const showSecret = ref(false);
const message = ref('');
const messageBad = ref(false);

const enabledDraft = ref(false);
const appIdDraft = ref('');
const secretDraft = ref('');
const approversDraft = ref('');
const notifyChatDraft = ref('');

const settings = computed(() => store.feishuSettings);

const STATUS_LABELS: Record<string, string> = {
  disabled: '未启用',
  connecting: '连接中…',
  connected: '已连接',
  reconnecting: '重连中…',
  error: '连接异常',
  stopped: '已停止',
};

const statusLabel = computed(() => STATUS_LABELS[settings.value.status.state] || settings.value.status.state);
const statusClass = computed(() => `is-${settings.value.status.state || 'disabled'}`);

function applySnapshot() {
  enabledDraft.value = !!settings.value.enabled;
  appIdDraft.value = settings.value.app_id || '';
  approversDraft.value = (settings.value.approvers || []).join(', ');
  notifyChatDraft.value = settings.value.notify_chat_id || '';
  secretDraft.value = '';
}

async function load() {
  loading.value = true;
  try {
    await store.refreshFeishuSettings();
    applySnapshot();
  } finally {
    loading.value = false;
  }
}

function parseApprovers(text: string): string[] {
  return String(text || '')
    .replace(/，/g, ',')
    .split(',')
    .map((item) => item.trim())
    .filter(Boolean);
}

async function toggleSecret() {
  if (showSecret.value) {
    showSecret.value = false;
    return;
  }
  if (!secretDraft.value && settings.value.has_secret) {
    secretDraft.value = await store.revealFeishuSecret();
  }
  showSecret.value = true;
}

async function save() {
  saving.value = true;
  message.value = '';
  try {
    const result = await store.saveFeishuSettings({
      enabled: enabledDraft.value,
      app_id: appIdDraft.value.trim(),
      app_secret: secretDraft.value,
      approvers: parseApprovers(approversDraft.value),
      notify_chat_id: notifyChatDraft.value.trim(),
    });
    messageBad.value = !result.ok;
    message.value = result.ok ? '已保存' : `保存失败：${result.msg}`;
    if (result.ok) applySnapshot();
  } finally {
    saving.value = false;
  }
}

async function testConnection() {
  testing.value = true;
  message.value = '';
  try {
    const result = await store.testFeishuConnection(appIdDraft.value.trim(), secretDraft.value.trim());
    messageBad.value = !result.ok;
    message.value = result.ok ? result.msg || '连接正常' : `连接失败：${result.msg}`;
  } finally {
    testing.value = false;
  }
}

onMounted(load);
</script>

<style scoped>
.feishu-settings { display: flex; flex-direction: column; gap: 14px; }
.feishu-header { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }
.feishu-header h1 { margin: 0 0 4px; font-size: 17px; }
.feishu-header p { margin: 0; font-size: 12.5px; color: var(--text-2); line-height: 1.6; }
.refresh-button { display: inline-flex; align-items: center; justify-content: center; width: 30px; height: 30px; border: 1px solid var(--border); border-radius: 8px; background: var(--bg2); color: var(--text-2); }

.feishu-status { display: flex; align-items: center; gap: 8px; padding: 9px 12px; border: 1px solid var(--border); border-radius: 9px; background: var(--bg2); font-size: 12.5px; }
.feishu-status .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--text-3); }
.feishu-status.is-connected .dot { background: var(--ok); }
.feishu-status.is-connecting .dot,
.feishu-status.is-reconnecting .dot { background: var(--warn); }
.feishu-status.is-error .dot { background: var(--danger); }
.feishu-status .meta { margin-left: auto; color: var(--text-3); font-size: 11.5px; }
.feishu-status .err { color: var(--danger); font-size: 11.5px; }

.feishu-card { display: flex; flex-direction: column; gap: 6px; padding: 14px; border: 1px solid var(--border); border-radius: 10px; background: var(--bg2); }
.switch-row { display: flex; align-items: flex-start; gap: 10px; padding-bottom: 8px; }
.switch-row input { margin-top: 3px; }
.switch-row b { display: block; font-size: 13px; }
.switch-row small { display: block; margin-top: 2px; font-size: 11.5px; color: var(--text-3); line-height: 1.5; }

.field-label { margin-top: 6px; font-size: 12px; color: var(--text-2); }
.field-hint { margin: 2px 0 6px; font-size: 11.5px; color: var(--text-3); line-height: 1.6; }
.text-field { min-height: 38px; padding: 0 12px; border: 1px solid var(--border); border-radius: 9px; background: var(--bg1); color: var(--text); font-size: 13px; }
.text-field:focus { outline: none; border-color: var(--accent); box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent) 15%, transparent); }
.mono { font-family: var(--font-mono); }
.secret-field { display: flex; gap: 8px; align-items: center; }
.secret-field .text-field { flex: 1; }

.feishu-actions { display: flex; gap: 8px; margin-top: 12px; }
.primary-button { padding: 0 18px; min-height: 38px; border: none; border-radius: 9px; background: var(--accent); color: #fff; font-size: 13px; font-weight: 600; }
.primary-button:disabled { opacity: .5; }
.secondary-button { padding: 0 14px; min-height: 38px; border: 1px solid var(--border); border-radius: 9px; background: var(--bg1); color: var(--text-2); font-size: 13px; }
.secondary-button:disabled { opacity: .5; }
.feishu-message { margin: 8px 0 0; font-size: 12px; color: var(--ok); }
.feishu-message.bad { color: var(--danger); }

.feishu-help { padding: 14px; border: 1px solid var(--border); border-radius: 10px; background: var(--bg2); }
.feishu-help h2 { margin: 0 0 8px; font-size: 13.5px; }
.feishu-help ol { margin: 0; padding-left: 18px; }
.feishu-help li { margin-bottom: 6px; font-size: 12.5px; color: var(--text-2); line-height: 1.7; }
.feishu-help code { padding: 1px 5px; border-radius: 4px; background: var(--bg1); font-family: var(--font-mono); font-size: 11.5px; }
</style>

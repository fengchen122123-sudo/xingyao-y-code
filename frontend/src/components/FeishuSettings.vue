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

    <section class="feishu-checklist" data-test="feishu-checklist">
      <h2>配置检查清单</h2>
      <ul>
        <li v-for="item in checklist" :key="item.label" :class="{ ok: item.ok }">
          <span class="mark">{{ item.ok ? '✅' : '⬜' }}</span>
          <span class="what">{{ item.label }}</span>
          <span class="how">{{ item.hint }}</span>
        </li>
      </ul>
    </section>

    <section class="feishu-help">
      <button class="help-toggle" type="button" data-test="feishu-guide-toggle" @click="showGuide = !showGuide">
        <span>飞书侧怎么配（6 步）</span>
        <span class="chev" :class="{ open: showGuide }">›</span>
      </button>

      <div v-show="showGuide" class="help-body" data-test="feishu-guide">
        <ol>
          <li>
            <b>建应用</b>：打开
            <a href="https://open.feishu.cn/" target="_blank" rel="noreferrer">飞书开放平台</a>
            → 开发者后台 → 「创建企业自建应用」。
            建好后在 <b>凭证与基础信息</b> 页复制 <code>App ID</code> 与 <code>App Secret</code>，
            填到上面的表单里。
          </li>
          <li>
            <b>开权限</b>：应用详情 → <b>权限管理</b>，搜索并开通：
            <code>im:message</code>（接收消息）、<code>im:message:send_as_bot</code>（以机器人身份发消息）。
            需要在群里 @ 机器人时再加 <code>im:message.group_at_msg</code>。
          </li>
          <li>
            <b>事件订阅切长连接</b>：应用详情 → <b>事件与回调</b> → 订阅方式选
            <b>使用长连接接收事件</b>；然后添加两个事件：
            <code>im.message.receive_v1</code>（收到消息）、
            <code>card.action.trigger</code>（卡片按钮回传）。
            <span class="tip">选长连接就<b>不用填回调地址、不用公网 IP、不用内网穿透</b>。</span>
          </li>
          <li>
            <b>发布版本</b>：应用详情 → <b>版本管理与发布</b> → 创建版本并申请发布，
            由企业管理员审核通过。
            <span class="tip">这一步最容易漏：应用没发布，权限与事件订阅都不会真正生效，表现为「发消息没反应」。</span>
          </li>
          <li>
            <b>把机器人加进会话</b>：在飞书里搜到这个应用/机器人，单聊直接发消息即可；
            群里需要把它添加为群机器人，并用 <code>@机器人 内容</code> 触发。
          </li>
          <li>
            <b>拿到你的 open_id 填白名单</b>：保存上面的凭据并启用后，
            在飞书里给机器人发 <code>/whoami</code>，它会回你的 <code>open_id</code>，
            复制进「审批白名单」再保存一次即可。
            <span class="tip">open_id 是<b>按应用</b>区分的——必须用星瑶看到的这个值，别处复制的不能用。</span>
          </li>
        </ol>

        <h3>配好后怎么用</h3>
        <ul>
          <li>直接在飞书里发一句话，就是给星瑶下达一轮任务；跑完会收到「执行完成卡」。</li>
          <li>发 <code>/help</code> 看全部指令；<code>/panel</code> 是按钮面板。</li>
          <li>星瑶要动敏感操作时会推「审批卡」，在飞书里点批准 / 拒绝（前提：白名单已配）。</li>
        </ul>

        <h3>连不上 / 收不到消息时依次查</h3>
        <ul>
          <li>上面的状态条是不是「已连接」；不是就先点「测试连接」看凭据是否有效。</li>
          <li>事件订阅是不是选了<b>长连接</b>（选了「回调地址」而没填地址，事件不会推过来）。</li>
          <li>应用是否<b>已发布</b>且管理员已通过；权限是否已开通。</li>
          <li>星瑶这边「启用飞书通道」开关是否打开（关闭时完全不建连接）。</li>
        </ul>
      </div>
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
const showGuide = ref(true);
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

/** 配置检查清单：让用户一眼看出「我配到哪一步了」 */
const checklist = computed(() => {
  const current = settings.value;
  const approvers = current.approvers || [];
  const connected = current.status.state === 'connected';
  return [
    {
      label: 'App ID 已填写',
      ok: !!current.app_id,
      hint: current.app_id ? '' : '在飞书应用「凭证与基础信息」里复制',
    },
    {
      label: 'App Secret 已配置',
      ok: !!current.has_secret,
      hint: current.has_secret ? '（加密存储，不显示明文）' : '填完点「保存」',
    },
    {
      label: '通道已启用',
      ok: !!current.enabled,
      hint: current.enabled ? '' : '勾选上面的「启用飞书通道」并保存',
    },
    {
      label: '已连上飞书',
      ok: connected,
      hint: connected ? '' : `当前状态：${statusLabel.value}`,
    },
    {
      label: '审批白名单已配置',
      ok: approvers.length > 0,
      hint: approvers.length ? `共 ${approvers.length} 人` : '在飞书里发 /whoami 拿到自己的 open_id 再填；不配则飞书侧不能审批',
    },
  ];
});

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

.feishu-checklist { padding: 12px 14px; border: 1px solid var(--border); border-radius: 10px; background: var(--bg2); }
.feishu-checklist h2 { margin: 0 0 8px; font-size: 13px; }
.feishu-checklist ul { margin: 0; padding: 0; list-style: none; display: flex; flex-direction: column; gap: 5px; }
.feishu-checklist li { display: flex; align-items: baseline; gap: 7px; font-size: 12.5px; color: var(--text-2); }
.feishu-checklist li .mark { flex: none; }
.feishu-checklist li.ok .what { color: var(--text-1); }
.feishu-checklist li .how { margin-left: auto; text-align: right; font-size: 11.5px; color: var(--text-3); max-width: 60%; }

.feishu-help { padding: 0 14px 14px; border: 1px solid var(--border); border-radius: 10px; background: var(--bg2); }
.help-toggle { display: flex; align-items: center; justify-content: space-between; width: 100%; padding: 13px 0; border: none; background: none; color: var(--text-1); font-size: 13.5px; font-weight: 600; text-align: left; }
.help-toggle .chev { transition: transform .15s; color: var(--text-3); font-size: 16px; }
.help-toggle .chev.open { transform: rotate(90deg); }
.help-body ol, .help-body ul { margin: 0; padding-left: 18px; }
.help-body li { margin-bottom: 8px; font-size: 12.5px; color: var(--text-2); line-height: 1.75; }
.help-body h3 { margin: 14px 0 6px; font-size: 12.5px; color: var(--text-1); }
.help-body a { color: var(--accent); }
.help-body code { padding: 1px 5px; border-radius: 4px; background: var(--bg1); font-family: var(--font-mono); font-size: 11.5px; }
.help-body .tip { display: block; margin-top: 3px; color: var(--text-3); font-size: 11.5px; }
</style>

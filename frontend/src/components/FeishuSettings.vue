<template>
  <div class="feishu-settings" data-test="feishu-settings-page">
    <header class="page-header">
      <div>
        <h1>飞书集成</h1>
        <p>通过飞书机器人收发任务与处理审批（长连接模式，无需公网 IP）。</p>
      </div>
      <div class="header-actions">
        <span v-if="isDirty" class="dirty-pill" data-test="feishu-dirty-pill">有未保存修改</span>
        <button
          class="refresh-button"
          type="button"
          title="重新加载"
          data-test="feishu-refresh"
          :disabled="loading"
          @click="load"
        >
          <AppIcon name="refresh" :size="14" :class="{ 'icon-spin': loading }" />
        </button>
      </div>
    </header>

    <!-- 总开关与连接状态 Hero 卡片 -->
    <section class="setting-card hero-card">
      <div class="hero-master-row">
        <div class="hero-copy">
          <div class="hero-text">
            <div class="hero-title-line">
              <h2>启用飞书通道</h2>
              <span class="state-pill" :class="{ on: enabledDraft }" data-test="feishu-enabled-pill">
                {{ enabledDraft ? '已开启' : '已关闭' }}
              </span>
            </div>
            <p>开启后建立长连接收发任务；关闭时不建立连接、不接收飞书消息。</p>
          </div>
        </div>
        <button
          type="button"
          role="switch"
          class="setting-switch"
          data-test="feishu-enabled"
          aria-label="启用飞书通道"
          :aria-checked="enabledDraft ? 'true' : 'false'"
          :disabled="loading || saving || toggling"
          @click="toggleEnabled"
        >
          <span class="switch-knob" />
        </button>
      </div>

      <div class="hero-status-strip" :class="statusClass" data-test="feishu-status">
        <div class="status-left">
          <span class="dot" />
          <span class="label">{{ statusLabel }}</span>
          <span v-if="settings.status.last_error" class="err">{{ settings.status.last_error }}</span>
        </div>
        <span v-if="settings.status.received_events" class="meta mono">
          已收事件 {{ settings.status.received_events }}
          <template v-if="settings.status.duplicate_events">（去重 {{ settings.status.duplicate_events }}）</template>
        </span>
      </div>
    </section>

    <!-- 凭证与权限配置 -->
    <section class="setting-group">
      <div class="group-title">
        <AppIcon name="settings" :size="14" />
        <span>应用凭证与审批策略</span>
      </div>
      <div class="setting-card">
        <div class="setting-row">
          <div class="setting-copy">
            <h3>App ID</h3>
            <p>在飞书开放平台「凭证与基础信息」中获取。</p>
          </div>
          <div class="setting-control setting-control--wide">
            <input
              id="feishu-app-id"
              v-model="appIdDraft"
              data-test="feishu-app-id"
              class="text-field mono"
              type="text"
              placeholder="cli_xxxxxxxx"
              :disabled="loading || saving"
            />
          </div>
        </div>

        <div class="setting-row setting-row--top">
          <div class="setting-copy">
            <h3>App Secret</h3>
            <p>加密保存在本地，默认掩码不回显明文。</p>
          </div>
          <div class="setting-control setting-control--wide">
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
                class="secondary-button compact"
                type="button"
                :disabled="loading || saving"
                @click="toggleSecret"
              >
                {{ showSecret ? '隐藏' : '显示' }}
              </button>
            </div>
            <p class="field-hint">
              <span class="status-tag" :class="{ ok: settings.has_secret }">
                {{ settings.has_secret ? '已配置' : '未配置' }}
              </span>
              点击「显示」可按需读取已保存的密钥。
            </p>
          </div>
        </div>

        <div class="setting-row setting-row--top">
          <div class="setting-copy">
            <h3>审批白名单</h3>
            <p>允许在飞书卡片点击批准/拒绝的成员 <code>open_id</code>（逗号分隔）。</p>
          </div>
          <div class="setting-control setting-control--wide">
            <input
              id="feishu-approvers"
              v-model="approversDraft"
              data-test="feishu-approvers"
              class="text-field mono"
              type="text"
              placeholder="ou_xxx, ou_yyy"
              :disabled="loading || saving"
            />
            <p class="field-hint">仅名单内成员可在飞书审批；留空则仅作提醒，需在本机确认。</p>
          </div>
        </div>

        <div class="setting-row">
          <div class="setting-copy">
            <h3>固定推送会话 ID（可选）</h3>
            <p>指定通知卡片发往的群或单聊会话，留空推给最后说话的会话。</p>
          </div>
          <div class="setting-control setting-control--wide">
            <input
              id="feishu-notify-chat"
              v-model="notifyChatDraft"
              data-test="feishu-notify-chat"
              class="text-field mono"
              type="text"
              placeholder="oc_xxxxxxxx（留空推给最近会话）"
              :disabled="loading || saving"
            />
          </div>
        </div>

        <div class="card-footer">
          <div class="card-feedback">
            <p v-if="message" data-test="feishu-message" class="feishu-message" :class="{ bad: messageBad }">
              {{ message }}
            </p>
            <p v-else-if="isDirty" class="dirty-hint">有未保存的配置修改</p>
            <p v-else class="muted-hint">填好凭据后可先测试连接，再点击保存生效</p>
          </div>
          <div class="feishu-actions">
            <button
              data-test="feishu-test"
              class="secondary-button"
              type="button"
              :disabled="loading || saving || testing"
              @click="testConnection"
            >
              {{ testing ? '测试中…' : '测试连接' }}
            </button>
            <button
              data-test="feishu-save"
              class="primary-button"
              type="button"
              :disabled="loading || saving"
              @click="save"
            >
              {{ saving ? '保存中…' : '保存' }}
            </button>
          </div>
        </div>
      </div>
    </section>

    <!-- 配置检查清单 -->
    <section class="setting-group">
      <div class="group-title-row">
        <div class="group-title">
          <AppIcon name="check-circle" :size="14" />
          <span>配置检查清单</span>
        </div>
        <span class="group-meta">{{ readyCount }} / {{ checklist.length }} 项就绪</span>
      </div>
      <div class="setting-card feishu-checklist" data-test="feishu-checklist">
        <ul>
          <li v-for="item in checklist" :key="item.label" :class="{ ok: item.ok }">
            <span class="check-circle" aria-hidden="true">
              <AppIcon v-if="item.ok" name="check" :size="11" :stroke-width="2.6" />
            </span>
            <span class="what">{{ item.label }}</span>
            <span v-if="item.hint" class="how">{{ item.hint }}</span>
          </li>
        </ul>
      </div>
    </section>

    <!-- 配置引导 -->
    <section class="setting-group">
      <div class="setting-card feishu-help">
        <button class="help-toggle" type="button" data-test="feishu-guide-toggle" @click="showGuide = !showGuide">
          <span class="help-title">
            <AppIcon name="info" :size="14" />
            <span>飞书侧怎么配（6 步）</span>
          </span>
          <AppIcon name="chevron-right" :size="15" class="chev" :class="{ open: showGuide }" />
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
              <b>事件与回调都要配</b>：应用详情 → <b>事件与回调</b>，先把订阅方式选
              <b>使用长连接接收事件</b>，然后分两块加：
              <ul>
                <li>
                  <b>事件订阅</b>（收消息）：
                  <code>im.message.receive_v1</code>。
                </li>
                <li>
                  <b>回调 / 卡片回调</b>（卡片按钮回传）：
                  <code>card.action.trigger</code>。
                  <span class="tip">
                    这块<b>最容易漏</b>：只配事件不配回调时，消息能收到、机器人也能回话，
                    但卡片上的按钮（批准 / 拒绝 / 面板 / 翻页）点了<b>没有任何反应</b>。
                  </span>
                </li>
              </ul>
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

          <h3>消息能收到，但卡片按钮点了没反应</h3>
          <ul>
            <li>
              多半是<b>回调没配</b>：回「事件与回调」里确认 <code>card.action.trigger</code>
              加在了<b>回调 / 卡片回调</b>那一块（不是只加在事件订阅里），且该处的订阅方式同样是长连接。
            </li>
            <li>再确认应用版本已重新发布——改完订阅要<b>重新发版</b>才会生效。</li>
          </ul>
        </div>
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
const toggling = ref(false);
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

const hasCredentials = computed(() =>
  Boolean(appIdDraft.value.trim() && (settings.value.has_secret || secretDraft.value.trim())),
);

const isDirty = computed(() => {
  const current = settings.value;
  return (
    enabledDraft.value !== !!current.enabled ||
    appIdDraft.value.trim() !== (current.app_id || '') ||
    secretDraft.value.length > 0 ||
    parseApprovers(approversDraft.value).join(',') !== (current.approvers || []).join(',') ||
    notifyChatDraft.value.trim() !== (current.notify_chat_id || '')
  );
});

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
      hint: current.has_secret ? '加密存储，不显示明文' : '填完点「保存」',
    },
    {
      label: '通道已启用',
      ok: !!current.enabled,
      hint: current.enabled ? '' : '开启上方的「启用飞书通道」开关',
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

const readyCount = computed(() => checklist.value.filter((item) => item.ok).length);

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

/** 切换总开关：凭据已就绪或关闭时立即持久化；未填凭据开启时先切为开启草稿并提示填写保存 */
async function toggleEnabled() {
  if (loading.value || saving.value || toggling.value) return;
  const next = !enabledDraft.value;
  message.value = '';

  if (next && !hasCredentials.value) {
    enabledDraft.value = true;
    messageBad.value = false;
    message.value = '已开启开关：请填写 App ID 与 App Secret 后点击「保存」生效';
    return;
  }

  toggling.value = true;
  enabledDraft.value = next;
  try {
    const result = await store.saveFeishuSettings({
      enabled: next,
      app_id: appIdDraft.value.trim(),
      app_secret: secretDraft.value,
      approvers: parseApprovers(approversDraft.value),
      notify_chat_id: notifyChatDraft.value.trim(),
    });
    messageBad.value = !result.ok;
    if (result.ok) {
      applySnapshot();
      message.value = next ? '已启用飞书通道' : '已关闭飞书通道';
    } else {
      enabledDraft.value = !next;
      message.value = `保存失败：${result.msg}`;
    }
  } finally {
    toggling.value = false;
  }
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
.feishu-settings {
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

.page-header h1 {
  margin: 0 0 6px;
  font-size: 22px;
  font-weight: 600;
  letter-spacing: -0.02em;
  color: var(--text);
}

.page-header p {
  margin: 0;
  font-size: 13px;
  color: var(--text-faint);
  line-height: 1.5;
}

.header-actions {
  display: flex;
  align-items: center;
  gap: 10px;
  flex: none;
}

.dirty-pill {
  padding: 3px 10px;
  border-radius: 999px;
  font-size: 11.5px;
  font-weight: 500;
  color: var(--warning, #d97706);
  background: color-mix(in srgb, var(--warning, #d97706) 11%, transparent);
  border: 1px solid color-mix(in srgb, var(--warning, #d97706) 28%, transparent);
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

.refresh-button:hover:not(:disabled) {
  color: var(--text);
  background: var(--bg3);
}

.refresh-button:disabled {
  opacity: .55;
  cursor: wait;
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

/* 顶部 Hero 总控卡片 */
.hero-master-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 20px;
  padding: 18px 20px;
}

.hero-copy {
  min-width: 0;
  flex: 1 1 auto;
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

.hero-status-strip {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
  padding: 11px 20px;
  border-top: 1px solid var(--border);
  background: var(--bg2);
  font-size: 12.5px;
}

.status-left {
  display: flex;
  align-items: center;
  gap: 9px;
  flex-wrap: wrap;
  min-width: 0;
}

.hero-status-strip .dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--text-faint);
  flex: none;
}

.hero-status-strip .label {
  font-weight: 600;
  color: var(--text);
}

.hero-status-strip.is-connected .dot {
  background: var(--green);
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--green) 18%, transparent);
}

.hero-status-strip.is-connecting .dot,
.hero-status-strip.is-reconnecting .dot {
  background: var(--warning, #d97706);
}

.hero-status-strip.is-error .dot {
  background: var(--red);
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--red) 18%, transparent);
}

.hero-status-strip .meta {
  color: var(--text-faint);
  font-size: 11.5px;
}

.hero-status-strip .err {
  color: var(--red);
  font-size: 12px;
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

/* 表单行 */
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

.text-field:disabled {
  opacity: .55;
  cursor: not-allowed;
}

.mono {
  font-family: var(--mono);
}

.secret-field {
  display: flex;
  gap: 8px;
  align-items: center;
}

.secret-field .text-field {
  flex: 1;
  min-width: 0;
}

.field-hint {
  margin: 2px 0 0;
  font-size: 11.5px;
  color: var(--text-faint);
  line-height: 1.55;
}

.status-tag {
  display: inline-block;
  margin-right: 6px;
  padding: 1px 7px;
  border: 1px solid var(--border);
  border-radius: 999px;
  font-size: 11px;
  color: var(--text-faint);
  background: var(--bg2);
}

.status-tag.ok {
  color: var(--green);
  border-color: color-mix(in srgb, var(--green) 35%, transparent);
  background: color-mix(in srgb, var(--green) 10%, transparent);
}

/* 卡片底部保存栏 */
.card-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 13px 20px;
  background: var(--bg2);
  flex-wrap: wrap;
}

.card-feedback {
  flex: 1 1 220px;
  min-width: 0;
}

.feishu-message {
  margin: 0;
  font-size: 12.5px;
  color: var(--green);
}

.feishu-message.bad {
  color: var(--red);
}

.dirty-hint {
  margin: 0;
  font-size: 12px;
  font-weight: 500;
  color: var(--warning, #d97706);
}

.muted-hint {
  margin: 0;
  font-size: 12px;
  color: var(--text-faint);
}

.feishu-actions {
  display: flex;
  gap: 10px;
  flex: none;
}

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
  transition: opacity .15s ease, background .15s ease;
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

/* 检查清单 */
.feishu-checklist ul {
  margin: 0;
  padding: 0;
  list-style: none;
}

.feishu-checklist li {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 12px 20px;
  border-bottom: 1px solid var(--border);
  font-size: 12.5px;
  color: var(--text-dim);
}

.feishu-checklist li:last-child {
  border-bottom: 0;
}

.check-circle {
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

.feishu-checklist li.ok .check-circle {
  border-color: var(--green);
  background: color-mix(in srgb, var(--green) 14%, transparent);
  color: var(--green);
}

.feishu-checklist li.ok .what {
  color: var(--text);
  font-weight: 500;
}

.feishu-checklist li .how {
  margin-left: auto;
  text-align: right;
  font-size: 11.5px;
  color: var(--text-faint);
  max-width: 58%;
}

/* 配置引导 */
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

.help-toggle:hover {
  background: var(--bg2);
}

.help-title {
  display: inline-flex;
  align-items: center;
  gap: 8px;
}

.help-toggle .chev {
  color: var(--text-faint);
  transition: transform .15s ease;
}

.help-toggle .chev.open {
  transform: rotate(90deg);
}

.help-body {
  padding: 14px 20px 18px;
  border-top: 1px solid var(--border);
  background: var(--bg2);
}

.help-body ol,
.help-body ul {
  margin: 0;
  padding-left: 18px;
}

.help-body li {
  margin-bottom: 8px;
  font-size: 12.5px;
  color: var(--text-dim);
  line-height: 1.75;
}

.help-body li > ul {
  margin: 4px 0;
}

.help-body li > ul > li {
  margin-bottom: 4px;
  list-style: circle;
}

.help-body h3 {
  margin: 16px 0 6px;
  font-size: 12.5px;
  color: var(--text);
}

.help-body a {
  color: var(--accent);
}

.help-body code {
  padding: 1px 5px;
  border-radius: 4px;
  background: var(--bg1);
  border: 1px solid var(--border);
  font-family: var(--mono);
  font-size: 11.5px;
}

.help-body .tip {
  display: block;
  margin-top: 3px;
  color: var(--text-faint);
  font-size: 11.5px;
}

@media (max-width: 768px) {
  .page-header {
    flex-direction: column;
  }
  .hero-master-row {
    align-items: flex-start;
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
  .feishu-checklist li {
    flex-wrap: wrap;
    padding: 12px 16px;
  }
  .feishu-checklist li .how {
    margin-left: 28px;
    text-align: left;
    max-width: none;
  }
}
</style>

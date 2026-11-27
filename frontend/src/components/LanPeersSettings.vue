<template>
  <div class="lan-peers-settings" data-test="lan-peers-settings">
    <header class="page-header">
      <div class="head-text">
        <h1>局域网协作</h1>
        <p>连接同一局域网（或 Tailscale 私网）里的另一台星瑶 / A2A 智能体，直接互派任务并自动回收结果。</p>
      </div>
      <div class="header-actions">
        <button
          class="refresh-button"
          type="button"
          title="刷新节点与本机状态"
          data-test="lan-peers-refresh"
          :disabled="busy"
          @click="loadAll()"
        >
          <AppIcon name="refresh" :size="14" :class="{ 'icon-spin': busy }" />
        </button>
      </div>
    </header>

    <div v-if="notice" class="notice-bar" :class="{ bad: noticeBad }" data-test="lan-peers-notice">
      <AppIcon :name="noticeBad ? 'triangle-alert' : 'check-circle'" :size="14" class="notice-icon" />
      <span class="notice-text">{{ notice }}</span>
      <button
        type="button"
        class="notice-close"
        title="关闭提示"
        aria-label="关闭提示"
        @click="notice = ''"
      >
        <AppIcon name="x" :size="13" />
      </button>
    </div>

    <!-- 本机接入信息卡片 -->
    <section class="setting-card local-card" data-test="lan-peers-local-card">
      <div class="hero-master-row">
        <div class="hero-copy">
          <div class="hero-title-line">
            <h2>本机作为被控端</h2>
            <span class="state-pill" :class="{ on: localInfo.running }">
              {{ localInfo.running ? '监听中' : '未启动' }}
            </span>
          </div>
          <p>供同一局域网（或 Tailscale 私网）的另一台星瑶或支持 A2A 协议的 Agent 向本机派发任务。</p>
        </div>
        <div class="head-buttons">
          <button
            class="secondary-button"
            type="button"
            data-test="lan-peers-start-local"
            :disabled="busy"
            @click="loadLocalInfo(true)"
          >
            <AppIcon :name="localInfo.running && localInfo.token ? 'refresh' : 'play'" :size="13" />
            <span>{{ localActionLabel }}</span>
          </button>
          <button
            class="primary-button"
            type="button"
            data-test="lan-peers-copy-import"
            :disabled="busy || !localInfo.import_json || !localInfo.token"
            @click="copyText(localInfo.import_json, '已复制一键导入配置，可粘贴到另一台电脑')"
          >
            <AppIcon name="copy" :size="13" />
            <span>复制一键导入配置</span>
          </button>
        </div>
      </div>

      <div class="local-status-strip" :class="{ on: localInfo.running }">
        <span class="dot" />
        <span class="label">{{ localInfo.running ? '服务监听中' : '本地接入服务未启动' }}</span>
        <span class="hint">
          <template v-if="localInfo.running">
            {{ localInfo.lan_ip ? `${localInfo.lan_ip}${localInfo.a2a_port ? ` · A2A :${localInfo.a2a_port}` : ''}${localInfo.lan_port ? ` · WS :${localInfo.lan_port}` : ''}` : '已就绪，可接收局域网远程派发' }}
          </template>
          <template v-else>
            点击右上角「启动并获取信息」开启本机监听
          </template>
        </span>
      </div>

      <div class="endpoint-rows">
        <div class="setting-row">
          <div class="setting-copy">
            <h3>A2A 接口地址 <span class="rec-badge">推荐</span></h3>
            <p>跨 Agent 通用 HTTP 协议入口</p>
          </div>
          <div class="endpoint-control">
            <code class="endpoint-box mono" :class="{ empty: !localInfo.a2a_url }">{{ localInfo.a2a_url || '启动服务后自动生成' }}</code>
            <button
              v-if="localInfo.a2a_url"
              class="secondary-button compact"
              type="button"
              @click="copyText(localInfo.a2a_url, '已复制 A2A 地址')"
            >复制</button>
          </div>
        </div>

        <div class="setting-row">
          <div class="setting-copy">
            <h3>WebSocket 地址</h3>
            <p>星瑶直连低延迟通道</p>
          </div>
          <div class="endpoint-control">
            <code class="endpoint-box mono" :class="{ empty: !localInfo.ws_url }">{{ localInfo.ws_url || '未启动' }}</code>
            <button
              v-if="localInfo.ws_url"
              class="secondary-button compact"
              type="button"
              @click="copyText(localInfo.ws_url, '已复制 WS 地址')"
            >复制</button>
          </div>
        </div>

        <div class="setting-row">
          <div class="setting-copy">
            <h3>配对令牌</h3>
            <p>Bearer Token，请妥善保管</p>
          </div>
          <div class="endpoint-control">
            <code class="endpoint-box mono" :class="{ empty: !localInfo.token }">{{ localTokenDisplay }}</code>
            <button
              v-if="localInfo.token"
              class="secondary-button compact"
              type="button"
              data-test="lan-peers-toggle-local-token"
              @click="showLocalToken = !showLocalToken"
            >{{ showLocalToken ? '隐藏' : '显示' }}</button>
            <button
              v-if="localInfo.token"
              class="secondary-button compact"
              type="button"
              @click="copyText(localInfo.token, '已复制配对令牌')"
            >复制</button>
          </div>
        </div>
      </div>
    </section>

    <!-- 已配置远程节点列表 -->
    <section class="setting-group">
      <div class="group-title-row">
        <div class="group-title">
          <AppIcon name="layers" :size="14" />
          <span>远程协作节点</span>
          <span class="count-badge">{{ peers.length }}</span>
        </div>
        <button
          class="primary-button compact-btn"
          type="button"
          data-test="lan-peers-add-btn"
          :disabled="busy"
          @click="openCreateForm"
        >
          <AppIcon name="plus" :size="13" :stroke-width="2.2" />
          <span>添加节点</span>
        </button>
      </div>

      <div v-if="!peers.length" class="setting-card empty-card" data-test="lan-peers-empty">
        <div class="empty-icon">
          <AppIcon name="layers" :size="18" />
        </div>
        <div class="empty-title">还没有配置远程协作节点</div>
        <p class="empty-desc">点击右上角「添加节点」，填入另一台电脑的星瑶或 A2A Agent 地址即可开始跨机派活。</p>
      </div>

      <div v-else class="peers-list" data-test="lan-peers-list">
        <div
          v-for="peer in peers"
          :key="peer.id"
          class="setting-card peer-card"
          :class="{ disabled: !peer.enabled }"
          :data-test="`lan-peer-card-${peer.id}`"
        >
          <div class="peer-main">
            <div class="peer-title-row">
              <span
                class="peer-dot"
                :class="{
                  on: peer.enabled && peer.last_status === 'online',
                  bad: peer.enabled && (peer.last_status === 'error' || peer.last_status === 'offline'),
                }"
              />
              <strong class="peer-name">{{ peer.name }}</strong>
              <span class="mono peer-id">{{ peer.id }}</span>
              <span class="proto-badge" :class="protocolLabel(peer) === 'LAN WS' ? 'ws' : 'a2a'">{{ protocolLabel(peer) }}</span>
              <span v-if="!peer.enabled" class="state-pill">已停用</span>
              <span
                v-else-if="peer.last_status"
                class="state-pill"
                :class="{ on: peer.last_status === 'online', bad: peer.last_status === 'error' || peer.last_status === 'offline' }"
              >{{ peerStatusLabel(peer.last_status) }}</span>
            </div>
            <div class="peer-url-box mono">{{ peer.url }}</div>
            <div class="peer-meta">
              <span>令牌：{{ peer.has_token ? (peer.token_plaintext ? '已保存（明文降级）' : '已加密保存') : '未配置' }}</span>
              <span v-if="peer.last_agent_name">对端：{{ peer.last_agent_name }}</span>
              <span v-if="peer.default_session">隔离会话：{{ peer.default_session }}</span>
              <span v-if="peer.description">{{ peer.description }}</span>
            </div>
            <p v-if="peer.last_error" class="peer-err">{{ peer.last_error }}</p>
          </div>
          <div class="peer-actions">
            <button
              class="secondary-button compact"
              type="button"
              :data-test="`lan-peer-test-${peer.id}`"
              :disabled="busy"
              @click="testPeer(peer.id)"
            >测试</button>
            <button
              class="secondary-button compact"
              type="button"
              :data-test="`lan-peer-edit-${peer.id}`"
              :disabled="busy"
              @click="openEditForm(peer)"
            >编辑</button>
            <button
              class="secondary-button compact"
              type="button"
              :data-test="`lan-peer-toggle-${peer.id}`"
              :disabled="busy"
              @click="togglePeerEnabled(peer)"
            >{{ peer.enabled ? '停用' : '启用' }}</button>
            <button
              class="secondary-button compact danger-soft"
              type="button"
              :data-test="`lan-peer-delete-${peer.id}`"
              :disabled="busy"
              @click="removePeer(peer.id)"
            >删除</button>
          </div>
        </div>
      </div>
    </section>

    <!-- 添加/编辑表单 -->
    <section v-if="showForm" class="setting-group" data-test="lan-peer-form-section">
      <div class="setting-card form-card">
        <div class="form-head">
          <div class="form-head-title">
            <AppIcon name="layers" :size="14" />
            <h3>{{ editingId ? `编辑节点：${editingId}` : '添加远程协作节点' }}</h3>
          </div>
          <button type="button" class="secondary-button compact" @click="showForm = false">收起</button>
        </div>

        <div class="form-body">
          <div class="import-box">
            <label class="field-label">快捷导入：粘贴另一台电脑复制的「一键导入配置」JSON（可选）</label>
            <div class="import-row">
              <textarea
                v-model="importRaw"
                data-test="lan-peer-import-input"
                class="text-field text-area mono import-textarea"
                rows="2"
                placeholder='{"name":"星瑶 (192.168.1.25)","url":"http://192.168.1.25:8765/a2a","token":"..."}'
              />
              <button
                type="button"
                class="secondary-button compact"
                data-test="lan-peer-apply-import"
                @click="applyImportJson"
              >解析填充</button>
            </div>
          </div>

          <div class="form-grid">
            <div class="form-field">
              <label class="field-label">节点 ID（英文/数字/_/-）</label>
              <input
                v-model="form.id"
                data-test="lan-peer-form-id"
                class="text-field mono"
                type="text"
                placeholder="如 office-pc（留空自动生成）"
                :disabled="Boolean(editingId) || busy"
              />
            </div>
            <div class="form-field">
              <label class="field-label">显示名称</label>
              <input
                v-model="form.name"
                data-test="lan-peer-form-name"
                class="text-field"
                type="text"
                placeholder="如 公司台式机"
                :disabled="busy"
              />
            </div>
            <div class="form-field form-field--wide">
              <label class="field-label">节点地址（URL）</label>
              <input
                v-model="form.url"
                data-test="lan-peer-form-url"
                class="text-field mono"
                type="text"
                placeholder="http://192.168.1.25:8765/a2a 或 ws://192.168.1.25:8765/"
                :disabled="busy"
              />
            </div>
            <div class="form-field">
              <label class="field-label">通信协议</label>
              <select v-model="form.protocol" data-test="lan-peer-form-protocol" class="text-field" :disabled="busy">
                <option value="auto">自动识别（按 http/ws 判断）</option>
                <option value="a2a">A2A HTTP（推荐，跨 Agent 通用）</option>
                <option value="lan_ws">星瑶 LAN WebSocket</option>
              </select>
            </div>
            <div class="form-field">
              <label class="field-label">配对令牌（Bearer Token）</label>
              <input
                v-model="form.token"
                data-test="lan-peer-form-token"
                class="text-field mono"
                type="password"
                :placeholder="editingId ? '留空表示保留原令牌' : '粘贴对端配对令牌'"
                :disabled="busy"
              />
            </div>
            <div class="form-field">
              <label class="field-label">对端默认隔离会话（可选）</label>
              <input
                v-model="form.default_session"
                data-test="lan-peer-form-session"
                class="text-field mono"
                type="text"
                placeholder="留空按本机主机名隔离"
                :disabled="busy"
              />
            </div>
            <div class="form-field">
              <label class="field-label">对端默认工作目录（可选）</label>
              <input
                v-model="form.default_workdir"
                data-test="lan-peer-form-workdir"
                class="text-field mono"
                type="text"
                placeholder="如 E:/workspace/project"
                :disabled="busy"
              />
            </div>
            <div class="form-field form-field--wide">
              <label class="field-label">备注 / 能力说明（供星瑶选节点参考，可选）</label>
              <input
                v-model="form.description"
                data-test="lan-peer-form-desc"
                class="text-field"
                type="text"
                placeholder="如：带 RTX 4090 GPU 的 Windows 主机，负责编译测试与跑模型"
                :disabled="busy"
              />
            </div>
          </div>
        </div>

        <div class="card-footer">
          <span class="muted-hint">
            <AppIcon name="shield" :size="12.5" />
            <span>令牌使用系统凭据加密保存到本地，绝不在工具响应或页面回显明文。</span>
          </span>
          <div class="footer-actions">
            <button type="button" class="secondary-button" :disabled="busy" @click="showForm = false">取消</button>
            <button
              type="button"
              class="primary-button"
              data-test="lan-peer-form-save"
              :disabled="busy"
              @click="submitForm"
            >{{ busy ? '保存中…' : '保存节点' }}</button>
          </div>
        </div>
      </div>
    </section>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue';
import AppIcon from './AppIcon.vue';
import { useAppStore } from '../stores/appStore';
import type { LanPeerInfo, LanPeerLocalInfo } from '../types/pywebview';

const peers = ref<LanPeerInfo[]>([]);
const localInfo = ref<LanPeerLocalInfo>({ ok: true, running: false });
const showLocalToken = ref(false);
const busy = ref(false);
const notice = ref('');
const noticeBad = ref(false);

const showForm = ref(false);
const editingId = ref('');
const importRaw = ref('');
const form = reactive({
  id: '',
  name: '',
  url: '',
  protocol: 'auto',
  token: '',
  default_session: '',
  default_workdir: '',
  description: '',
  enabled: true,
});

const localTokenDisplay = computed(() => {
  if (!localInfo.value.token) return '尚未签发（启动后生成）';
  return showLocalToken.value ? localInfo.value.token : '••••••••••••••••••••••••';
});

const localActionLabel = computed(() => {
  if (!localInfo.value.running) return '启动并获取信息';
  if (!localInfo.value.token) return '签发配对令牌';
  return '刷新本机信息';
});

function refreshSlashCommands() {
  try {
    void useAppStore().loadSlashCommands();
  } catch {
    // 无 Pinia 上下文的独立组件单测跳过
  }
}

function api() {
  return (window as any).pywebview?.api;
}

function setNotice(msg: string, bad = false) {
  notice.value = msg;
  noticeBad.value = bad;
}

function protocolLabel(peer: LanPeerInfo): string {
  if (peer.protocol === 'lan_ws' || (peer.protocol === 'auto' && /^wss?:\/\//i.test(peer.url))) {
    return 'LAN WS';
  }
  return 'A2A HTTP';
}

function peerStatusLabel(status: string): string {
  if (status === 'online') return '在线';
  if (status === 'offline') return '离线';
  if (status === 'error') return '异常';
  return status;
}

async function loadPeers() {
  const res = await api()?.get_lan_peers?.();
  if (res?.ok && Array.isArray(res.peers)) {
    peers.value = res.peers;
  }
}

async function loadLocalInfo(ensureStarted = false) {
  busy.value = true;
  try {
    const res = await api()?.get_lan_peer_local_info?.(ensureStarted);
    if (res?.ok) {
      localInfo.value = res;
      if (ensureStarted) {
        setNotice(res.running ? '本机局域网接入服务已就绪。' : '本机服务尚未就绪。');
      }
    } else if (res?.msg) {
      setNotice(res.msg, true);
    }
  } finally {
    busy.value = false;
  }
}

async function loadAll() {
  busy.value = true;
  try {
    await Promise.all([loadPeers(), loadLocalInfo(false)]);
  } finally {
    busy.value = false;
  }
}

function openCreateForm() {
  editingId.value = '';
  importRaw.value = '';
  form.id = '';
  form.name = '';
  form.url = '';
  form.protocol = 'auto';
  form.token = '';
  form.default_session = '';
  form.default_workdir = '';
  form.description = '';
  form.enabled = true;
  showForm.value = true;
}

function openEditForm(peer: LanPeerInfo) {
  editingId.value = peer.id;
  importRaw.value = '';
  form.id = peer.id;
  form.name = peer.name;
  form.url = peer.url;
  form.protocol = peer.protocol || 'auto';
  form.token = '';
  form.default_session = peer.default_session || '';
  form.default_workdir = peer.default_workdir || '';
  form.description = peer.description || '';
  form.enabled = peer.enabled;
  showForm.value = true;
}

function applyImportJson() {
  const raw = importRaw.value.trim();
  if (!raw) return;
  try {
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== 'object') {
      setNotice('导入配置必须是 JSON 对象', true);
      return;
    }
    if (!editingId.value && typeof parsed.id === 'string') form.id = parsed.id;
    if (typeof parsed.name === 'string') form.name = parsed.name;
    if (typeof parsed.url === 'string') form.url = parsed.url;
    if (typeof parsed.protocol === 'string') form.protocol = parsed.protocol;
    if (typeof parsed.token === 'string') form.token = parsed.token;
    if (typeof parsed.description === 'string') form.description = parsed.description;
    setNotice('已解析导入配置，确认无误后点击「保存节点」。');
  } catch {
    setNotice('JSON 解析失败，请检查粘贴内容是否完整。', true);
  }
}

async function submitForm() {
  busy.value = true;
  try {
    const res = await api()?.save_lan_peer?.({
      id: editingId.value || form.id.trim(),
      name: form.name.trim(),
      url: form.url.trim(),
      protocol: form.protocol,
      token: form.token.trim(),
      default_session: form.default_session.trim(),
      default_workdir: form.default_workdir.trim(),
      description: form.description.trim(),
      enabled: form.enabled,
    });
    if (!res?.ok) {
      setNotice(res?.msg || '保存节点失败', true);
      return;
    }
    if (Array.isArray(res.peers)) peers.value = res.peers;
    showForm.value = false;
    refreshSlashCommands();
    setNotice('节点配置已保存。');
  } finally {
    busy.value = false;
  }
}

async function testPeer(peerId: string) {
  busy.value = true;
  try {
    const res = await api()?.test_lan_peer?.(peerId);
    if (Array.isArray(res?.peers)) peers.value = res.peers;
    if (res?.ok && res?.online) {
      setNotice(`节点 ${peerId} 探测成功：${res.msg || '在线'}`);
    } else {
      setNotice(`节点 ${peerId} 探测失败：${res?.msg || '连接异常'}`, true);
    }
  } finally {
    busy.value = false;
  }
}

async function togglePeerEnabled(peer: LanPeerInfo) {
  busy.value = true;
  try {
    const res = await api()?.save_lan_peer?.({
      ...peer,
      token: '',
      enabled: !peer.enabled,
    });
    if (res?.ok && Array.isArray(res.peers)) {
      peers.value = res.peers;
      refreshSlashCommands();
      setNotice(`已${!peer.enabled ? '启用' : '停用'}节点 ${peer.name}。`);
    } else if (res?.msg) {
      setNotice(res.msg, true);
    }
  } finally {
    busy.value = false;
  }
}

async function removePeer(peerId: string) {
  busy.value = true;
  try {
    const res = await api()?.delete_lan_peer?.(peerId);
    if (res?.ok && Array.isArray(res.peers)) {
      peers.value = res.peers;
      if (editingId.value === peerId) showForm.value = false;
      refreshSlashCommands();
      setNotice(`已删除节点 ${peerId}。`);
    } else if (res?.msg) {
      setNotice(res.msg, true);
    }
  } finally {
    busy.value = false;
  }
}

async function copyText(text: string | undefined, okMsg: string) {
  if (!text) return;
  try {
    await navigator.clipboard?.writeText(text);
    setNotice(okMsg);
  } catch {
    setNotice('复制失败，请手动选中复制。', true);
  }
}

onMounted(() => {
  void loadAll();
});
</script>

<style scoped>
.lan-peers-settings {
  max-width: 880px;
  margin: 0 auto;
  padding-bottom: 36px;
}

.page-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 18px;
  margin-bottom: 22px;
}

.head-text h1 {
  margin: 0 0 6px;
  font-size: 22px;
  font-weight: 600;
  letter-spacing: -0.02em;
  color: var(--text);
}

.head-text p {
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
  transition: background .15s ease, color .15s ease, border-color .15s ease;
}

.refresh-button:hover:not(:disabled) {
  color: var(--text);
  background: var(--bg3);
}

.refresh-button:disabled {
  opacity: .5;
  cursor: not-allowed;
}

.notice-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 16px;
  padding: 10px 14px;
  border-radius: 10px;
  border: 1px solid color-mix(in srgb, var(--green) 35%, var(--border));
  background: color-mix(in srgb, var(--green) 8%, var(--bg1));
  color: var(--text);
  font-size: 12.5px;
  line-height: 1.45;
}

.notice-icon {
  flex: none;
  color: var(--green);
}

.notice-text {
  flex: 1 1 auto;
  min-width: 0;
}

.notice-close {
  flex: none;
  width: 22px;
  height: 22px;
  display: grid;
  place-items: center;
  padding: 0;
  border: none;
  border-radius: 6px;
  background: transparent;
  color: var(--text-faint);
  cursor: pointer;
  transition: background .14s ease, color .14s ease;
}

.notice-close:hover {
  background: color-mix(in srgb, var(--text) 8%, transparent);
  color: var(--text);
}

.notice-bar.bad {
  border-color: color-mix(in srgb, var(--red) 40%, var(--border));
  background: color-mix(in srgb, var(--red) 8%, var(--bg1));
  color: var(--red);
}

.notice-bar.bad .notice-icon {
  color: var(--red);
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
  font-size: 13.5px;
  font-weight: 600;
  color: var(--text-dim);
}

.count-badge {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 20px;
  height: 18px;
  padding: 0 6px;
  border-radius: 999px;
  background: var(--bg2);
  border: 1px solid var(--border);
  font-size: 11px;
  font-weight: 600;
  color: var(--text-dim);
  font-variant-numeric: tabular-nums;
}

.setting-card {
  border: 1px solid var(--border);
  border-radius: 12px;
  background: var(--bg1);
  overflow: hidden;
}

/* Hero 被控端卡片 */
.hero-master-row {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 18px;
  padding: 18px 20px;
  flex-wrap: wrap;
}

.hero-copy {
  min-width: 0;
  flex: 1 1 320px;
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

.hero-copy p {
  margin: 0;
  font-size: 12.5px;
  line-height: 1.55;
  color: var(--text-faint);
}

.head-buttons {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  flex: none;
}

.local-status-strip {
  display: flex;
  align-items: center;
  gap: 9px;
  flex-wrap: wrap;
  padding: 10px 20px;
  border-top: 1px solid var(--border);
  border-bottom: 1px solid var(--border);
  background: var(--bg2);
  font-size: 12.5px;
  transition: background .2s ease;
}

.local-status-strip .dot {
  flex: none;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--text-faint);
}

.local-status-strip .label {
  flex: none;
  font-weight: 600;
  color: var(--text);
}

.local-status-strip .hint {
  margin-left: auto;
  font-size: 12px;
  color: var(--text-faint);
}

.local-status-strip.on {
  background: color-mix(in srgb, var(--green) 7%, var(--bg2));
}

.local-status-strip.on .dot {
  background: var(--green);
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--green) 18%, transparent);
}

.endpoint-rows .setting-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 20px;
  padding: 14px 20px;
  border-bottom: 1px solid var(--border);
}

.endpoint-rows .setting-row:last-child {
  border-bottom: none;
}

.setting-copy {
  flex: 1 1 auto;
  min-width: 0;
}

.setting-copy h3 {
  display: flex;
  align-items: center;
  gap: 7px;
  margin: 0 0 2px;
  font-size: 13.5px;
  font-weight: 600;
  color: var(--text);
}

.setting-copy p {
  margin: 0;
  font-size: 12px;
  line-height: 1.5;
  color: var(--text-faint);
}

.rec-badge {
  display: inline-flex;
  align-items: center;
  padding: 0 6px;
  height: 17px;
  border-radius: 999px;
  font-size: 10.5px;
  font-weight: 500;
  color: var(--accent);
  background: color-mix(in srgb, var(--accent) 12%, transparent);
}

.endpoint-control {
  flex: 0 1 460px;
  width: 460px;
  max-width: 100%;
  display: flex;
  align-items: center;
  gap: 8px;
}

.endpoint-box {
  flex: 1 1 auto;
  min-width: 0;
  min-height: 32px;
  display: flex;
  align-items: center;
  padding: 5px 10px;
  border-radius: 8px;
  border: 1px solid var(--border);
  background: var(--bg2);
  font-size: 12px;
  line-height: 1.45;
  color: var(--text);
  word-break: break-all;
}

.endpoint-box.empty {
  color: var(--text-faint);
}

/* 远程节点列表 */
.peers-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.peer-card {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  padding: 15px 18px;
  flex-wrap: wrap;
  transition: border-color .15s ease;
}

.peer-card:hover {
  border-color: var(--xy-border-strong, var(--border));
}

.peer-card.disabled .peer-main {
  opacity: .68;
}

.peer-main {
  min-width: 0;
  flex: 1 1 320px;
}

.peer-title-row {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 7px;
}

.peer-dot {
  flex: none;
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--text-faint);
}

.peer-dot.on {
  background: var(--green);
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--green) 18%, transparent);
}

.peer-dot.bad {
  background: var(--red);
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--red) 16%, transparent);
}

.peer-name {
  font-size: 14px;
  font-weight: 600;
  color: var(--text);
}

.peer-id {
  padding: 1px 6px;
  border-radius: 5px;
  background: var(--bg2);
  border: 1px solid var(--border);
  font-size: 11px;
  color: var(--text-faint);
}

.proto-badge,
.state-pill {
  display: inline-flex;
  align-items: center;
  padding: 1px 8px;
  border-radius: 999px;
  font-size: 11px;
  font-weight: 500;
  border: 1px solid var(--border);
  background: var(--bg2);
  color: var(--text-faint);
}

.proto-badge.a2a {
  color: var(--accent);
  border-color: color-mix(in srgb, var(--accent) 30%, transparent);
  background: color-mix(in srgb, var(--accent) 9%, transparent);
}

.proto-badge.ws {
  color: var(--cyan, var(--accent));
  border-color: color-mix(in srgb, var(--cyan, var(--accent)) 30%, transparent);
  background: color-mix(in srgb, var(--cyan, var(--accent)) 9%, transparent);
}

.state-pill.on {
  color: var(--green);
  border-color: color-mix(in srgb, var(--green) 35%, transparent);
  background: color-mix(in srgb, var(--green) 10%, transparent);
}

.state-pill.bad {
  color: var(--red);
  border-color: color-mix(in srgb, var(--red) 35%, transparent);
  background: color-mix(in srgb, var(--red) 10%, transparent);
}

.peer-url-box {
  padding: 4px 9px;
  border-radius: 6px;
  background: var(--bg2);
  border: 1px solid var(--border);
  font-size: 12px;
  color: var(--text-dim);
  word-break: break-all;
  width: fit-content;
  max-width: 100%;
}

.peer-meta {
  display: flex;
  gap: 6px 14px;
  flex-wrap: wrap;
  margin-top: 8px;
  font-size: 12px;
  color: var(--text-faint);
}

.peer-err {
  margin: 8px 0 0;
  padding: 6px 10px;
  border-radius: 7px;
  background: color-mix(in srgb, var(--red) 8%, var(--bg2));
  border: 1px solid color-mix(in srgb, var(--red) 28%, transparent);
  font-size: 12px;
  color: var(--red);
}

.peer-actions {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}

.empty-card {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 32px 24px;
  text-align: center;
}

.empty-icon {
  width: 40px;
  height: 40px;
  border-radius: 10px;
  display: grid;
  place-items: center;
  margin-bottom: 10px;
  background: var(--bg2);
  border: 1px solid var(--border);
  color: var(--text-faint);
}

.empty-title {
  font-size: 13.5px;
  font-weight: 600;
  color: var(--text);
  margin-bottom: 4px;
}

.empty-desc {
  margin: 0;
  max-width: 440px;
  font-size: 12.5px;
  line-height: 1.55;
  color: var(--text-faint);
}

/* 添加/编辑表单 */
.form-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 13px 20px;
  border-bottom: 1px solid var(--border);
  background: var(--bg2);
}

.form-head-title {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  color: var(--text);
}

.form-head h3 {
  margin: 0;
  font-size: 14px;
  font-weight: 600;
}

.form-body {
  padding: 18px 20px;
}

.import-box {
  margin-bottom: 16px;
  padding: 12px 14px;
  border-radius: 10px;
  border: 1px dashed var(--xy-border-strong, var(--border));
  background: var(--bg2);
}

.import-box .field-label {
  display: block;
  margin-bottom: 8px;
}

.import-row {
  display: flex;
  gap: 8px;
  align-items: flex-start;
}

.import-textarea {
  flex: 1 1 auto;
  resize: vertical;
}

.form-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 14px;
}

.form-field {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.form-field--wide {
  grid-column: 1 / -1;
}

.field-label {
  font-size: 12px;
  font-weight: 500;
  color: var(--text-dim);
}

.text-field {
  width: 100%;
  box-sizing: border-box;
  min-height: 36px;
  padding: 0 12px;
  border-radius: 9px;
  border: 1px solid var(--border);
  background: var(--bg2);
  color: var(--text);
  font-size: 13px;
  transition: border-color .15s ease, box-shadow .15s ease, background .15s ease;
}

.text-field.text-area {
  min-height: 60px;
  padding: 8px 12px;
  line-height: 1.5;
}

.text-field:focus {
  outline: none;
  border-color: var(--accent);
  background: var(--bg1);
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent) 16%, transparent);
}

.text-field:disabled {
  opacity: .58;
  cursor: not-allowed;
}

.card-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 13px 20px;
  border-top: 1px solid var(--border);
  background: var(--bg2);
  flex-wrap: wrap;
}

.muted-hint {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: var(--text-faint);
}

.footer-actions {
  display: flex;
  gap: 8px;
}

/* 统一按钮样式（对齐 CloudRelaySettings / FeishuSettings） */
.primary-button,
.secondary-button {
  min-height: 34px;
  padding: 0 14px;
  border-radius: 8px;
  border: 1px solid var(--border);
  font-size: 12.5px;
  font-weight: 600;
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  white-space: nowrap;
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

.primary-button:disabled {
  opacity: .42;
  cursor: not-allowed;
}

.secondary-button {
  background: var(--bg1);
  color: var(--text);
  font-weight: 500;
}

.secondary-button:hover:not(:disabled) {
  background: var(--bg3);
  border-color: var(--xy-border-strong, var(--border));
}

.secondary-button:disabled {
  opacity: .48;
  cursor: not-allowed;
}

.secondary-button.compact,
.compact-btn {
  min-height: 30px;
  padding: 0 11px;
  font-size: 12px;
}

.secondary-button.danger-soft {
  color: var(--red);
  border-color: color-mix(in srgb, var(--red) 35%, var(--border));
}

.secondary-button.danger-soft:hover:not(:disabled) {
  background: color-mix(in srgb, var(--red) 9%, var(--bg1));
  border-color: color-mix(in srgb, var(--red) 55%, var(--border));
}

.mono {
  font-family: var(--mono, ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace);
}

@media (max-width: 720px) {
  .endpoint-rows .setting-row {
    flex-direction: column;
    align-items: stretch;
    gap: 8px;
  }

  .endpoint-control {
    width: 100%;
    flex: 1 1 auto;
  }

  .local-status-strip .hint {
    margin-left: 0;
    width: 100%;
  }

  .form-grid {
    grid-template-columns: 1fr;
  }
}
</style>

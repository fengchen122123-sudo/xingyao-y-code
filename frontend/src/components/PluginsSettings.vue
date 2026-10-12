<template>
  <div class="plugins-settings" :class="{ 'settings-page--mobile': isMobile }">
    <header class="settings-header">
      <div>
        <h1>插件</h1>
        <p>插件随安装/删除自动出现或消失；提供设置页的插件可在这里配置。</p>
      </div>
      <button class="icon-button" title="刷新插件列表" data-test="plugins-refresh" @click="store.refreshPlugins()">
        <AppIcon name="refresh" :size="14" />
      </button>
    </header>

    <section class="model-settings-card">
      <aside class="provider-list">
        <div class="list-label">已安装插件</div>
        <button
          v-for="p in store.plugins"
          :key="p.name"
          class="provider-item"
          :class="{ active: selectedName === p.name }"
          :data-test="`plugin-item-${p.name}`"
          @click="selectPlugin(p.name)"
        >
          <span class="provider-logo"><AppIcon name="layers" :size="13" /></span>
          <span class="provider-name">{{ p.name }}</span>
          <span class="plugin-badge" :class="'tone-' + badgeTone(p)">{{ badgeText(p) }}</span>
        </button>
        <div v-if="!store.plugins.length" class="provider-empty">
          暂无插件：把插件目录放入 ~/.ycode/plugins/ 后点右上角刷新
        </div>
      </aside>

      <div class="provider-detail">
        <div v-if="!selectedName" class="provider-empty">选择左侧的插件查看它的设置页</div>

        <div v-else-if="loading" class="provider-empty">正在加载设置页…</div>

        <template v-else-if="detail && detail.ok === false">
          <div class="detail-title-row"><h2>{{ selectedName }}</h2></div>
          <p class="modal-field-hint">{{ detail.msg || '该插件暂不可配置' }}</p>
          <button v-if="detail.needs === 'enable'" class="primary-button" :disabled="busy" @click="enableSelected">
            启用该插件
          </button>
          <button v-else-if="detail.needs === 'trust'" class="primary-button" :disabled="busy" @click="trustSelected">
            信任并启用
          </button>
        </template>

        <template v-else-if="page">
          <div class="detail-title-row">
            <div>
              <h2>{{ page.title || selectedName }}</h2>
              <p v-if="page.description">{{ page.description }}</p>
            </div>
          </div>

          <div v-for="(section, idx) in page.sections || []" :key="idx" class="plugin-section">
            <template v-if="section.type === 'info'">
              <div v-if="section.title" class="field-label">{{ section.title }}</div>
              <div v-for="(item, i) in section.items || []" :key="i" class="plugin-info-row">
                <span class="plugin-info-label">{{ item.label }}</span>
                <span class="plugin-info-value">{{ item.value }}</span>
                <span v-if="item.badge" class="plugin-badge" :class="'tone-' + (item.badge_tone || 'muted')">
                  {{ item.badge }}
                </span>
              </div>
            </template>

            <p v-else-if="section.type === 'text'" class="plugin-text">{{ section.content }}</p>

            <template v-else-if="section.type === 'list'">
              <div v-for="(item, i) in section.items || []" :key="i" class="plugin-list-row">
                <div class="plugin-list-main">
                  <div class="plugin-list-title">{{ item.title }}</div>
                  <div v-if="item.subtitle" class="plugin-list-sub">{{ item.subtitle }}</div>
                </div>
                <span v-if="item.badge" class="plugin-badge" :class="'tone-' + (item.badge_tone || 'muted')">
                  {{ item.badge }}
                </span>
                <div class="plugin-list-actions">
                  <button
                    v-for="(act, j) in item.actions || []"
                    :key="j"
                    class="chip-btn"
                    :class="{ danger: act.style === 'danger', primary: act.style === 'primary' }"
                    :disabled="busy"
                    :data-test="`row-action-${act.action}`"
                    @click="onRowAction(act)"
                  >{{ act.label }}</button>
                </div>
              </div>
            </template>

            <template v-else-if="section.type === 'form'">
              <div v-for="field in section.fields || []" :key="field.key" class="plugin-field">
                <label class="field-label">{{ field.label }}</label>
                <input
                  v-if="field.type === 'text' || field.type === 'password'"
                  v-model="formValues[field.key]"
                  :type="field.type === 'password' ? 'password' : 'text'"
                  :placeholder="field.placeholder || ''"
                />
                <label v-else-if="field.type === 'switch'" class="switch">
                  <input type="checkbox" v-model="formValues[field.key]" />
                  <span class="switch-track"></span>
                </label>
                <select v-else-if="field.type === 'select'" v-model="formValues[field.key]">
                  <option v-for="opt in field.options || []" :key="String(opt.value)" :value="opt.value">
                    {{ opt.label }}
                  </option>
                </select>
              </div>
              <button
                v-if="section.submit"
                class="primary-button"
                :disabled="busy"
                :data-test="`form-submit-${section.submit.action}`"
                @click="onFormSubmit(section)"
              >{{ section.submit.label || '保存' }}</button>
            </template>

            <template v-else-if="section.type === 'actions'">
              <div class="plugin-actions">
                <button
                  v-for="(act, i) in section.items || []"
                  :key="i"
                  class="chip-btn"
                  :class="{ primary: act.style === 'primary', danger: act.style === 'danger' }"
                  :disabled="busy"
                  :data-test="`plugin-action-${act.action}`"
                  @click="onRowAction(act)"
                >{{ act.label }}</button>
              </div>
            </template>
          </div>

          <div v-if="authUrl" class="plugin-auth-box">
            <div class="field-label">授权链接</div>
            <div class="plugin-auth-url">{{ authUrl }}</div>
            <div class="plugin-actions">
              <button class="chip-btn primary" data-test="open-auth-url" @click="openAuthUrl">在浏览器中打开授权页</button>
              <button class="chip-btn" data-test="copy-auth-url" @click="copyAuthUrl">复制链接</button>
            </div>
            <p v-if="polling" class="plugin-text">等待你在浏览器完成登录…（完成后本页自动刷新）</p>
          </div>
        </template>
      </div>
    </section>
  </div>
</template>

<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from 'vue';
import AppIcon from './AppIcon.vue';
import { useIsMobile } from '../composables/useViewport';
import { useAppStore } from '../stores/appStore';

const store = useAppStore();

const selectedName = ref('');
const loading = ref(false);
const busy = ref(false);
const page = ref<any>(null);
const detail = ref<any>(null);
const authUrl = ref('');
const formValues = ref<Record<string, any>>({});
const polling = ref(false);
// 与 SettingsPage 同口径：手机端列表/详情改单列堆叠（store 无移动端标记，用同一断点 composable）
const isMobile = useIsMobile();

let pollTimer: number | null = null;

function badgeText(p: any): string {
  if (p.status === 'untrusted') return '未信任';
  if (p.status === 'disabled') return '未启用';
  if (p.status === 'failed') return '加载失败';
  return '使用中';
}
function badgeTone(p: any): string {
  if (p.status === 'failed') return 'err';
  if (p.status === 'untrusted') return 'warn';
  if (p.status === 'disabled') return 'muted';
  return 'ok';
}

async function selectPlugin(name: string) {
  selectedName.value = name;
  stopPoll();
  authUrl.value = '';
  page.value = null;
  detail.value = null;
  loading.value = true;
  try {
    const res = await store.loadPluginSettings(name);
    if (res && res.ok) {
      page.value = res.page || null;
      seedFormValues();
    } else {
      detail.value = res || { ok: false, msg: '加载失败' };
    }
  } finally {
    loading.value = false;
  }
}

function seedFormValues() {
  const values: Record<string, any> = {};
  for (const section of page.value?.sections || []) {
    if (section.type === 'form') {
      for (const field of section.fields || []) {
        values[field.key] = field.value ?? (field.type === 'switch' ? false : '');
      }
    }
  }
  formValues.value = values;
}

async function runAction(action: string, params: Record<string, unknown> = {}, notify = true) {
  if (busy.value || !selectedName.value) return;
  busy.value = true;
  stopPoll();
  try {
    const res = await store.runPluginSettingsAction(selectedName.value, action, params);
    applyResult(res, notify);
  } finally {
    busy.value = false;
  }
}

function applyResult(res: any, notify = true) {
  if (!res) return;
  if (res.page) {
    page.value = res.page;
    detail.value = null;
    seedFormValues();
  }
  if (res.auth_url) authUrl.value = String(res.auth_url);
  if (res.done) authUrl.value = '';
  if (res.poll) startPoll(res.poll);
  if (notify && res.msg) store.notifyModal('插件', res.msg);
}

async function onRowAction(act: any) {
  if (act.style === 'danger') {
    const ok = await store.askConfirm('危险操作', '请确认', `确定执行「${act.label}」吗？`);
    if (!ok) return;
  }
  runAction(act.action, act.params || {});
}

function onFormSubmit(section: any) {
  const submit = section.submit || {};
  runAction(submit.action, { values: { ...formValues.value } });
}

async function enableSelected() {
  if (!selectedName.value) return;
  busy.value = true;
  try {
    await store.togglePlugin(selectedName.value, true);
    await selectPlugin(selectedName.value);
  } finally {
    busy.value = false;
  }
}

async function trustSelected() {
  if (!selectedName.value) return;
  const ok = await store.askConfirm(
    '信任插件', '信任后插件代码将被执行',
    `确定信任插件「${selectedName.value}」并启用吗？`
  );
  if (!ok) return;
  busy.value = true;
  try {
    await store.trustPlugin(selectedName.value);
    await store.togglePlugin(selectedName.value, true);
    await selectPlugin(selectedName.value);
  } finally {
    busy.value = false;
  }
}

function startPoll(poll: any) {
  stopPoll();
  const interval = Math.max(1500, Number(poll?.interval_ms) || 3000);
  const pollAction = String(poll?.action || '');
  const pollParams = poll?.params || {};
  if (!pollAction) return;
  let count = 0;
  polling.value = true;
  pollTimer = window.setInterval(async () => {
    count += 1;
    if (count > 200 || !selectedName.value) {
      stopPoll();
      store.notifyModal('插件', '等待超时或已切换插件，已停止自动刷新');
      return;
    }
    const res = await store.runPluginSettingsAction(selectedName.value, pollAction, pollParams);
    applyResult(res, false);
    if (!res || res.done) stopPoll();
  }, interval);
}

function stopPoll() {
  if (pollTimer !== null) {
    window.clearInterval(pollTimer);
    pollTimer = null;
  }
  polling.value = false;
}

async function openAuthUrl() {
  if (!authUrl.value) return;
  const ok = await store.openExternalUrl(authUrl.value);
  if (!ok) store.notifyModal('插件', '无法打开系统浏览器，请手动复制链接打开');
}

async function copyAuthUrl() {
  try {
    await navigator.clipboard.writeText(authUrl.value);
    store.notifyModal('插件', '授权链接已复制');
  } catch {
    store.notifyModal('插件', '复制失败，请手动选择链接复制');
  }
}

watch(
  () => (store.plugins || []).map((p: any) => p.name).join(','),
  (names) => {
    // 插件被删除/改名：当前选中项消失时清空详情（"删除即消失"）
    if (selectedName.value && !String(names).split(',').includes(selectedName.value)) {
      stopPoll();
      selectedName.value = '';
      page.value = null;
      detail.value = null;
      authUrl.value = '';
      polling.value = false;
    }
  }
);

onBeforeUnmount(() => stopPoll());
</script>

<style scoped>
.plugins-settings { max-width: 1120px; margin: 0 auto; }

/* 标题栏 / 刷新按钮 / 左列表右详情骨架：与 SettingsPage 同款视觉 */
.settings-header { display: flex; align-items: flex-end; justify-content: space-between; margin: 0 auto 26px; }
.settings-header h1 { margin: 0 0 10px; font-size: 32px; letter-spacing: -1px; }
.settings-header p { margin: 0; color: var(--text-faint); font-size: 13px; }
.icon-button { background: transparent; color: var(--text-dim); border: 0; font-size: 20px; }
.icon-button:hover { color: var(--text); }
.model-settings-card { display: grid; grid-template-columns: 280px minmax(0, 1fr); min-height: 610px; border: 1px solid var(--border); border-radius: 16px; background: var(--bg1); overflow: hidden; }
.provider-list { padding: 16px 12px; border-right: 1px solid var(--border); background: var(--bg2); overflow-y: auto; }
.list-label { padding: 4px 10px 10px; color: var(--text-faint); font-size: 11px; font-weight: 600; }
.provider-empty { padding: 12px 10px; color: var(--text-faint); font-size: 11px; }
.provider-item { width: 100%; display: flex; align-items: center; gap: 9px; padding: 9px 10px; border: 1px solid transparent; border-radius: 9px; background: transparent; color: var(--text-dim); text-align: left; cursor: pointer; }
.provider-item:hover, .provider-item.active { background: var(--bg3); color: var(--text); }
.provider-item.active { border-color: var(--border); }
.provider-logo { width: 24px; height: 24px; display: grid; place-items: center; border-radius: 7px; background: var(--accent-soft); color: var(--accent); flex-shrink: 0; }
.provider-name { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 13px; }
.provider-detail { min-width: 0; padding: 38px; overflow-y: auto; }
.detail-title-row { display: flex; justify-content: space-between; gap: 18px; margin-bottom: 28px; }
.detail-title-row h2 { margin: 0 0 8px; font-size: 20px; }
.detail-title-row p { margin: 0; color: var(--text-faint); font-size: 13px; }
.field-label { display: block; margin: 16px 0 7px; color: var(--text-dim); font-size: 12px; }
.modal-field-hint { margin: 10px 0 0; color: var(--text-faint); font-size: 12px; line-height: 1.6; }
.primary-button { min-height: 38px; margin-top: 20px; padding: 0 16px; border-radius: 9px; border: 1px solid var(--border); background: var(--text); color: var(--bg0); font-weight: 600; }
.primary-button:disabled { opacity: 0.55; cursor: not-allowed; }
.chip-btn { background: var(--bg3); border: 1px solid var(--border); color: var(--text-dim); border-radius: 6px; padding: 4px 10px; font-size: 12px; cursor: pointer; transition: 0.15s; display: inline-flex; align-items: center; }
.chip-btn:hover { border-color: var(--text-faint); color: var(--text); }
.chip-btn:disabled { opacity: 0.6; cursor: not-allowed; }
.chip-btn.primary { color: var(--accent); background: var(--accent-soft); border-color: color-mix(in srgb, var(--accent) 35%, var(--border)); }
.chip-btn.danger { color: var(--red); border-color: color-mix(in srgb, var(--red) 35%, var(--border)); }

/* 声明式渲染器新增：状态徽标 / 信息行 / 列表行 / 动作区 / 授权框 */
.plugin-badge { flex-shrink: 0; padding: 1px 8px; border: 1px solid var(--border); border-radius: 999px; background: var(--bg3); color: var(--text-faint); font-size: 10.5px; white-space: nowrap; }
.plugin-badge.tone-ok { color: var(--green); border-color: color-mix(in srgb, var(--green) 35%, var(--border)); }
.plugin-badge.tone-warn { color: var(--yellow); border-color: color-mix(in srgb, var(--yellow) 35%, var(--border)); }
.plugin-badge.tone-err { color: var(--red); border-color: color-mix(in srgb, var(--red) 35%, var(--border)); }
.plugin-badge.tone-muted { color: var(--text-faint); }
.plugin-section { margin-bottom: 20px; }
.plugin-info-row { display: flex; align-items: center; gap: 10px; padding: 9px 0; border-bottom: 1px solid var(--border); font-size: 13px; }
.plugin-info-row:last-child { border-bottom: 0; }
.plugin-info-label { min-width: 84px; color: var(--text-faint); }
.plugin-info-value { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; }
.plugin-text { margin: 8px 0; color: var(--text-dim); font-size: 12.5px; line-height: 1.7; }
.plugin-list-row { display: flex; align-items: center; gap: 10px; padding: 10px 0; border-bottom: 1px solid var(--border); }
.plugin-list-row:last-child { border-bottom: 0; }
.plugin-list-main { flex: 1; min-width: 0; }
.plugin-list-title { color: var(--text); font-size: 13px; }
.plugin-list-sub { margin-top: 3px; color: var(--text-faint); font-size: 11px; }
.plugin-list-actions { display: flex; gap: 7px; flex-shrink: 0; }
.plugin-actions { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 14px; }
.plugin-auth-box { margin-top: 20px; padding: 14px 16px; border: 1px solid var(--border); border-radius: 10px; background: var(--bg2); }
.plugin-auth-url { margin: 6px 0 0; padding: 9px 11px; border: 1px solid var(--border); border-radius: 8px; background: var(--bg1); color: var(--text-dim); font-family: var(--mono); font-size: 11.5px; word-break: break-all; }

/* 表单字段（文本 / 密码 / 下拉） */
.plugin-field input[type='text'],
.plugin-field input[type='password'],
.plugin-field select { width: 100%; min-height: 38px; box-sizing: border-box; border: 1px solid var(--border); border-radius: 9px; background: var(--bg2); color: var(--text); font-size: 13px; padding: 0 12px; outline: none; transition: 0.15s; }
.plugin-field input[type='text']:focus,
.plugin-field input[type='password']:focus,
.plugin-field select:focus { border-color: var(--accent); }

/* 开关 Pill（与 HooksSettings 同款） */
.switch { position: relative; display: inline-block; width: 34px; height: 18px; cursor: pointer; flex-shrink: 0; }
.switch input { opacity: 0; width: 0; height: 0; position: absolute; }
.switch-track { position: absolute; inset: 0; background: var(--bg3); border: 1px solid var(--border); border-radius: 99px; transition: 0.15s; }
.switch-track::before { content: ''; position: absolute; width: 12px; height: 12px; left: 2px; top: 2px; background: var(--text-faint); border-radius: 50%; transition: 0.15s; }
.switch input:checked + .switch-track { background: var(--accent); border-color: transparent; }
.switch input:checked + .switch-track::before { transform: translateX(16px); background: #fff; }

/* 手机端：列表/详情改单列堆叠（根节点由 useIsMobile 断点加上 settings-page--mobile） */
.plugins-settings.settings-page--mobile .settings-header { align-items: flex-start; }
.plugins-settings.settings-page--mobile .settings-header h1 { font-size: 24px; letter-spacing: -0.5px; }
.plugins-settings.settings-page--mobile .model-settings-card { grid-template-columns: 1fr; min-height: 0; }
.plugins-settings.settings-page--mobile .provider-list { border-right: 0; border-bottom: 1px solid var(--border); padding: 12px; }
.plugins-settings.settings-page--mobile .provider-detail { padding: 18px 16px 22px; }
</style>

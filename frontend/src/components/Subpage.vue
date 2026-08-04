<template>
  <div id="subpage" :class="{ show: visible }">
    <div class="sub-head">
      <button class="sub-back" @click="emit('close')">← 返回</button>
      <h2>{{ title }}</h2>
      <span class="sub-desc">{{ desc }}</span>
    </div>

    <div class="sub-body">
      <!-- 技能页 -->
      <div v-if="subType === 'skills'" class="sub-grid">
        <div v-if="store.skills.length === 0" class="sub-empty">暂无可用技能</div>
        <div v-for="s in store.skills" :key="s.id" class="sub-card">
          <div class="sc-top">
            <div class="sc-ico">❖</div>
            <div class="sc-name">{{ s.name || s.id }}</div>
            <label class="switch" title="启用/禁用该技能" @click.stop>
              <input type="checkbox" :checked="s.enabled" @change="onToggleSkill(s, ($event.target as HTMLInputElement).checked)" />
              <span class="switch-track"></span>
            </label>
          </div>
          <div class="sc-desc">{{ s.description || '项目/用户自定义扩展技能' }}</div>
          <div class="sc-id">{{ s.short_id || s.id }}</div>
        </div>
      </div>

      <!-- MCP 页 -->
      <div v-else-if="subType === 'mcp'">
        <div class="sub-toolbar">
          <button class="chip-btn" title="重新连接所有 MCP 服务" @click="store.reconnectMcp()">↻ 重连</button>
        </div>
        <div class="sub-grid">
          <div v-if="store.mcps.length === 0" class="sub-empty">暂无已配置的 MCP 服务</div>
          <div v-for="m in store.mcps" :key="m.name" class="sub-card mcp-card">
            <div class="sc-top">
              <div class="sc-ico">⧉</div>
              <div class="sc-name">
                {{ m.name }}
                <span v-if="!m.configured" class="mcp-preset-badge">预置</span>
              </div>
              <span class="status-dot" :class="mcpStatusClass(m)" :title="mcpStatusText(m)"></span>
              <label class="switch" :title="m.configured ? '启用/禁用该 MCP 服务' : '启用该预置服务（缺密钥会弹窗填写）'" @click.stop>
                <input type="checkbox" :checked="m.enabled" @change="onToggleMcp(m, ($event.target as HTMLInputElement).checked)" />
                <span class="switch-track"></span>
              </label>
            </div>
            <div class="sc-desc">{{ m.configured ? ('工具数: ' + (m.tools_count ?? 0) + ' · ' + mcpStatusText(m)) : (m.comment || '预置服务模板') }}</div>
          </div>
        </div>
      </div>

      <!-- 插件页 -->
      <div v-else-if="subType === 'plugins'" class="sub-grid">
        <div v-if="store.plugins.length === 0" class="sub-empty">暂无已安装的插件</div>
        <div v-for="p in store.plugins" :key="p.name" class="sub-card">
          <div class="sc-top">
            <div class="sc-ico">⬡</div>
            <div class="sc-name">
              {{ p.name }}
              <span class="pl-ver">v{{ p.version }}</span>
              <span class="pl-src" :class="p.source">{{ p.source === 'user' ? '用户' : '内置' }}</span>
            </div>
            <label class="switch" title="启用/禁用该插件（重启后完全生效）" @click.stop>
              <input type="checkbox" :checked="p.enabled" @change="store.togglePlugin(p.name, ($event.target as HTMLInputElement).checked)" />
              <span class="switch-track"></span>
            </label>
          </div>
          <div class="sc-desc">{{ p.description || '无描述' }}</div>
          <div class="sc-id">
            {{ pluginStatusText(p) }}<template v-if="p.tools.length"> · 工具: {{ p.tools.join(', ') }}</template>
          </div>
        </div>
      </div>

      <!-- 模型管理页 -->
      <div v-else class="models-page">
        <div class="model-section-title">添加供应商</div>
        <div class="model-form">
          <input v-model="newProvider.name" placeholder="供应商名称" />
          <input v-model="newProvider.base_url" placeholder="API 地址 (base_url)" style="flex:1;min-width:180px" />
          <input v-model="newProvider.api_key" type="password" placeholder="API Key" />
          <select v-model="newProvider.protocol" title="接口协议">
            <option value="openai">openai</option>
            <option value="anthropic">anthropic</option>
          </select>
          <button class="btn primary" :disabled="addingProvider" title="添加供应商" @click="submitAddProvider">
            <span v-if="addingProvider" class="mini-spin"></span>添加供应商
          </button>
        </div>

        <div class="model-section-title">添加模型</div>
        <div class="model-form">
          <select v-model="newModel.provider_id" title="选择供应商" style="min-width:140px">
            <option value="" disabled>选择供应商</option>
            <option v-for="p in store.providerOptions" :key="p.id" :value="p.id">{{ p.name }}</option>
          </select>
          <input v-model="newModel.model_name" placeholder="模型名称" />
          <input v-model.number="newModel.context_window" type="number" min="0" placeholder="上下文窗口" style="width:120px" />
          <button class="btn primary" :disabled="addingModel" title="添加模型" @click="submitAddModel">
            <span v-if="addingModel" class="mini-spin"></span>添加模型
          </button>
        </div>

        <div class="model-section-title">已配置模型</div>
        <div v-if="store.models.length === 0" class="sub-empty">暂无模型，请先在上方添加</div>
        <div v-else class="model-groups">
          <div v-for="(list, provider) in store.modelsByProvider" :key="provider" class="model-group">
            <div class="model-group-title">{{ provider }}</div>
            <div v-for="m in list" :key="m.id" class="model-row">
              <span class="m-name">{{ m.name }}</span>
              <span v-if="m.id === store.activeModelId" class="m-active">● 使用中</span>
              <span v-if="testResults[m.id]" class="m-test-result">{{ testResults[m.id] }}</span>
              <span class="model-row-actions">
                <button class="chip-btn" :disabled="testingModelId === m.id" title="测试连接" @click="testModelRow(m)">
                  <span v-if="testingModelId === m.id" class="mini-spin"></span>测试连接
                </button>
                <button class="chip-btn" title="切换为当前使用模型" @click="store.setModel(m.id)">切换使用</button>
                <button class="chip-btn danger" title="删除该模型" @click="store.deleteModel(m)">删除</button>
              </span>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- 预置 MCP 启用时的密钥输入弹窗 -->
    <div v-if="envModal.show" class="env-modal-mask" @click.self="envModal.show = false">
      <div class="env-modal">
        <div class="em-title">启用 {{ envModal.name }}</div>
        <div class="em-desc">该服务需要以下密钥，填写后会保存到 mcp.json，之后开关无需再输入</div>
        <div v-for="key in envModal.keys" :key="key" class="em-row">
          <label class="em-label">{{ key }}</label>
          <input v-model="envModal.values[key]" type="password" :placeholder="'请输入 ' + key" class="em-input" />
        </div>
        <div class="em-actions">
          <button class="chip-btn" @click="envModal.show = false">取消</button>
          <button class="chip-btn em-confirm" @click="submitEnvModal">确认启用</button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue';
import { useAppStore, type SubpageType } from '../stores/appStore';
import type { SkillInfo, McpInfo, ModelInfo, PluginInfo } from '../types/pywebview';

const props = defineProps<{
  visible: boolean;
  subType: SubpageType;
}>();

const emit = defineEmits(['close']);
const store = useAppStore();

const title = computed(() => {
  if (props.subType === 'skills') return '技能 Skills';
  if (props.subType === 'mcp') return 'MCP 服务';
  if (props.subType === 'plugins') return '插件 Plugins';
  return '模型管理';
});
const desc = computed(() => {
  if (props.subType === 'skills') return '已扫描的项目技能与用户扩展技能';
  if (props.subType === 'mcp') return '来自 mcp.json 的服务接口';
  if (props.subType === 'plugins') return '与 CLI 共用同一份 plugin.json 配置，开关状态两端同步';
  return '管理模型供应商与可用模型列表';
});

function onToggleSkill(s: SkillInfo, checked: boolean) {
  store.toggleSkill(s.id, checked);
}

function onToggleMcp(m: McpInfo, checked: boolean) {
  if (checked && m.configured === false) {
    // 预置服务首次启用：需要密钥的弹窗填写，不需要的直接启用
    const keys = m.required_inputs || [];
    if (keys.length) {
      envModal.show = true;
      envModal.name = m.name;
      envModal.keys = keys;
      envModal.values = Object.fromEntries(keys.map(k => [k, '']));
      return;
    }
    store.enableMcpServer(m.name, {});
    return;
  }
  store.toggleMcpServer(m.name, checked);
}

// 预置 MCP 密钥输入弹窗状态
const envModal = reactive({
  show: false,
  name: '',
  keys: [] as string[],
  values: {} as Record<string, string>,
});

function submitEnvModal() {
  const missing = envModal.keys.filter(k => !(envModal.values[k] || '').trim());
  if (missing.length) {
    store.showToast('请填写: ' + missing.join(', '), 'warn');
    return;
  }
  const name = envModal.name;
  const values = { ...envModal.values };
  envModal.show = false;
  store.enableMcpServer(name, values);
}

function mcpStatusClass(m: McpInfo) {
  if (!m.enabled) return 'gray';
  return m.connected ? 'green' : 'yellow';
}

function mcpStatusText(m: McpInfo) {
  if (m.configured === false) return '预置 · 未配置';
  if (!m.enabled) return '已禁用';
  return m.connected ? '已连接' : '已启用但未连接';
}

function pluginStatusText(p: PluginInfo) {
  switch (p.status) {
    case 'loaded': return '已加载';
    case 'disabled': return '已禁用';
    case 'failed': return p.error ? `加载失败: ${p.error}` : '加载失败';
    case 'overridden': return '已被同名用户插件覆盖';
    case 'pending_enable': return '已启用（重启后生效）';
    case 'pending_disable': return '已禁用（重启后生效）';
    default: return p.status || '';
  }
}

// 打开 MCP / 插件页时刷新一次最新状态
watch(
  () => [props.visible, props.subType] as const,
  ([visible, type]) => {
    if (visible && type === 'mcp') {
      store.refreshMcpServers();
    } else if (visible && type === 'plugins') {
      store.refreshPlugins();
    }
  }
);

// ---- 模型管理：添加供应商 ----
const newProvider = reactive({ name: '', base_url: '', api_key: '', protocol: 'openai' });
const addingProvider = ref(false);

async function submitAddProvider() {
  if (!newProvider.name.trim() || !newProvider.base_url.trim()) {
    store.showToast('请填写供应商名称与 API 地址', 'warn');
    return;
  }
  addingProvider.value = true;
  try {
    await store.addProvider(newProvider.name.trim(), newProvider.base_url.trim(), newProvider.api_key.trim(), newProvider.protocol);
    newProvider.name = '';
    newProvider.base_url = '';
    newProvider.api_key = '';
    newProvider.protocol = 'openai';
  } finally {
    addingProvider.value = false;
  }
}

// ---- 模型管理：添加模型 ----
const newModel = reactive({ provider_id: '', model_name: '', context_window: 32000 });
const addingModel = ref(false);

async function submitAddModel() {
  if (!newModel.provider_id || !newModel.model_name.trim()) {
    store.showToast('请选择供应商并填写模型名称', 'warn');
    return;
  }
  addingModel.value = true;
  try {
    await store.addModel(newModel.provider_id, newModel.model_name.trim(), Number(newModel.context_window) || 0);
    newModel.model_name = '';
  } finally {
    addingModel.value = false;
  }
}

// ---- 模型管理：测试连接 ----
const testingModelId = ref<string | null>(null);
const testResults = ref<Record<string, string>>({});

async function testModelRow(m: ModelInfo) {
  testingModelId.value = m.id;
  testResults.value[m.id] = '';
  const res = await store.testModel(m.id);
  testResults.value[m.id] = (res.ok ? '✓ ' : '✕ ') + res.msg;
  testingModelId.value = null;
}
</script>

<style scoped>
#subpage {
  position: absolute;
  inset: 0;
  background: var(--bg0);
  z-index: 50;
  display: none;
  flex-direction: column;
}
#subpage.show {
  display: flex;
}
.sub-head {
  height: 52px;
  border-bottom: 1px solid var(--border);
  display: flex;
  align-items: center;
  padding: 0 20px;
  gap: 12px;
  background: var(--bg1);
  flex-shrink: 0;
}
.sub-back {
  background: var(--bg2);
  border: 1px solid var(--border);
  color: var(--text);
  border-radius: 8px;
  padding: 6px 14px;
  font-size: 12.5px;
  display: flex;
  align-items: center;
  gap: 6px;
}
.sub-back:hover {
  border-color: var(--accent);
  color: var(--accent);
}
.sub-head h2 {
  font-size: 15px;
}
.sub-head .sub-desc {
  font-size: 12px;
  color: var(--text-faint);
}
.sub-body {
  flex: 1;
  overflow-y: auto;
  padding: 22px 26px;
}
.sub-grid {
  max-width: 960px;
  margin: 0 auto;
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
  gap: 14px;
}
.sub-toolbar {
  max-width: 960px;
  margin: 0 auto 14px;
  display: flex;
  justify-content: flex-end;
}
.sub-empty {
  color: var(--text-faint);
  font-size: 12.5px;
  padding: 20px 0;
  text-align: center;
  grid-column: 1 / -1;
}
.sub-card {
  background: var(--bg1);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 16px;
  transition: 0.15s;
}
.sub-card:hover {
  border-color: var(--text-faint);
}
.sub-card .sc-top {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 8px;
}
.sub-card .sc-ico {
  width: 34px;
  height: 34px;
  border-radius: 10px;
  background: var(--bg2);
  border: 1px solid var(--border);
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 16px;
  flex-shrink: 0;
  color: var(--text-dim);
}
.sub-card .sc-name {
  font-size: 13.5px;
  font-weight: 600;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.sub-card .sc-desc {
  font-size: 12px;
  color: var(--text-dim);
  line-height: 1.6;
  min-height: 38px;
}
.sub-card .sc-id {
  font-size: 10.5px;
  color: var(--text-faint);
  font-family: var(--mono);
  margin-top: 4px;
}

/* 预置角标 */
.mcp-preset-badge {
  font-size: 10px;
  padding: 0 6px;
  border-radius: 6px;
  margin-left: 4px;
  background: var(--accent-soft);
  color: var(--accent);
  font-weight: 400;
}

/* MCP 密钥输入弹窗 */
.env-modal-mask {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.55);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 1300;
}
.env-modal {
  width: 420px;
  max-width: 90vw;
  background: var(--bg1);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 18px;
  box-shadow: 0 20px 60px rgba(0, 0, 0, 0.6);
}
.em-title {
  font-size: 15px;
  font-weight: 600;
  color: var(--text);
}
.em-desc {
  font-size: 12px;
  color: var(--text-faint);
  margin: 6px 0 14px;
  line-height: 1.6;
}
.em-row {
  margin-bottom: 10px;
}
.em-label {
  display: block;
  font-size: 11.5px;
  color: var(--text-dim);
  margin-bottom: 4px;
  font-family: var(--mono);
}
.em-input {
  width: 100%;
  background: var(--bg2);
  border: 1px solid var(--border);
  border-radius: 7px;
  padding: 7px 10px;
  color: var(--text);
  font-size: 12.5px;
  outline: none;
}
.em-input:focus {
  border-color: var(--accent);
}
.em-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  margin-top: 14px;
}
.em-confirm {
  border-color: var(--accent);
  color: var(--accent);
  background: var(--accent-soft);
}
.pl-ver {
  font-size: 10.5px;
  color: var(--text-faint);
  font-weight: 400;
  margin-left: 4px;
}
.pl-src {
  font-size: 10px;
  padding: 0 6px;
  border-radius: 6px;
  margin-left: 4px;
  background: var(--bg3);
  color: var(--text-dim);
  font-weight: 400;
}
.pl-src.user {
  background: var(--accent-soft);
  color: var(--accent);
}

/* 状态圆点 */
.status-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  flex-shrink: 0;
  margin-left: auto;
}
.status-dot.green {
  background: var(--green);
}
.status-dot.gray {
  background: var(--text-faint);
}
.status-dot.yellow {
  background: var(--yellow);
}

/* 开关 Pill */
.switch {
  position: relative;
  display: inline-block;
  width: 34px;
  height: 18px;
  margin-left: auto;
  cursor: pointer;
  flex-shrink: 0;
}
.switch input {
  opacity: 0;
  width: 0;
  height: 0;
  position: absolute;
}
.switch-track {
  position: absolute;
  inset: 0;
  background: var(--bg3);
  border: 1px solid var(--border);
  border-radius: 99px;
  transition: 0.15s;
}
.switch-track::before {
  content: '';
  position: absolute;
  width: 12px;
  height: 12px;
  left: 2px;
  top: 2px;
  background: var(--text-faint);
  border-radius: 50%;
  transition: 0.15s;
}
.switch input:checked + .switch-track {
  background: var(--accent);
  border-color: transparent;
}
.switch input:checked + .switch-track::before {
  transform: translateX(16px);
  background: #fff;
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
  display: inline-flex;
  align-items: center;
}
.chip-btn:hover {
  border-color: var(--text-faint);
  color: var(--text);
}
.chip-btn:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}
.chip-btn.danger:hover {
  border-color: var(--red);
  color: var(--red);
  background: rgba(248, 81, 73, 0.1);
}

.btn {
  border-radius: 8px;
  padding: 6px 14px;
  font-size: 12.5px;
  border: 1px solid var(--border);
  background: var(--bg3);
  color: var(--text);
  transition: 0.15s;
  display: inline-flex;
  align-items: center;
  cursor: pointer;
}
.btn:hover {
  border-color: var(--text-faint);
}
.btn:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}
.btn.primary {
  background: var(--accent);
  border: none;
  color: #fff;
  font-weight: 600;
}

.mini-spin {
  display: inline-block;
  width: 9px;
  height: 9px;
  border: 2px solid var(--accent);
  border-top-color: transparent;
  border-radius: 50%;
  animation: mini-spin 0.8s linear infinite;
  margin-right: 5px;
  vertical-align: -1px;
}
@keyframes mini-spin {
  to { transform: rotate(360deg); }
}

/* 模型管理页 */
.models-page {
  max-width: 760px;
  margin: 0 auto;
}
.model-section-title {
  font-size: 12px;
  color: var(--text-faint);
  letter-spacing: 1px;
  text-transform: uppercase;
  margin: 18px 0 8px;
}
.model-section-title:first-child {
  margin-top: 0;
}
.model-form {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
  background: var(--bg1);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 12px;
}
.model-form input,
.model-form select {
  background: var(--bg2);
  border: 1px solid var(--border);
  border-radius: 6px;
  color: var(--text);
  padding: 6px 9px;
  font-size: 12.5px;
  outline: none;
  transition: 0.15s;
  font-family: inherit;
}
.model-form input:focus,
.model-form select:focus {
  border-color: var(--accent);
  box-shadow: 0 0 0 2px var(--accent-soft);
}
.model-groups {
  display: flex;
  flex-direction: column;
  gap: 14px;
}
.model-group-title {
  font-size: 12px;
  font-weight: 600;
  color: var(--text-dim);
  margin-bottom: 6px;
}
.model-row {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 9px 12px;
  border-radius: 8px;
  background: var(--bg2);
  border: 1px solid var(--border);
  margin-bottom: 6px;
  flex-wrap: wrap;
}
.model-row .m-name {
  font-weight: 600;
  font-size: 12.5px;
}
.model-row .m-active {
  font-size: 10.5px;
  color: var(--green);
}
.model-row .m-test-result {
  font-size: 11px;
  color: var(--text-faint);
}
.model-row-actions {
  margin-left: auto;
  display: flex;
  gap: 6px;
}
</style>

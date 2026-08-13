<template>
  <div class="settings-page">
    <aside class="settings-nav">
      <button class="back-workspace" @click="store.closeModelSettings()">← 返回工作区</button>
      <div class="nav-section">基础设置</div>
      <button class="nav-item" :class="{ active: activeSection === 'models' }" @click="activeSection = 'models'"><span>▤</span>模型配置</button>
      <button data-test="token-statistics-nav" class="nav-item" :class="{ active: activeSection === 'tokens' }" @click="activeSection = 'tokens'"><span>◔</span>Token 统计</button>
      <div class="nav-spacer"></div>
      <div class="settings-user"><span class="avatar">Y</span><span>Y-CODE</span><span>⚙</span></div>
    </aside>

    <main class="settings-main">
      <template v-if="activeSection === 'models'">
      <header class="settings-header">
        <div>
          <h1>模型配置</h1>
          <p>CLI 与 GUI 共用同一份厂商、模型和 API 配置。</p>
        </div>
        <button class="icon-button" title="刷新模型配置" @click="store.openModelSettings()">↻</button>
      </header>

      <section class="model-settings-card">
        <aside class="provider-list">
          <div class="list-label">已配置厂商</div>
          <button
            v-for="provider in store.providers"
            :key="provider.id"
            :data-test="`provider-${provider.id}`"
            class="provider-item"
            :class="{ active: selectedKind === 'provider' && selectedId === provider.id }"
            @click="selectProvider(provider.id)"
          >
            <span class="provider-logo cube">◇</span>
            <span class="provider-name">{{ provider.name }}</span>
            <span class="provider-state ready"></span>
          </button>
          <div v-if="!store.providers.length" class="provider-empty">暂无已配置厂商</div>

          <button
            data-test="add-provider-toggle"
            class="add-provider-row"
            :class="{ active: showAddProviders }"
            @click="showAddProviders = !showAddProviders"
          >
            <span>＋ 添加厂商</span><span>{{ showAddProviders ? '⌃' : '⌄' }}</span>
          </button>

          <div v-if="showAddProviders" class="add-provider-choices">
            <div class="list-label">预置厂商</div>
            <button
              v-for="preset in store.modelPresets"
              :key="preset.id"
              :data-test="`preset-${preset.id}`"
              class="provider-item"
              :class="{ active: selectedKind === 'preset' && selectedId === preset.id }"
              @click="selectPreset(preset.id)"
            >
              <span class="provider-logo">{{ preset.name.slice(0, 1) }}</span>
              <span class="provider-name">{{ preset.name }}</span>
              <span class="provider-state" :class="{ ready: configuredPresetIds.has(preset.id) }"></span>
            </button>
            <button
              data-test="add-custom-provider"
              class="add-provider-row custom-provider-row"
              :class="{ active: selectedKind === 'custom' }"
              @click="startCustomProvider"
            >＋ 添加自定义厂商</button>
          </div>

          <!-- 副模型入口（红框位置）：点击后在右侧设置副模型 -->
          <button
            data-test="secondary-entry"
            class="secondary-entry-row"
            :class="{ active: selectedKind === 'secondary' }"
            @click="selectSecondary"
          >
            <span class="provider-logo">◈</span>
            <span class="provider-name">副模型</span>
            <span class="provider-state" :class="{ ready: !!secondaryModelId }"></span>
          </button>
        </aside>

        <div class="provider-detail">
          <template v-if="selectedPreset">
            <div class="detail-title-row">
              <div>
                <h2>{{ selectedPreset.name }}</h2>
                <p>预置厂商信息由 CLI 共享目录维护，只需填写 API Key。</p>
              </div>
              <span class="protocol-badge">{{ protocolLabel(selectedPreset.protocol) }}</span>
            </div>
            <label class="field-label">Base URL</label>
            <div class="readonly-field">{{ selectedPreset.base_url }}</div>
            <label class="field-label">API Key</label>
            <div class="key-field">
              <input
                v-model="presetApiKey"
                data-test="preset-api-key"
                :type="showPresetKey ? 'text' : 'password'"
                placeholder="输入 API Key"
              />
              <button @click="showPresetKey = !showPresetKey">{{ showPresetKey ? '隐藏' : '显示' }}</button>
            </div>
            <div class="model-preview-title">将添加以下模型</div>
            <div class="preset-models">
              <div v-for="model in selectedPreset.models" :key="model.name" class="preset-model-row">
                <span class="mono">{{ model.name }}</span>
                <span>{{ formatContext(model.context_window) }}</span>
                <span :class="['vision-tag', { on: model.supports_vision }]">
                  {{ model.supports_vision ? '多模态' : '纯文本' }}
                </span>
              </div>
            </div>
            <button class="primary-button" :disabled="saving" @click="savePreset">
              {{ configuredPresetIds.has(selectedPreset.id) ? '更新 API Key' : '保存并添加厂商' }}
            </button>
          </template>

          <template v-else-if="selectedProvider">
            <div class="detail-title-row">
              <div>
                <h2>{{ selectedProvider.name }}</h2>
                <p>{{ selectedProvider.preset_id ? '预置厂商配置' : '自定义模型供应商' }}</p>
              </div>
              <button class="danger-icon" title="删除厂商" @click="store.deleteProvider(selectedProvider)">⌫</button>
            </div>

            <template v-if="selectedProvider.preset_id">
              <label class="field-label">Base URL</label>
              <div class="readonly-field">{{ selectedProvider.base_url }}</div>
              <label class="field-label">API 格式</label>
              <div class="readonly-field">{{ protocolLabel(selectedProvider.protocol || 'openai') }}</div>
            </template>
            <template v-else>
              <label class="field-label">名称</label>
              <input v-model="providerForm.name" class="text-field" />
              <label class="field-label">Base URL</label>
              <input v-model="providerForm.base_url" class="text-field mono" />
              <label class="field-label">API 格式</label>
              <select v-model="providerForm.protocol" class="text-field">
                <option v-for="protocol in store.modelProtocols" :key="protocol.id" :value="protocol.id">
                  {{ protocol.label }}
                </option>
              </select>
            </template>

            <label class="field-label">API Key</label>
            <div class="key-field">
              <input
                v-model="providerForm.api_key"
                :type="showProviderKey ? 'text' : 'password'"
                :placeholder="selectedProvider.has_api_key ? '已配置；留空表示不修改' : '输入 API Key'"
              />
              <button @click="showProviderKey = !showProviderKey">{{ showProviderKey ? '隐藏' : '显示' }}</button>
            </div>
            <button class="secondary-button" :disabled="saving" @click="saveProvider">保存厂商配置</button>

            <div class="model-list-header">
              <div>
                <h3>模型列表</h3>
                <span>{{ selectedProvider.models?.length || 0 }} 个模型</span>
              </div>
              <button data-test="add-model" class="secondary-button compact" @click="openModelModal()">＋ 添加模型</button>
            </div>
            <div v-if="!selectedProvider.models?.length" class="empty-models">该厂商暂无模型</div>
            <div v-else class="configured-models">
              <div v-for="model in selectedProvider.models" :key="model.id" class="configured-model-row">
                <div class="model-main">
                  <span class="mono model-id">{{ model.name }}</span>
                  <span>{{ formatContext(model.context_window) }}</span>
                  <span :class="['vision-tag', { on: model.supports_vision }]">
                    {{ model.supports_vision ? '多模态' : '纯文本' }}
                  </span>
                </div>
                <span class="test-result">{{ testResults[model.id] || '' }}</span>
                <div class="row-actions">
                  <button :disabled="testingId === model.id" @click="testModel(model)">测试</button>
                  <button @click="openModelModal(model)">编辑</button>
                  <button class="danger" @click="store.deleteModel(model)">删除</button>
                </div>
              </div>
            </div>

          </template>

          <template v-else-if="selectedKind === 'secondary'">
            <div class="detail-title-row">
              <div>
                <h2>副模型</h2>
                <p>用于识图 / 记忆压缩 / 子 Agent 等后台任务，独立于主模型。</p>
              </div>
            </div>
            <label class="field-label">副模型</label>
            <select v-model="secondaryModelId" class="text-field" data-test="secondary-model" @change="onSecondaryModelChange">
              <option value="">无（不启用副模型）</option>
              <option v-for="m in store.models" :key="m.id" :value="m.id">
                {{ m.name }}（{{ m.provider_name }}）
              </option>
            </select>
            <div class="role-row">
              <label class="role-choice">
                <input v-model="secondaryRoles.vision" type="checkbox" data-test="secondary-role-vision" @change="onSecondaryRolesChange" />
                <span><b>识图</b><small>用于需要理解图片的任务</small></span>
              </label>
              <label class="role-choice">
                <input v-model="secondaryRoles.compression" type="checkbox" data-test="secondary-role-compression" @change="onSecondaryRolesChange" />
                <span><b>压缩</b><small>用于对话记忆压缩</small></span>
              </label>
              <label class="role-choice">
                <input v-model="secondaryRoles.subagent" type="checkbox" data-test="secondary-role-subagent" @change="onSecondaryRolesChange" />
                <span><b>子 Agent</b><small>用于子 Agent 调度</small></span>
              </label>
            </div>
          </template>

          <template v-else-if="selectedKind === 'custom'">
            <div class="detail-title-row">
              <div><h2>添加自定义厂商</h2><p>配置 API endpoint、密钥和真实请求格式。</p></div>
            </div>
            <label class="field-label">名称</label>
            <input v-model="customForm.name" data-test="custom-name" class="text-field" placeholder="如：团队网关" />
            <label class="field-label">Base URL</label>
            <input v-model="customForm.base_url" class="text-field mono" placeholder="https://api.example.com/v1" />
            <label class="field-label">API Key</label>
            <input v-model="customForm.api_key" type="password" class="text-field" placeholder="输入 API Key" />
            <label class="field-label">API 格式</label>
            <select v-model="customForm.protocol" class="text-field">
              <option v-for="protocol in store.modelProtocols" :key="protocol.id" :value="protocol.id">
                {{ protocol.label }}
              </option>
            </select>
            <button class="primary-button" :disabled="saving" @click="saveCustomProvider">添加厂商</button>
          </template>

          <div v-else class="empty-detail">从左侧选择一个厂商开始配置</div>
        </div>
      </section>
      </template>
      <TokenUsageSettings v-else />
    </main>

    <div v-if="modelModal.show" class="modal-mask">
      <form data-test="model-modal" class="model-modal" @submit.prevent="saveModel">
        <div class="modal-head"><h3>{{ modelModal.model_id ? '编辑模型' : '添加模型' }}</h3><button type="button" @click="closeModelModal">×</button></div>
        <label class="field-label">模型 ID</label>
        <input v-model="modelModal.name" class="text-field mono" placeholder="模型 ID" />
        <label class="field-label">上下文窗口</label>
        <input v-model.number="modelModal.context_window" class="text-field" type="number" min="1" />
        <label class="vision-choice">
          <input v-model="modelModal.supports_vision" data-test="model-supports-vision" type="checkbox" />
          <span><b>是否支持多模态</b><small>启用后允许向该模型发送图片内容</small></span>
        </label>
        <div class="modal-actions">
          <button type="button" class="secondary-button" @click="closeModelModal">取消</button>
          <button type="submit" class="primary-button" :disabled="saving">保存</button>
        </div>
      </form>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue';
import { useAppStore } from '../stores/appStore';
import type { ModelInfo } from '../types/pywebview';
import TokenUsageSettings from './TokenUsageSettings.vue';

const store = useAppStore();
const activeSection = ref<'models' | 'tokens'>('models');
const selectedKind = ref<'preset' | 'provider' | 'custom' | 'secondary'>('provider');
const selectedId = ref(store.providers[0]?.id || '');
const showAddProviders = ref(false);
const presetApiKey = ref('');
const showPresetKey = ref(false);
const showProviderKey = ref(false);
const saving = ref(false);
const testingId = ref('');
const testResults = reactive<Record<string, string>>({});

const providerForm = reactive({ name: '', base_url: '', api_key: '', protocol: 'openai' });
const customForm = reactive({ name: '', base_url: '', api_key: '', protocol: 'openai' });

// M33 修复：副模型状态不再在 setup 时一次性快照（会与 store 脱节，
// 后端/其他入口变更后设置页显示陈旧值）。改为 computed 双向绑定，
// 读始终取 store 当前值，写直接同步回 store（与 setSecondaryRoles 乐观更新语义一致）。
const secondaryModelId = computed({
  get: () => store.secondaryModelId,
  set: (v: string) => { store.secondaryModelId = v; },
});
const secondaryRoles = computed({
  get: () => store.secondaryModelRoles,
  set: (v: { vision: boolean; compression: boolean; subagent: boolean }) => { store.secondaryModelRoles = v; },
});
const modelModal = reactive({
  show: false,
  model_id: '',
  name: '',
  context_window: 128000,
  supports_vision: false,
});

const selectedPreset = computed(() => selectedKind.value === 'preset'
  ? store.modelPresets.find(item => item.id === selectedId.value)
  : undefined);
const selectedProvider = computed(() => selectedKind.value === 'provider'
  ? store.providers.find(item => item.id === selectedId.value)
  : undefined);
const configuredPresetIds = computed(() => new Set(
  store.providers.map(item => item.preset_id).filter(Boolean) as string[],
));

watch(selectedProvider, (provider) => {
  if (!provider) return;
  providerForm.name = provider.name;
  providerForm.base_url = provider.base_url || '';
  providerForm.api_key = '';
  providerForm.protocol = provider.protocol || 'openai';
}, { immediate: true });

watch(
  () => [store.modelPresets.map(item => item.id), store.providers.map(item => item.id)],
  () => {
    if (selectedKind.value === 'custom' || selectedKind.value === 'secondary') return;
    const presetExists = selectedKind.value === 'preset'
      && store.modelPresets.some(item => item.id === selectedId.value);
    const providerExists = selectedKind.value === 'provider'
      && store.providers.some(item => item.id === selectedId.value);
    if (presetExists || providerExists) return;
    if (store.providers.length) {
      selectProvider(store.providers[0].id);
    } else {
      selectedKind.value = 'provider';
      selectedId.value = '';
    }
  },
  { immediate: true },
);

function selectPreset(id: string) {
  selectedKind.value = 'preset';
  selectedId.value = id;
  presetApiKey.value = '';
}

function selectProvider(id: string) {
  selectedKind.value = 'provider';
  selectedId.value = id;
  showAddProviders.value = false;
}

// 点击左侧"副模型"入口：右侧切换到副模型设置面板
function selectSecondary() {
  selectedKind.value = 'secondary';
  selectedId.value = '';
  showAddProviders.value = false;
}

function startCustomProvider() {
  selectedKind.value = 'custom';
  selectedId.value = '';
}

function protocolLabel(id: string) {
  return store.modelProtocols.find(item => item.id === id)?.label || id;
}

function formatContext(value: number) {
  if (value >= 1000000) return `${Math.round(value / 100000) / 10}M`;
  return `${Math.round(value / 1000)}K`;
}

async function savePreset() {
  if (!selectedPreset.value || !presetApiKey.value.trim()) {
    store.showToast('请填写 API Key', 'warn');
    return;
  }
  saving.value = true;
  const existing = store.providers.find(item => item.preset_id === selectedPreset.value?.id);
  const result = existing
    ? await store.updateProvider(
        existing.id,
        selectedPreset.value.name,
        selectedPreset.value.base_url,
        presetApiKey.value.trim(),
        selectedPreset.value.protocol,
      )
    : await store.addPresetProvider(selectedPreset.value.id, presetApiKey.value.trim());
  saving.value = false;
  if (result?.ok) {
    presetApiKey.value = '';
    selectProvider(existing?.id || result.provider_id);
  }
}

async function saveCustomProvider() {
  if (!customForm.name.trim() || !customForm.base_url.trim()) {
    store.showToast('请填写名称和 Base URL', 'warn');
    return;
  }
  saving.value = true;
  const result = await store.addProvider(
    customForm.name.trim(), customForm.base_url.trim(), customForm.api_key.trim(), customForm.protocol,
  );
  saving.value = false;
  if (result?.ok) selectProvider(result.provider_id);
}

async function saveProvider() {
  const provider = selectedProvider.value;
  if (!provider) return;
  saving.value = true;
  await store.updateProvider(
    provider.id,
    provider.preset_id ? provider.name : providerForm.name.trim(),
    provider.preset_id ? (provider.base_url || '') : providerForm.base_url.trim(),
    providerForm.api_key.trim(),
    provider.preset_id ? (provider.protocol || 'openai') : providerForm.protocol,
  );
  providerForm.api_key = '';
  saving.value = false;
}

function openModelModal(model?: ModelInfo) {
  modelModal.show = true;
  modelModal.model_id = model?.id || '';
  modelModal.name = model?.name || '';
  modelModal.context_window = model?.context_window || 128000;
  modelModal.supports_vision = model?.supports_vision || false;
}

function closeModelModal() {
  modelModal.show = false;
}

async function saveModel() {
  const provider = selectedProvider.value;
  if (!provider || !modelModal.name.trim() || modelModal.context_window < 1) {
    store.showToast('请填写有效的模型 ID 和上下文窗口', 'warn');
    return;
  }
  saving.value = true;
  let result;
  if (modelModal.model_id) {
    result = await store.updateModel(
      provider.id, modelModal.model_id, modelModal.name.trim(),
      Number(modelModal.context_window), modelModal.supports_vision,
    );
  } else {
    result = await store.addModel(
      provider.id, modelModal.name.trim(), Number(modelModal.context_window), modelModal.supports_vision,
    );
  }
  saving.value = false;
  if (result?.ok) closeModelModal();
}

async function testModel(model: ModelInfo) {
  testingId.value = model.id;
  testResults[model.id] = '测试中…';
  const result = await store.testModel(model.id);
  testResults[model.id] = result.ok ? '✓ 连接成功' : `✕ ${result.msg}`;
  testingId.value = '';
}

async function onSecondaryModelChange() {
  await store.setSecondaryModel(secondaryModelId.value);
}

async function onSecondaryRolesChange() {
  await store.setSecondaryRoles({ ...secondaryRoles.value });
}
</script>

<style scoped>
.settings-page { position: relative; flex: 1; min-width: 0; z-index: 80; display: flex; background: var(--bg0); color: var(--text); }
.settings-nav { width: 220px; padding: 22px 12px 14px; background: var(--bg1); border-right: 1px solid var(--border); display: flex; flex-direction: column; }
.back-workspace, .nav-item { width: 100%; border: 0; background: transparent; color: var(--text-dim); text-align: left; border-radius: 9px; padding: 10px 12px; }
.back-workspace:hover, .nav-item:hover { color: var(--text); background: var(--bg2); }
.nav-section { margin: 28px 12px 8px; color: var(--text-faint); font-size: 11px; }
.nav-item { display: flex; gap: 10px; align-items: center; font-size: 13px; }
.nav-item.active { background: var(--bg3); color: var(--text); }
.nav-spacer { flex: 1; }
.settings-user { display: flex; align-items: center; gap: 10px; padding: 10px; color: var(--text-dim); font-size: 12px; }
.settings-user span:nth-child(2) { flex: 1; font-weight: 600; }
.avatar { width: 30px; height: 30px; display: grid; place-items: center; border-radius: 50%; background: var(--bg3); color: var(--text); }
.settings-main { flex: 1; min-width: 0; overflow: auto; padding: 46px 5vw; }
.settings-header { max-width: 1120px; margin: 0 auto 26px; display: flex; align-items: flex-end; justify-content: space-between; }
.settings-header h1 { margin: 0 0 10px; font-size: 32px; letter-spacing: -1px; }
.settings-header p, .detail-title-row p { margin: 0; color: var(--text-faint); font-size: 13px; }
.icon-button { background: transparent; color: var(--text-dim); border: 0; font-size: 20px; }
.model-settings-card { max-width: 1120px; min-height: 610px; margin: 0 auto; display: grid; grid-template-columns: 280px minmax(0, 1fr); border: 1px solid var(--border); border-radius: 16px; background: var(--bg1); overflow: hidden; }
.provider-list { padding: 16px 12px; border-right: 1px solid var(--border); background: var(--bg2); overflow-y: auto; }
.list-label { padding: 4px 10px 10px; color: var(--text-faint); font-size: 11px; font-weight: 600; }
.provider-empty { padding: 12px 10px; color: var(--text-faint); font-size: 11px; }
.provider-item { width: 100%; display: flex; align-items: center; gap: 9px; padding: 9px 10px; border: 1px solid transparent; border-radius: 9px; background: transparent; color: var(--text-dim); text-align: left; }
.provider-item:hover, .provider-item.active { background: var(--bg3); color: var(--text); }
.provider-item.active { border-color: var(--border); }
.provider-logo { width: 24px; height: 24px; display: grid; place-items: center; border-radius: 7px; background: var(--accent-soft); color: var(--accent); font-size: 11px; }
.provider-logo.cube { background: transparent; color: var(--text-dim); font-size: 18px; }
.provider-name { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 13px; }
.provider-state { width: 7px; height: 7px; border-radius: 50%; background: var(--text-faint); }
.provider-state.ready { background: var(--green); }
.add-provider-row { width: 100%; margin-top: 10px; padding: 9px; border: 1px dashed var(--border); border-radius: 9px; background: transparent; color: var(--text-dim); text-align: left; display: flex; align-items: center; justify-content: space-between; }
.add-provider-row:hover { border-color: var(--accent); color: var(--accent); }
.add-provider-row.active { border-color: var(--accent); color: var(--accent); background: var(--bg3); }
.add-provider-choices { margin-top: 10px; padding-top: 10px; border-top: 1px solid var(--border); }
.custom-provider-row { margin-top: 10px; }
/* 副模型入口：位于左侧列表底部（添加厂商下方），点击后在右侧设置副模型 */
.secondary-entry-row {
  width: 100%;
  margin-top: 10px;
  padding: 9px 10px;
  border: 1px solid var(--border);
  border-radius: 9px;
  background: transparent;
  color: var(--text-dim);
  text-align: left;
  display: flex;
  align-items: center;
  gap: 9px;
}
.secondary-entry-row:hover, .secondary-entry-row.active {
  border-color: var(--accent);
  color: var(--accent);
  background: var(--bg3);
}
.secondary-entry-row .provider-logo { background: var(--bg3); color: var(--text-dim); }
.secondary-entry-row.active .provider-logo { color: var(--accent); }
.secondary-entry-row .provider-name { font-size: 13px; }
.provider-detail { min-width: 0; padding: 38px; overflow-y: auto; }
.detail-title-row { display: flex; justify-content: space-between; gap: 18px; margin-bottom: 28px; }
.detail-title-row h2 { margin: 0 0 8px; font-size: 20px; }
.protocol-badge, .vision-tag { align-self: flex-start; padding: 4px 9px; border: 1px solid var(--border); border-radius: 8px; color: var(--text-faint); font-size: 11px; }
.field-label { display: block; margin: 16px 0 7px; color: var(--text-dim); font-size: 12px; }
.text-field, .readonly-field, .key-field { width: 100%; min-height: 42px; box-sizing: border-box; border: 1px solid var(--border); border-radius: 9px; background: var(--bg2); color: var(--text); font-size: 13px; }
.text-field { padding: 0 13px; }
.readonly-field { display: flex; align-items: center; padding: 0 13px; color: var(--text-dim); }
.key-field { display: flex; align-items: center; overflow: hidden; }
.key-field input { flex: 1; min-width: 0; height: 40px; padding: 0 13px; border: 0; background: transparent; color: var(--text); outline: 0; }
.key-field button, .row-actions button { border: 0; background: transparent; color: var(--text-faint); }
.key-field button { padding: 0 13px; }
.mono { font-family: var(--mono); }
.model-preview-title { margin: 24px 0 9px; color: var(--text-dim); font-size: 12px; }
.preset-models, .configured-models { border: 1px solid var(--border); border-radius: 10px; overflow: hidden; }
.preset-model-row, .configured-model-row { min-height: 48px; display: flex; align-items: center; gap: 14px; padding: 0 14px; border-bottom: 1px solid var(--border); font-size: 12px; }
.preset-model-row:last-child, .configured-model-row:last-child { border-bottom: 0; }
.preset-model-row .mono { flex: 1; color: var(--text); }
.vision-tag { padding: 2px 7px; }
.vision-tag.on { color: var(--green); border-color: color-mix(in srgb, var(--green) 35%, var(--border)); }
.primary-button, .secondary-button { min-height: 38px; margin-top: 20px; padding: 0 16px; border-radius: 9px; border: 1px solid var(--border); font-weight: 600; }
.primary-button { background: var(--text); color: var(--bg0); }
.secondary-button { background: var(--bg3); color: var(--text); }
.secondary-button.compact { margin: 0; min-height: 34px; }
.danger-icon { border: 0; background: transparent; color: var(--text-faint); font-size: 18px; }
.danger-icon:hover, .row-actions .danger:hover { color: var(--red); }
.model-list-header { display: flex; align-items: center; justify-content: space-between; margin: 34px 0 10px; }
.model-list-header h3 { margin: 0 0 4px; font-size: 14px; }
.model-list-header span { color: var(--text-faint); font-size: 11px; }
.model-main { flex: 1; min-width: 0; display: flex; align-items: center; gap: 12px; }
.model-id { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; }
.test-result { color: var(--text-faint); font-size: 11px; }
.row-actions { display: flex; gap: 7px; }
.row-actions button:hover { color: var(--accent); }
.empty-models, .empty-detail { padding: 48px 0; text-align: center; color: var(--text-faint); font-size: 12px; }
.modal-mask { position: fixed; inset: 0; z-index: 5000; display: grid; place-items: center; background: rgba(0, 0, 0, .68); backdrop-filter: blur(8px); }
.model-modal { width: min(560px, calc(100vw - 40px)); padding: 24px; box-sizing: border-box; border: 1px solid var(--border); border-radius: 15px; background: var(--bg2); box-shadow: 0 24px 80px rgba(0, 0, 0, .55); }
.modal-head { display: flex; align-items: center; justify-content: space-between; }
.modal-head h3 { margin: 0; }
.modal-head button { border: 0; background: transparent; color: var(--text-dim); font-size: 20px; }
.vision-choice { margin-top: 20px; display: flex; align-items: flex-start; gap: 10px; padding: 13px; border: 1px solid var(--border); border-radius: 10px; color: var(--text); }
.vision-choice span { display: flex; flex-direction: column; gap: 4px; font-size: 12px; }
.vision-choice small { color: var(--text-faint); }
.role-row { margin-top: 14px; display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 10px; }
.role-choice { display: flex; align-items: flex-start; gap: 9px; padding: 11px; border: 1px solid var(--border); border-radius: 10px; background: var(--bg1); color: var(--text); }
.role-choice span { display: flex; flex-direction: column; gap: 3px; font-size: 12px; }
.role-choice small { color: var(--text-faint); }
.modal-actions { display: flex; justify-content: flex-end; gap: 10px; }
@media (max-width: 900px) {
  .settings-nav { width: 170px; }
  .settings-main { padding: 28px 20px; }
  .model-settings-card { grid-template-columns: 220px minmax(0, 1fr); }
  .provider-detail { padding: 24px; }
}
</style>

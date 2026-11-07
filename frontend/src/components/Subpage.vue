<template>
  <div id="subpage" :class="{ show: visible }">
    <div class="sub-head">
      <button class="sub-back" @click="emit('close')"><AppIcon name="arrow-left" :size="13" /> 返回</button>
      <h2>{{ title }}</h2>
      <span class="sub-desc">{{ desc }}</span>
    </div>

    <div class="sub-body">
      <!-- 技能页 -->
      <div v-if="subType === 'skills'">
        <!-- 会话级稳定段落快照：技能的启停会改变 system 头，而进行中的会话已在首请求
             定型目录字节以保住前缀缓存；因此本页的改动只对新建会话生效。 -->
        <p class="sub-note" data-test="session-snapshot-note">
          已打开的会话将在新会话中生效：进行中的会话沿用其启动时的目录快照，以保证前缀缓存稳定。
        </p>
        <div class="sub-toolbar">
          <div class="src-filter" data-test="source-filter">
            <button class="seg-btn" :class="{ active: sourceFilter === 'all' }" data-test="src-filter-all" @click="sourceFilter = 'all'">全部</button>
            <button class="seg-btn" :class="{ active: sourceFilter === 'builtin' }" data-test="src-filter-builtin" @click="sourceFilter = 'builtin'">内置</button>
            <button class="seg-btn" :class="{ active: sourceFilter === 'user' }" data-test="src-filter-user" @click="sourceFilter = 'user'">用户</button>
          </div>
          <span class="toolbar-spacer"></span>
          <input
            v-model="searchQuery"
            class="sub-search"
            data-test="sub-search-input"
            placeholder="搜索技能…"
            title="按名称 / ID / 介绍搜索，多个关键词用空格分隔"
          />
          <button
            class="chip-btn"
            data-test="refresh-skills-btn"
            title="重新扫描所有技能"
            :disabled="refreshingSkills"
            @click="onRefreshSkills"
          >
            <AppIcon name="refresh" :size="12" :class="{ 'icon-spin': refreshingSkills }" />
            {{ refreshingSkills ? '刷新中…' : '刷新技能' }}
          </button>
        </div>
        <div class="sub-grid">
          <!-- 首屏技能改为后端后台扫描 + skills_loaded 事件推送：空列表可能是
               "还没扫完"而非"真没有技能"，据此区分文案，避免误导用户 -->
          <div v-if="store.skills.length === 0" class="sub-empty">
            {{ store.skillsLoading ? '技能加载中…' : '暂无可用技能' }}
          </div>
          <div v-else-if="filteredSkills.length === 0" class="sub-empty">没有符合筛选条件的技能</div>
          <div v-for="s in filteredSkills" :key="s.id" class="sub-card">
            <div class="sc-top">
              <div class="sc-ico"><AppIcon name="gem" :size="16" /></div>
              <div class="sc-name">
                {{ s.name || s.id }}
                <span class="pl-src" :class="s.source === 'builtin' ? 'builtin' : 'user'">{{ s.source === 'builtin' ? '内置' : '用户' }}</span>
              </div>
              <label class="switch" title="启用/禁用该技能" @click.stop>
                <input type="checkbox" :checked="s.enabled" @change="onToggleSkill(s, ($event.target as HTMLInputElement).checked)" />
                <span class="switch-track"></span>
              </label>
            </div>
            <div class="sc-desc">{{ s.description || '项目/用户自定义扩展技能' }}</div>
            <div class="sc-id">
              {{ s.short_id || s.id }}
              <!-- 已偏离内置版本（曾由内置释放但当前 source 非 builtin）才提供恢复入口：
                   内置技能本身无需恢复，被用户改过或老索引缺失导致误判为用户的才需要 -->
              <button
                v-if="s.from_builtin === true && s.source !== 'builtin'"
                class="btn primary sc-restore-btn"
                data-test="restore-skill-btn"
                title="用内置源覆盖当前技能文件中的用户改动"
                @click.stop="onRestoreSkill(s)"
              >恢复内置版本</button>
            </div>
          </div>
        </div>
      </div>

      <!-- MCP 页 -->
      <div v-else-if="subType === 'mcp'">
         <div class="sub-toolbar">
           <span class="toolbar-spacer"></span>
           <input
             v-model="searchQuery"
             class="sub-search"
             data-test="sub-search-input"
             placeholder="搜索服务名…"
             title="按 MCP 服务名搜索"
           />
           <button class="chip-btn" title="添加自定义 MCP 服务" @click="addForm.show = !addForm.show"><AppIcon name="plus" :size="12" /> 添加服务</button>
           <button class="chip-btn" title="重新连接所有 MCP 服务" @click="store.reconnectMcp()"><AppIcon name="refresh" :size="12" /> 重连</button>
         </div>
         <div v-if="addForm.show" class="mcp-add-form">
           <div class="maf-row">
             <input v-model="addForm.name" placeholder="服务名（如 filesystem）" style="width:180px" />
             <input v-model="addForm.command" placeholder="启动命令：npx / node / python" style="width:200px" />
             <input v-model="addForm.args" placeholder="参数，空格分隔（如 -y @modelcontextprotocol/server-filesystem）" style="flex:1;min-width:220px" />
           </div>
           <div class="maf-hint">启动命令仅支持 npx / node / python 裸名，不能填路径；其他 CLI 类服务可用 node 启动它的入口脚本。</div>
           <textarea v-model="addForm.env" class="maf-env" rows="3" placeholder="环境变量（可选），每行一个 KEY=VALUE，如：&#10;API_KEY=sk-xxx"></textarea>
           <div class="maf-row maf-actions">
             <label class="maf-check"><input type="checkbox" v-model="addForm.enabled" /> 添加后立即启用</label>
             <span style="flex:1"></span>
             <button class="chip-btn" @click="addForm.show = false">取消</button>
             <button class="chip-btn maf-confirm" @click="submitAddMcp">添加</button>
           </div>
         </div>        <div class="sub-grid">
          <div v-if="store.mcps.length === 0" class="sub-empty">暂无已配置的 MCP 服务</div>
          <div v-else-if="filteredMcps.length === 0" class="sub-empty">没有匹配的 MCP 服务</div>
          <div v-for="m in filteredMcps" :key="m.name" class="sub-card mcp-card">
            <div class="sc-top">
              <div class="sc-ico">⧉</div>
              <div class="sc-name">
                {{ m.name }}
              </div>
              <span class="status-dot" :class="mcpStatusClass(m)" :title="mcpStatusText(m)"></span>
              <label class="switch" title="启用/禁用该 MCP 服务" @click.stop>
                <input type="checkbox" :checked="m.enabled" @change="onToggleMcp(m, ($event.target as HTMLInputElement).checked)" />
                <span class="switch-track"></span>
              </label>
            </div>
            <div class="sc-desc">
              {{ '工具数: ' + (m.tools_count ?? 0) + ' · ' + mcpStatusText(m) }}<span
                v-if="mcpErrorText(m)"
                class="mcp-err"
                :title="mcpErrorFull(m)"
              > · 失败原因: {{ mcpErrorText(m) }}</span>
            </div>
            <div class="sc-id">
              <button class="chip-btn danger" title="删除该 MCP 服务" @click.stop="onRemoveMcp(m)">删除</button>
            </div>
          </div>
        </div>
      </div>

      <!-- 插件页 -->
      <div v-else-if="subType === 'plugins'">
        <div class="sub-toolbar">
          <div class="src-filter" data-test="source-filter">
            <button class="seg-btn" :class="{ active: sourceFilter === 'all' }" data-test="src-filter-all" @click="sourceFilter = 'all'">全部</button>
            <button class="seg-btn" :class="{ active: sourceFilter === 'builtin' }" data-test="src-filter-builtin" @click="sourceFilter = 'builtin'">内置</button>
            <button class="seg-btn" :class="{ active: sourceFilter === 'user' }" data-test="src-filter-user" @click="sourceFilter = 'user'">用户</button>
          </div>
          <span class="toolbar-spacer"></span>
          <input
            v-model="searchQuery"
            class="sub-search"
            data-test="sub-search-input"
            placeholder="搜索插件…"
            title="按名称 / 介绍 / 工具名搜索，多个关键词用空格分隔"
          />
        </div>
        <div class="sub-grid">
          <div v-if="store.plugins.length === 0" class="sub-empty">暂无已安装的插件</div>
          <div v-else-if="filteredPlugins.length === 0" class="sub-empty">没有符合筛选条件的插件</div>
          <div v-for="p in filteredPlugins" :key="p.name" class="sub-card">
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
              <button v-if="p.status === 'untrusted'" class="btn primary sc-trust-btn" title="确认插件内容后加载" @click.stop="store.trustPlugin(p.name)">信任并加载</button>
            </div>
          </div>
        </div>
      </div>

      <!-- 星轨：自动化任务 + 工作流 双标签页（参考 ZCode 的 Automations 页签形态） -->
      <div v-else-if="subType === 'automations' || subType === 'workflows'" class="auto-page">
        <div class="auto-tabs" role="tablist" aria-label="星轨页签" data-test="auto-tabs">
          <button
            class="seg-btn"
            :class="{ active: subType === 'automations' }"
            role="tab"
            :aria-selected="subType === 'automations'"
            data-test="auto-tab-automations"
            @click="switchAutoTab('automations')"
          >星轨</button>
          <button
            class="seg-btn"
            :class="{ active: subType === 'workflows' }"
            role="tab"
            :aria-selected="subType === 'workflows'"
            data-test="auto-tab-workflows"
            @click="switchAutoTab('workflows')"
          >星流</button>
        </div>
        <AutomationPane v-if="subType === 'automations'" />
        <WorkflowPane v-else />
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
          <select v-model="newModel.provider_id" title="选择供应商" style="min-width:140px" @change="subpageRemoteModels = null">
            <option value="" disabled>选择供应商</option>
            <option v-for="p in store.providerOptions" :key="p.id" :value="p.id">{{ p.name }}</option>
          </select>
          <input v-model="newModel.model_name" placeholder="模型名称" />
          <input v-model.number="newModel.context_window" type="number" min="0" placeholder="上下文窗口" style="width:120px" />
          <button class="btn" :disabled="subpageFetchingModels" title="从厂商拉取可用模型列表" @click="fetchSubpageModels">
            <span v-if="subpageFetchingModels" class="mini-spin"></span>获取模型列表
          </button>
          <button class="btn primary" :disabled="addingModel" title="添加模型" @click="submitAddModel">
            <span v-if="addingModel" class="mini-spin"></span>添加模型
          </button>
        </div>
        <div v-if="subpageRemoteModels && subpageRemoteModels.length" class="model-form">
          <select style="flex:1" data-test="subpage-remote-model-select" @change="pickSubpageModel">
            <option value="">共 {{ subpageRemoteModels.length }} 个模型，点选自动填充（上下文/最大输出/多模态）</option>
            <option v-for="entry in subpageRemoteModels" :key="entry.id" :value="entry.id">
              {{ entry.id }}{{ entry.model_type ? `（${entry.model_type}）` : '' }}
            </option>
          </select>
        </div>
        <div class="model-form">
          <input v-model.number="newModel.max_tokens" type="number" min="0" placeholder="最大输出（留空默认）" style="width:150px" />
        </div>

        <div class="model-section-title">已配置模型</div>
        <div v-if="store.models.length === 0" class="sub-empty">暂无模型，请先在上方添加</div>
        <div v-else class="model-groups">
          <div v-for="(list, provider) in store.modelsByProvider" :key="provider" class="model-group">
            <div class="model-group-title">{{ provider }}</div>
            <div v-for="m in list" :key="m.id" class="model-row">
              <span class="m-name">{{ m.name }}</span>
              <span v-if="m.id === store.activeModelId" class="m-active"><i class="m-active-dot"></i>使用中</span>
              <span
                v-if="testResults[m.id]"
                class="m-test-result"
                :class="testResults[m.id].status"
                :title="testResults[m.id].msg"
              >
                <AppIcon
                  v-if="testResults[m.id].status === 'ok'"
                  name="check"
                  :size="11"
                  :stroke-width="2.25"
                />
                <AppIcon
                  v-else-if="testResults[m.id].status === 'fail'"
                  name="x"
                  :size="11"
                  :stroke-width="2.25"
                />
                <AppIcon
                  v-else-if="testResults[m.id].status === 'testing'"
                  name="loader"
                  :size="11"
                  class="icon-spin"
                />
                {{ testResults[m.id].msg }}
              </span>
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

  </div>
</template>

<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue';
import AppIcon from './AppIcon.vue';
import AutomationPane from './AutomationPane.vue';
import WorkflowPane from './WorkflowPane.vue';
import { useAppStore, type SubpageType } from '../stores/appStore';
import type { SkillInfo, McpInfo, ModelInfo, PluginInfo } from '../types/pywebview';
import { parseModelTestError } from '../utils/errorParser';

const props = defineProps<{
  visible: boolean;
  subType: SubpageType;
}>();

const emit = defineEmits(['close']);
const store = useAppStore();

const title = computed(() => {
  if (props.subType === 'skills') return '星盘';
  if (props.subType === 'mcp') return '星桥';
  if (props.subType === 'plugins') return '星匣';
  if (props.subType === 'automations' || props.subType === 'workflows') return '星轨';
  return '模型管理';
});
const desc = computed(() => {
  if (props.subType === 'skills') return '已扫描的项目技能与用户扩展技能';
  if (props.subType === 'mcp') return '来自 mcp.json 的服务接口';
  if (props.subType === 'plugins') return '与 CLI 共用同一份 plugin.json 配置，开关状态两端同步';
  if (props.subType === 'automations' || props.subType === 'workflows') return '定时触发的星轨自动化回合；工作流保存在项目里，填好参数就能再跑一次';
  return '管理模型供应商与可用模型列表';
});

const refreshingSkills = ref(false);

// 来源筛选：全部 / 内置 / 用户，口径与卡片角标一致——
// 技能缺 source（旧版后端）按用户算，插件非 'user' 按内置算
type SourceFilterValue = 'all' | 'builtin' | 'user';
const sourceFilter = ref<SourceFilterValue>('all');

// 关键词搜索：技能 / MCP / 插件三页共用同一个输入框，切页时与来源筛选一起重置。
// MCP 后端只返回服务名，故该页搜索范围仅服务名。
const searchQuery = ref('');
watch(() => props.subType, () => { sourceFilter.value = 'all'; searchQuery.value = ''; });

// 关键词按空格拆成多个词，全部命中才算匹配（忽略大小写）；只传小写后的 kw
function hitSearch(kw: string, fields: Array<string | undefined | null>): boolean {
  if (!kw) return true;
  const hay = fields.filter(Boolean).join(' ').toLowerCase();
  return kw.split(/\s+/).every(t => hay.includes(t));
}

const filteredSkills = computed(() => {
  const kw = searchQuery.value.trim().toLowerCase();
  return store.skills.filter(s => {
    if (sourceFilter.value !== 'all' && (s.source === 'builtin' ? 'builtin' : 'user') !== sourceFilter.value) return false;
    return hitSearch(kw, [s.name, s.id, s.short_id, s.description]);
  });
});
const filteredPlugins = computed(() => {
  const kw = searchQuery.value.trim().toLowerCase();
  return store.plugins.filter(p => {
    if (sourceFilter.value !== 'all' && (p.source === 'user' ? 'user' : 'builtin') !== sourceFilter.value) return false;
    return hitSearch(kw, [p.name, p.description, p.tools.join(' ')]);
  });
});
const filteredMcps = computed(() => {
  const kw = searchQuery.value.trim().toLowerCase();
  return store.mcps.filter(m => hitSearch(kw, [m.name]));
});

async function onRefreshSkills() {
  if (refreshingSkills.value) return;
  refreshingSkills.value = true;
  try {
    await store.refreshSkills();
  } finally {
    refreshingSkills.value = false;
  }
}

function onToggleSkill(s: SkillInfo, checked: boolean) {
  store.toggleSkill(s.id, checked);
}

// 恢复内置版本会用内置源覆盖用户改动（破坏性），先复用项目统一的 askConfirm
// 二次确认（与删除模型/撤销回合同一机制，底层是全局 ConfirmModal），
// 确认后再调 store action；失败提示由 store.restoreSkill 内部统一处理
async function onRestoreSkill(s: SkillInfo) {
  const ok = await store.askConfirm(
    '恢复内置版本',
    s.name || s.id,
    `将用内置源覆盖技能「${s.name || s.id}」，你在该技能文件里的改动会丢失。`,
    '恢复内置版本 ⏎',
  );
  if (ok) await store.restoreSkill(s.id);
}

function onToggleMcp(m: McpInfo, checked: boolean) {
  store.toggleMcpServer(m.name, checked);
}

// 删除 MCP 服务会改写 mcp.json，属破坏性操作，先二次确认再调 store action
async function onRemoveMcp(m: McpInfo) {
  const ok = await store.askConfirm(
    '删除 MCP 服务',
    m.name,
    '将从 mcp.json 移除该服务配置，删除后需要重新添加才能恢复。',
    '删除 ⏎',
  );
  if (ok) await store.removeMcpServer(m.name);
}

// 连接失败原因：仅展示已启用但未连接的服务的 last_error（旧后端不返回该字段，表现不变）
const MCP_ERR_MAX = 120;
function mcpErrorFull(m: McpInfo): string {
  if (!m.enabled || m.connected) return '';
  return (m.last_error || '').trim();
}
// 过长报错截断加省略号，完整内容由 title 承载，避免撑破卡片
function mcpErrorText(m: McpInfo): string {
  const err = mcpErrorFull(m);
  return err.length > MCP_ERR_MAX ? err.slice(0, MCP_ERR_MAX) + '…' : err;
}

// 添加自定义 MCP 服务的表单状态
const addForm = reactive({
  show: false,
  name: '',
  command: 'npx',
  args: '',
  env: '',
  enabled: true,
});

function submitAddMcp() {
  const name = addForm.name.trim();
  const command = addForm.command.trim();
  if (!name || !command) {
    store.notifyModal('表单校验', '请填写服务名和启动命令');
    return;
  }
  const args = addForm.args.split(/\s+/).filter(Boolean);
  // 解析每行一个的 KEY=VALUE 环境变量
  const env: Record<string, string> = {};
  for (const line of addForm.env.split('\n')) {
    const t = line.trim();
    if (!t) continue;
    const eq = t.indexOf('=');
    if (eq <= 0) {
      store.notifyModal('表单校验', '环境变量格式应为 KEY=VALUE: ' + t);
      return;
    }
    env[t.slice(0, eq).trim()] = t.slice(eq + 1).trim();
  }
  store.addMcpServer(name, command, args, Object.keys(env).length ? env : undefined, addForm.enabled);
  addForm.show = false;
  addForm.name = '';
  addForm.args = '';
  addForm.env = '';
}

function mcpStatusClass(m: McpInfo) {
  if (!m.enabled) return 'gray';
  return m.connected ? 'green' : 'yellow';
}

function mcpStatusText(m: McpInfo) {
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
    case 'untrusted': return '未信任 —— 首次安装或内容已变化，需确认后加载';
    default: return p.status || '';
  }
}


// 打开 MCP / 插件 / 自动化页时刷新一次最新状态；星轨/星流双标签共用一个
// 子页，切换或打开任一标签都同时刷新两侧数据（两侧各自幂等，开销可忽略）
watch(
  () => [props.visible, props.subType] as const,
  ([visible, type]) => {
    if (!visible) return;
    if (type === 'mcp') {
      store.refreshMcpServers();
    } else if (type === 'plugins') {
      store.refreshPlugins();
    } else if (type === 'automations' || type === 'workflows') {
      store.refreshAutomations();
      store.refreshWorkflows();
    }
  }
);

// 星轨/星流标签切换：换 subType 而不关子页（openSubpage 是幂等置值）
function switchAutoTab(tab: 'automations' | 'workflows') {
  store.openSubpage(tab);
}

// ---- 模型管理：添加供应商 ----
const newProvider = reactive({ name: '', base_url: '', api_key: '', protocol: 'openai' });
const addingProvider = ref(false);

async function submitAddProvider() {
  if (!newProvider.name.trim() || !newProvider.base_url.trim()) {
    store.notifyModal('表单校验', '请填写供应商名称与 API 地址');
    return;
  }
  addingProvider.value = true;
  try {
    // M34 修复：addProvider 失败（返回 null）时不检查返回值仍清空表单，
    // 用户填写内容丢失且误以为已添加。成功才清空。
    const res = await store.addProvider(newProvider.name.trim(), newProvider.base_url.trim(), newProvider.api_key.trim(), newProvider.protocol);
    if (res && res.ok) {
      newProvider.name = '';
      newProvider.base_url = '';
      newProvider.api_key = '';
      newProvider.protocol = 'openai';
    }
  } finally {
    addingProvider.value = false;
  }
}

// ---- 模型管理：添加模型 ----
const newModel = reactive({ provider_id: '', model_name: '', context_window: 32000, max_tokens: '' as number | '' });
const addingModel = ref(false);

// ---- 模型管理：获取模型列表一键填充 ----
const subpageRemoteModels = ref<{ id: string; model_type?: string; context_window?: number | null; supports_vision?: boolean | null; max_tokens?: number | null }[] | null>(null);
const subpageFetchingModels = ref(false);

async function fetchSubpageModels() {
  if (!newModel.provider_id) {
    store.notifyModal('获取模型列表', '请先选择供应商');
    return;
  }
  if (subpageFetchingModels.value) return;
  subpageFetchingModels.value = true;
  try {
    const res = await store.fetchProviderModels(newModel.provider_id);
    if (res?.ok) {
      subpageRemoteModels.value = res.models || [];
      if (!subpageRemoteModels.value.length) {
        store.notifyModal('获取模型列表', '服务端返回了空列表');
      }
    } else {
      store.notifyModal('获取模型列表', res?.msg || '未知错误');
    }
  } finally {
    subpageFetchingModels.value = false;
  }
}

function pickSubpageModel(event: Event) {
  const value = (event.target as HTMLSelectElement).value;
  if (!value) return;
  newModel.model_name = value;
  // 厂商元数据 + 本地规格表：能填的都填（未提供的字段保持表单原值）
  const entry = (subpageRemoteModels.value || []).find((m) => m.id === value);
  if (!entry) return;
  if (entry.context_window && entry.context_window > 0) {
    newModel.context_window = entry.context_window;
  }
  newModel.max_tokens = entry.max_tokens && entry.max_tokens > 0 ? entry.max_tokens : '';
}

async function submitAddModel() {
  if (!newModel.provider_id || !newModel.model_name.trim()) {
    store.notifyModal('表单校验', '请选择供应商并填写模型名称');
    return;
  }
  addingModel.value = true;
  try {
    // M34 修复：同上，addModel 成功才清空表单；最大输出留空 = null（走内置默认）
    const maxTokens = newModel.max_tokens === '' ? null : Number(newModel.max_tokens);
    const res = await store.addModel(newModel.provider_id, newModel.model_name.trim(), Number(newModel.context_window) || 0, false, true, '', maxTokens);
    if (res && res.ok) {
      newModel.model_name = '';
      newModel.max_tokens = '';
    }
  } finally {
    addingModel.value = false;
  }
}

// ---- 模型管理：测试连接 ----
const testingModelId = ref<string | null>(null);
const testResults = ref<Record<string, { status: 'testing' | 'ok' | 'fail'; msg: string }>>({});

async function testModelRow(m: ModelInfo) {
  testingModelId.value = m.id;
  testResults.value[m.id] = { status: 'testing', msg: '测试中…' };
  const res = await store.testModel(m.id);
  const friendly = res.ok ? null : parseModelTestError(res.msg);
  testResults.value[m.id] = {
    status: res.ok ? 'ok' : 'fail',
    msg: res.ok ? '连接成功' : (friendly?.title || res.msg),
  };
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
  flex-shrink: 0;
}
.sub-back:hover {
  border-color: var(--accent);
  color: var(--accent);
}
.sub-head h2 {
  font-size: 15px;
  white-space: nowrap;
}
.sub-head .sub-desc {
  font-size: 12px;
  color: var(--text-faint);
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.sub-note {
  max-width: 960px;
  margin: 0 auto 14px;
  font-size: 12px;
  line-height: 1.6;
  color: var(--text-faint);
}
.sub-body {
  flex: 1;
  overflow-y: auto;
  padding: 22px 26px;
  min-width: 0;
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
  align-items: center;
  justify-content: flex-end;
  gap: 10px;
}
.toolbar-spacer {
  flex: 1;
}
/* 星轨/星流双标签页签（复用技能页 .seg-btn 分段外观，置于内容区顶部） */
.auto-page {
  display: flex;
  flex-direction: column;
  gap: 14px;
  min-width: 0;
  max-width: 100%;
}
.auto-tabs {
  display: inline-flex;
  align-items: center;
  background: var(--bg2);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 2px;
  gap: 2px;
  align-self: flex-start;
}
.auto-tabs .seg-btn {
  border: none;
  background: transparent;
  color: var(--text-dim);
  font-size: 12px;
  line-height: 1;
  padding: 5px 14px;
  border-radius: 6px;
  cursor: pointer;
  transition: 0.15s;
  font-family: inherit;
}
.auto-tabs .seg-btn:hover {
  color: var(--text);
}
.auto-tabs .seg-btn.active {
  background: var(--accent-soft);
  color: var(--accent);
  font-weight: 600;
}
/* 技能 / MCP / 插件三页共用的关键词搜索框（与钩子页 .hook-search 同款外观） */
.sub-search {
  background: var(--bg2);
  border: 1px solid var(--border);
  border-radius: 7px;
  padding: 6px 10px;
  color: var(--text);
  font-size: 12px;
  width: 180px;
  outline: none;
  font-family: inherit;
  transition: 0.15s;
}
.sub-search:focus {
  border-color: var(--accent);
}
/* 来源分段筛选：全部 / 内置 / 用户 */
.src-filter {
  display: inline-flex;
  align-items: center;
  background: var(--bg2);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 2px;
  gap: 2px;
}
.src-filter .seg-btn {
  border: none;
  background: transparent;
  color: var(--text-dim);
  font-size: 12px;
  line-height: 1;
  padding: 5px 12px;
  border-radius: 6px;
  cursor: pointer;
  transition: 0.15s;
  font-family: inherit;
}
.src-filter .seg-btn:hover {
  color: var(--text);
}
.src-filter .seg-btn.active {
  background: var(--accent-soft);
  color: var(--accent);
  font-weight: 600;
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
.sub-card .sc-trust-btn {
  margin-left: 8px;
  padding: 1px 8px;
  font-size: 10px;
  vertical-align: middle;
}
/* 技能卡片「恢复内置版本」按钮：与信任按钮同量级的次级操作外观 */
.sub-card .sc-restore-btn {
  margin-left: 8px;
  padding: 1px 10px;
  font-size: 10px;
  line-height: 1.6;
  vertical-align: middle;
}

/* MCP 添加服务表单 */
.mcp-add-form {
  margin-bottom: 12px;
  padding: 12px;
  border: 1px solid var(--border);
  border-radius: 10px;
  background: var(--bg1);
}
.maf-row {
  display: flex;
  gap: 8px;
  margin-bottom: 8px;
}
.mcp-add-form input {
  background: var(--bg2);
  border: 1px solid var(--border);
  border-radius: 7px;
  padding: 7px 10px;
  color: var(--text);
  font-size: 12.5px;
  outline: none;
}
.mcp-add-form input:focus,
.maf-env:focus {
  border-color: var(--accent);
}
.maf-env {
  width: 100%;
  box-sizing: border-box;
  background: var(--bg2);
  border: 1px solid var(--border);
  border-radius: 7px;
  padding: 7px 10px;
  color: var(--text);
  font-size: 12px;
  font-family: var(--mono);
  outline: none;
  resize: vertical;
  margin-bottom: 8px;
}
.maf-hint {
  font-size: 11.5px;
  color: var(--text-dim);
  line-height: 1.5;
  margin-bottom: 8px;
}
.maf-actions {
  align-items: center;
  margin-bottom: 0;
}
.maf-check {
  font-size: 12px;
  color: var(--text-dim);
  display: flex;
  align-items: center;
  gap: 5px;
}
.maf-confirm {
  border-color: var(--accent);
  color: var(--accent);
  background: var(--accent-soft);
}
.mcp-err {
  color: var(--red);
  word-break: break-all;
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
  display: inline-flex;
  align-items: center;
  gap: 4px;
}
.model-row .m-active-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--green);
}
.model-row .m-test-result {
  font-size: 11px;
  color: var(--text-faint);
  display: inline-flex;
  align-items: center;
  gap: 4px;
  flex-shrink: 0;
  white-space: nowrap;
}
.model-row .m-test-result.ok { color: var(--green); }
.model-row .m-test-result.fail { color: var(--red); }
.model-row-actions {
  margin-left: auto;
  display: flex;
  gap: 6px;
  flex-shrink: 0;
  white-space: nowrap;
}
.model-row-actions .chip-btn {
  flex-shrink: 0;
  white-space: nowrap;
}
</style>

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
           <button class="chip-btn" title="添加自定义 MCP 服务" @click="addForm.show = !addForm.show"><AppIcon name="plus" :size="12" /> 添加服务</button>
           <button class="chip-btn" title="重新连接所有 MCP 服务" @click="store.reconnectMcp()"><AppIcon name="refresh" :size="12" /> 重连</button>
         </div>
         <div v-if="addForm.show" class="mcp-add-form">
           <div class="maf-row">
             <input v-model="addForm.name" placeholder="服务名（如 filesystem）" style="width:180px" />
             <input v-model="addForm.command" placeholder="启动命令（如 npx）" style="width:140px" />
             <input v-model="addForm.args" placeholder="参数，空格分隔（如 -y @modelcontextprotocol/server-filesystem）" style="flex:1;min-width:220px" />
           </div>
           <textarea v-model="addForm.env" class="maf-env" rows="3" placeholder="环境变量（可选），每行一个 KEY=VALUE，如：&#10;API_KEY=sk-xxx"></textarea>
           <div class="maf-row maf-actions">
             <label class="maf-check"><input type="checkbox" v-model="addForm.enabled" /> 添加后立即启用</label>
             <span style="flex:1"></span>
             <button class="chip-btn" @click="addForm.show = false">取消</button>
             <button class="chip-btn maf-confirm" @click="submitAddMcp">添加</button>
           </div>
         </div>        <div class="sub-grid">
          <div v-if="store.mcps.length === 0" class="sub-empty">暂无已配置的 MCP 服务</div>
          <div v-for="m in store.mcps" :key="m.name" class="sub-card mcp-card">
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
            <div class="sc-desc">{{ '工具数: ' + (m.tools_count ?? 0) + ' · ' + mcpStatusText(m) }}</div>
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

<!-- 钩子 Hooks 页：列表（筛选/搜索/信任/启停）+ 新建/编辑表单双视图（C13 手动配置） -->
      <div v-else-if="subType === 'hooks'">
        <!-- 表单视图：新建/编辑钩子（替换列表） -->
        <div v-if="hookForm.mode !== 'closed'" class="hook-form-page">
          <button class="hook-back" @click="closeHookForm"><AppIcon name="arrow-left" :size="12" /> 返回列表</button>
          <div class="hook-form-head">
            <h3>{{ hookForm.mode === 'edit' ? '编辑钩子' : '新建钩子' }}</h3>
            <p>在任务生命周期事件发生时自动执行命令。</p>
          </div>
          <div class="hook-form-card">
            <div class="hf-row">
              <label class="hf-field">
                <span class="hf-label">事件</span>
                <select v-model="hookForm.event" data-test="hook-form-event" title="触发该钩子的生命周期事件">
                  <option value="SessionStart">SessionStart</option>
                  <option value="PreToolUse">PreToolUse</option>
                  <option value="PostToolUse">PostToolUse</option>
                  <option value="Stop">Stop</option>
                </select>
              </label>
              <label class="hf-field">
                <span class="hf-label">作用域</span>
                <select v-if="hookForm.mode === 'create'" v-model="hookForm.scope" data-test="hook-form-scope" title="新建钩子的保存位置">
                  <option value="user">用户</option>
                  <option value="project">项目</option>
                </select>
                <!-- 编辑时作用域随原条目不迁移：只读展示 -->
                <span v-else class="hf-readonly" data-test="hook-form-scope">{{ hookForm.scope === 'user' ? '用户级' : '项目级' }}</span>
              </label>
            </div>
            <div class="hf-row">
              <label class="hf-field">
                <span class="hf-label">匹配器</span>
                <input v-model="hookForm.matcher" data-test="hook-form-matcher" placeholder="例如 write_file | edit_file（留空匹配全部）" />
                <span class="hf-hint">仅对 PreToolUse / PostToolUse 生效；留空或 * 匹配全部工具。</span>
              </label>
              <label class="hf-field">
                <span class="hf-label">命令</span>
                <input v-model="hookForm.command" class="hf-mono" data-test="hook-form-command" placeholder="例如 python ./scripts/guard.py" />
              </label>
            </div>
            <details class="hf-advanced">
              <summary>高级</summary>
              <div class="hf-row">
                <label class="hf-field">
                  <span class="hf-label">超时时间（秒）</span>
                  <input v-model.number="hookForm.timeoutSec" type="number" min="1" max="300" style="width:140px" data-test="hook-form-timeout" />
                  <span class="hf-hint">默认 60 秒，上限 300 秒；超时按失败处理，不阻断主流程。</span>
                </label>
                <label class="hf-field">
                  <span class="hf-label">状态消息</span>
                  <input v-model="hookForm.statusMessage" data-test="hook-form-status-message" placeholder="例如 正在检查工作区" />
                </label>
              </div>
            </details>
            <div class="hf-actions">
              <button v-if="hookForm.mode === 'edit'" class="hf-delete" data-test="hook-form-delete" @click="onDeleteHook">删除</button>
              <span style="flex:1"></span>
              <button class="btn primary" data-test="hook-form-save" :disabled="hookForm.saving" @click="submitHookForm">保存</button>
              <button class="btn" data-test="hook-form-cancel" @click="closeHookForm">取消</button>
            </div>
          </div>
        </div>

        <!-- 列表视图 -->
        <template v-else>
          <div class="sub-toolbar">
            <div class="src-filter">
              <button class="seg-btn" :class="{ active: hookScopeFilter === 'all' }" data-test="hook-scope-filter-all" @click="hookScopeFilter = 'all'">全部</button>
              <button class="seg-btn" :class="{ active: hookScopeFilter === 'user' }" data-test="hook-scope-filter-user" @click="hookScopeFilter = 'user'">用户</button>
              <button class="seg-btn" :class="{ active: hookScopeFilter === 'project' }" data-test="hook-scope-filter-project" @click="hookScopeFilter = 'project'">项目</button>
            </div>
            <input v-model="hookQuery" class="hook-search" data-test="hook-search-input" placeholder="搜索钩子…" />
            <span class="toolbar-spacer"></span>
            <button
              class="chip-btn"
              data-test="refresh-hooks-btn"
              title="重新读取用户级与项目级 Hook"
              :disabled="store.hooksLoading"
              @click="store.refreshHooks()"
            >
              <AppIcon name="refresh" :size="12" :class="{ 'icon-spin': store.hooksLoading }" />
              {{ store.hooksLoading ? '刷新中…' : '刷新' }}
            </button>
            <button class="chip-btn" data-test="hook-new-btn" title="新建钩子" @click="openHookCreate">
              <AppIcon name="plus" :size="12" /> 新建钩子
            </button>
          </div>
          <div class="hook-list">
            <div v-if="store.hooks.length === 0" class="hook-empty">
              <div class="he-title">尚未配置钩子</div>
              <div class="he-desc">新建钩子，以在任务生命周期事件中运行命令。（用户级 ~/.ycode/hooks.json、项目级 .ycode/hooks.json）</div>
              <button class="btn primary" data-test="hook-empty-new-btn" @click="openHookCreate">新建钩子</button>
            </div>
            <div v-else-if="filteredHooks.length === 0" class="sub-empty">没有符合筛选条件的钩子</div>
            <div
              v-for="h in filteredHooks"
              :key="h.digest"
              class="hook-row"
              :class="{ 'is-disabled': h.disabled === true }"
              data-test="hook-row"
              @click="openHookEditor(h)"
            >
              <div class="hr-ico"><AppIcon name="zap" :size="15" /></div>
              <div class="hr-main">
                <div class="hr-line">
                  <span class="hr-event">{{ h.event }}</span>
                  <span class="pl-src" :class="h.scope === 'user' ? 'user' : 'builtin'">{{ h.scope === 'user' ? '用户' : '项目' }}</span>
                  <span class="hook-status" :class="h.status">{{ hookStatusText(h.status) }}</span>
                  <span v-if="h.disabled === true" class="hook-status disabled-badge">已禁用</span>
                </div>
                <div class="hr-cmd">{{ h.command }}</div>
                <div class="hr-meta">matcher: {{ h.matcher }} · 来源: {{ h.source_path }}</div>
              </div>
              <div class="hr-actions" @click.stop>
                <!-- 待确认/配置已变更：需先信任才会执行，拒绝则显式屏蔽 -->
                <button
                  v-if="h.status === 'pending' || h.status === 'stale'"
                  class="btn primary sc-trust-btn"
                  data-test="hook-action-grant"
                  title="确认内容后允许该 Hook 命令执行"
                  @click="store.grantHookTrust(h.digest)"
                >信任</button>
                <button
                  v-if="h.status === 'pending' || h.status === 'stale'"
                  class="btn sc-trust-btn"
                  data-test="hook-action-deny"
                  title="拒绝后该 Hook 命令不再执行"
                  @click="store.denyHookTrust(h.digest)"
                >拒绝</button>
                <button
                  v-if="h.status === 'trusted'"
                  class="btn sc-trust-btn"
                  data-test="hook-action-revoke"
                  title="撤销信任，该 Hook 回到待确认"
                  @click="store.revokeHookTrust(h.digest)"
                >撤销信任</button>
                <button
                  v-if="h.status === 'blocked' || h.status === 'revoked'"
                  class="btn sc-trust-btn"
                  data-test="hook-action-reset"
                  title="重置为待确认，可重新决定是否信任"
                  @click="store.resetHookTrust(h.digest)"
                >重置为待确认</button>
                <label class="switch" :title="h.status === 'trusted' ? '启用/禁用该钩子' : '信任后可用'">
                  <input
                    type="checkbox"
                    data-test="hook-toggle-switch"
                    :checked="h.disabled !== true"
                    :disabled="h.status !== 'trusted'"
                    @change="onToggleHookDisabled(h, ($event.target as HTMLInputElement).checked)"
                  />
                  <span class="switch-track"></span>
                </label>
              </div>
            </div>
          </div>
        </template>
      </div>

      <!-- 星轨自动化任务页 -->
      <div v-else-if="subType === 'automations'" class="automations-page">
        <AutomationPane />
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
import { useAppStore, type SubpageType } from '../stores/appStore';
import type { SkillInfo, McpInfo, ModelInfo, PluginInfo, HookInfo } from '../types/pywebview';
import { parseModelTestError } from '../utils/errorParser';

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
  if (props.subType === 'automations') return '星轨自动化任务';
  if (props.subType === 'hooks') return '钩子 Hooks';
  return '模型管理';
});
const desc = computed(() => {
  if (props.subType === 'skills') return '已扫描的项目技能与用户扩展技能';
  if (props.subType === 'mcp') return '来自 mcp.json 的服务接口';
  if (props.subType === 'plugins') return '与 CLI 共用同一份 plugin.json 配置，开关状态两端同步';
  if (props.subType === 'automations') return '定时触发的星轨自动化回合，仅星瑶运行期间生效';
  if (props.subType === 'hooks') return '用户级与项目级生命周期钩子（支持手动配置，信任确认后生效）';
  return '管理模型供应商与可用模型列表';
});

const refreshingSkills = ref(false);

// 来源筛选：全部 / 内置 / 用户，口径与卡片角标一致——
// 技能缺 source（旧版后端）按用户算，插件非 'user' 按内置算
type SourceFilterValue = 'all' | 'builtin' | 'user';
const sourceFilter = ref<SourceFilterValue>('all');
watch(() => props.subType, () => { sourceFilter.value = 'all'; });

const filteredSkills = computed(() => {
  if (sourceFilter.value === 'all') return store.skills;
  return store.skills.filter(s => (s.source === 'builtin' ? 'builtin' : 'user') === sourceFilter.value);
});
const filteredPlugins = computed(() => {
  if (sourceFilter.value === 'all') return store.plugins;
  return store.plugins.filter(p => (p.source === 'user' ? 'user' : 'builtin') === sourceFilter.value);
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

// C12：Hook 信任状态文案（未知状态回退原文，避免新增状态时静默丢信息）
function hookStatusText(status: HookInfo['status']) {
  switch (status) {
    case 'pending': return '待确认';
    case 'trusted': return '已信任';
    case 'stale': return '配置已变更';
    case 'blocked': return '已拒绝';
    case 'revoked': return '已撤销';
    default: return status;
  }
}

// C13：Hook 手动配置——列表（作用域筛选/关键词搜索）+ 新建/编辑表单双视图
type HookScopeFilterValue = 'all' | 'user' | 'project';
const hookScopeFilter = ref<HookScopeFilterValue>('all');
const hookQuery = ref('');

// 关键词命中 event/command/matcher/status_message/source_path 任一字段即保留
const filteredHooks = computed(() => {
  const kw = hookQuery.value.trim().toLowerCase();
  return store.hooks.filter((h) => {
    if (hookScopeFilter.value !== 'all' && h.scope !== hookScopeFilter.value) return false;
    if (!kw) return true;
    return [h.event, h.command, h.matcher, h.status_message, h.source_path]
      .join(' ')
      .toLowerCase()
      .includes(kw);
  });
});

// 表单三种形态：closed 显示列表；create/edit 时表单替换列表
const hookForm = reactive({
  mode: 'closed' as 'closed' | 'create' | 'edit',
  editing: null as HookInfo | null,
  event: 'PreToolUse',
  scope: 'user' as 'user' | 'project',
  matcher: '',
  command: '',
  timeoutSec: 60,
  statusMessage: '',
  saving: false,
});

function openHookCreate() {
  hookForm.mode = 'create';
  hookForm.editing = null;
  hookForm.event = 'PreToolUse';
  hookForm.scope = 'user';
  hookForm.matcher = '';
  hookForm.command = '';
  hookForm.timeoutSec = 60;
  hookForm.statusMessage = '';
  hookForm.saving = false;
}

function openHookEditor(h: HookInfo) {
  hookForm.mode = 'edit';
  hookForm.editing = h;
  hookForm.event = h.event;
  hookForm.scope = h.scope === 'project' ? 'project' : 'user';
  // matcher 为 "*"（匹配全部）时表单显示为空，保存时归一化回 "*"
  hookForm.matcher = h.matcher === '*' ? '' : h.matcher;
  hookForm.command = h.command;
  hookForm.timeoutSec = Math.max(1, Math.round((h.timeout_ms || 60000) / 1000));
  hookForm.statusMessage = h.status_message || '';
  hookForm.saving = false;
}

function closeHookForm() {
  hookForm.mode = 'closed';
  hookForm.editing = null;
  hookForm.saving = false;
}

// 保存：编辑附 source_path + digest（后端定位口径），新建附 scope（写入用户级/项目级文件）
async function submitHookForm() {
  const command = hookForm.command.trim();
  if (!command) {
    store.notifyModal('表单校验', '请填写要执行的命令');
    return;
  }
  const timeoutSec = Number(hookForm.timeoutSec);
  if (!Number.isFinite(timeoutSec) || timeoutSec < 1 || timeoutSec > 300) {
    store.notifyModal('表单校验', '超时时间需为 1-300 秒之间的数字');
    return;
  }
  const payload: Record<string, any> = {
    event: hookForm.event,
    matcher: hookForm.matcher.trim() || '*',
    command,
    timeout_ms: Math.round(timeoutSec * 1000),
    status_message: hookForm.statusMessage.trim(),
  };
  if (hookForm.mode === 'edit' && hookForm.editing) {
    payload.source_path = hookForm.editing.source_path;
    payload.digest = hookForm.editing.digest;
  } else {
    payload.scope = hookForm.scope;
  }
  hookForm.saving = true;
  try {
    const res = await store.saveHook(payload);
    if (res.ok) {
      closeHookForm();
    } else {
      store.notifyModal('保存钩子', res.msg || '未知错误');
    }
  } finally {
    hookForm.saving = false;
  }
}

// 删除钩子（破坏性）：先复用项目统一的 askConfirm 二次确认，再调 store action
async function onDeleteHook() {
  const h = hookForm.editing;
  if (!h) return;
  const ok = await store.askConfirm(
    '删除钩子',
    h.event,
    `将从配置文件中删除该钩子：${h.command}`,
    '删除',
  );
  if (!ok) return;
  const res = await store.deleteHook(h.source_path, h.digest);
  if (res.ok) {
    closeHookForm();
  } else {
    store.notifyModal('删除钩子', res.msg || '未知错误');
  }
}

// 启停开关：checked = 启用，取反得到后端的 disabled 语义
async function onToggleHookDisabled(h: HookInfo, checked: boolean) {
  const res = await store.setHookDisabled(h.source_path, h.digest, !checked);
  if (!res.ok) store.notifyModal('钩子启停', res.msg || '未知错误');
}

// 切页时重置筛选/搜索并关闭在途表单，避免下次进入残留编辑态
watch(() => props.subType, () => {
  hookScopeFilter.value = 'all';
  hookQuery.value = '';
  closeHookForm();
});

// 打开 MCP / 插件 / 钩子 / 自动化页时刷新一次最新状态
watch(
  () => [props.visible, props.subType] as const,
  ([visible, type]) => {
    if (visible && type === 'mcp') {
      store.refreshMcpServers();
    } else if (visible && type === 'plugins') {
      store.refreshPlugins();
    } else if (visible && type === 'hooks') {
      store.refreshHooks();
    } else if (visible && type === 'automations') {
      store.refreshAutomations();
    }
  }
);

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

/* 钩子 Hooks 卡片：命令行按 mono 展示且允许换行，状态徽标按 status 分色 */
.sub-card .hook-cmd {
  font-family: var(--mono);
  white-space: pre-wrap;
  word-break: break-all;
  min-height: 0;
}
.hook-status {
  margin-left: auto;
  flex-shrink: 0;
  font-size: 10.5px;
  padding: 1px 8px;
  border-radius: 6px;
  background: var(--bg3);
  color: var(--text-dim);
}
.hook-status.trusted {
  background: var(--accent-soft);
  color: var(--accent);
}
.hook-status.pending,
.hook-status.stale {
  background: rgba(240, 136, 62, 0.15);
  color: var(--orange);
}
.hook-status.blocked {
  background: rgba(248, 81, 73, 0.12);
  color: var(--red);
}
.hook-status.revoked {
  color: var(--text-faint);
}
.hook-status.disabled-badge {
  background: var(--bg3);
  color: var(--text-faint);
}

/* C13：钩子搜索框 / 单列列表 / 行卡片 */
.hook-search {
  background: var(--bg2);
  border: 1px solid var(--border);
  border-radius: 7px;
  padding: 6px 10px;
  color: var(--text);
  font-size: 12px;
  width: 200px;
  outline: none;
  font-family: inherit;
  transition: 0.15s;
}
.hook-search:focus {
  border-color: var(--accent);
}
.hook-list {
  max-width: 960px;
  margin: 0 auto;
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.hook-row {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 12px 14px;
  background: var(--bg1);
  border: 1px solid var(--border);
  border-radius: 12px;
  cursor: pointer;
  transition: 0.15s;
}
.hook-row:hover {
  border-color: var(--text-faint);
}
.is-disabled .hr-cmd {
  color: var(--text-faint);
  text-decoration: line-through;
}
.hr-ico {
  width: 34px;
  height: 34px;
  border-radius: 10px;
  background: var(--bg2);
  border: 1px solid var(--border);
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--text-dim);
  flex-shrink: 0;
}
.hr-main {
  flex: 1;
  min-width: 0;
}
.hr-line {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
}
.hr-event {
  font-size: 13px;
  font-weight: 600;
  white-space: nowrap;
}
.hr-cmd {
  font-family: var(--mono);
  font-size: 11.5px;
  color: var(--text-dim);
  margin-top: 3px;
  overflow: hidden;
  white-space: nowrap;
  text-overflow: ellipsis;
}
.hr-meta {
  font-size: 10.5px;
  color: var(--text-faint);
  font-family: var(--mono);
  margin-top: 3px;
  overflow: hidden;
  white-space: nowrap;
  text-overflow: ellipsis;
}
.hr-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
}
/* 行内开关紧跟操作按钮，不需要卡片布局的自动右推 */
.hr-actions .switch {
  margin-left: 0;
}
.hook-row .sc-trust-btn {
  padding: 1px 8px;
  font-size: 10px;
  white-space: nowrap;
  vertical-align: middle;
}
.hook-empty {
  border: 1px dashed var(--border);
  border-radius: 12px;
  padding: 44px 24px;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 10px;
  text-align: center;
}
.he-title {
  font-size: 13.5px;
  font-weight: 600;
}
.he-desc {
  font-size: 12px;
  color: var(--text-faint);
  line-height: 1.7;
  max-width: 460px;
}

/* C13：钩子新建/编辑表单 */
.hook-form-page {
  max-width: 960px;
  margin: 0 auto;
}
.hook-back {
  background: transparent;
  border: none;
  color: var(--text-dim);
  font-size: 12px;
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 4px 0;
  cursor: pointer;
  font-family: inherit;
}
.hook-back:hover {
  color: var(--accent);
}
.hook-form-head h3 {
  font-size: 14px;
  margin: 8px 0 4px;
}
.hook-form-head p {
  font-size: 12px;
  color: var(--text-faint);
}
.hook-form-card {
  margin-top: 14px;
  background: var(--bg1);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 16px;
}
.hf-row {
  display: flex;
  gap: 12px;
  margin-bottom: 12px;
}
.hf-field {
  display: flex;
  flex-direction: column;
  gap: 5px;
  flex: 1;
  min-width: 0;
}
.hf-label {
  font-size: 11.5px;
  color: var(--text-dim);
}
.hf-field input,
.hf-field select {
  background: var(--bg2);
  border: 1px solid var(--border);
  border-radius: 7px;
  padding: 7px 10px;
  color: var(--text);
  font-size: 12.5px;
  outline: none;
  font-family: inherit;
  transition: 0.15s;
}
.hf-field input:focus,
.hf-field select:focus {
  border-color: var(--accent);
  box-shadow: 0 0 0 2px var(--accent-soft);
}
.hf-field input.hf-mono {
  font-family: var(--mono);
}
.hf-hint {
  font-size: 10.5px;
  color: var(--text-faint);
  line-height: 1.5;
}
.hf-readonly {
  font-size: 12.5px;
  color: var(--text-dim);
  padding: 7px 0;
}
.hf-advanced {
  border-top: 1px solid var(--border);
  padding-top: 12px;
  margin-bottom: 12px;
}
.hf-advanced summary {
  cursor: pointer;
  font-size: 12px;
  color: var(--text-dim);
  user-select: none;
}
.hf-advanced summary:hover {
  color: var(--text);
}
.hf-advanced[open] summary {
  margin-bottom: 12px;
}
.hf-advanced .hf-row:last-child {
  margin-bottom: 0;
}
.hf-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  border-top: 1px solid var(--border);
  padding-top: 12px;
}
.hf-delete {
  background: transparent;
  border: none;
  color: var(--red);
  font-size: 12.5px;
  cursor: pointer;
  padding: 6px 0;
  font-family: inherit;
}
.hf-delete:hover {
  text-decoration: underline;
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

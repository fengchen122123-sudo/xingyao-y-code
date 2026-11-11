<template>
  <div class="wp-wrap">
    <div class="wp-toolbar">
      <span class="wp-toolbar-label">已保存的工作流</span>
      <span class="wp-flex"></span>
      <button class="wp-btn" data-test="workflow-import-btn" :disabled="importing.busy" @click="onImport">导入文件</button>
      <button class="wp-btn" data-test="workflow-refresh-btn" title="重新扫描工作流目录" @click="store.refreshWorkflows()">刷新</button>
    </div>

    <div v-if="store.workflowsLoading" class="wp-empty">读取中…</div>

    <template v-else>
      <!-- 全局组：对所有项目可见 -->
      <div class="wp-group" data-test="workflow-group-user">
        <div class="wp-group-head">
          <span class="wp-group-name">全局</span>
          <span class="wp-chip">对所有项目可见</span>
          <span class="wp-flex"></span>
          <button class="wp-btn primary" data-test="workflow-create-user" @click="onCreateViaChat()">＋ 通过对话创建</button>
        </div>
        <div v-if="userWorkflows.length === 0" class="wp-group-empty">
          还没有全局工作流。适合放不依赖某个项目的流程，例如资料调研、每日摘要。
        </div>
        <div v-else class="wp-list">
          <div v-for="w in userWorkflows" :key="w.name" class="wp-card" :data-test="`workflow-card-${w.name}`">
            <div class="wp-card-top">
              <span class="wp-name">{{ w.name }}</span>
              <span class="wp-flex"></span>
              <button class="wp-btn" :data-test="`workflow-script-${w.name}`" @click="toggleScript(w)">
                {{ scriptShown[w.name] !== undefined ? '收起脚本' : '查看脚本' }}
              </button>
              <button class="wp-btn" :data-test="`workflow-history-${w.name}`" @click="toggleRuns(w)">
                {{ runsShown[w.name] ? '收起历史' : '运行历史' }}
              </button>
              <button class="wp-btn" :data-test="`workflow-edit-${w.name}`" title="去对话里修改这个工作流" @click="onEditViaChat(w)">修改</button>
              <button class="wp-btn danger" :data-test="`workflow-delete-${w.name}`" @click="onDelete(w)">删除</button>
              <button class="wp-btn primary" :data-test="`workflow-run-${w.name}`" @click="openRun(w)">运行…</button>
            </div>
            <div class="wp-desc">{{ w.description }}</div>
            <div v-if="w.when_to_use" class="wp-when" title="何时使用">适用：{{ w.when_to_use }}</div>
            <div v-if="argEntries(w).length" class="wp-args">
              <div class="wp-args-title">参数</div>
              <div v-for="[argName, decl] in argEntries(w)" :key="argName" class="wp-arg-row">
                <span class="wp-arg-name">{{ argName }}</span>
                <span class="wp-arg-type">{{ decl.type }}{{ decl.required ? ' *' : '' }}</span>
                <span v-if="'default' in decl" class="wp-arg-default" :title="'默认值'">默认 {{ defaultLabel(decl.default) }}</span>
                <span v-if="decl.description" class="wp-arg-desc">{{ decl.description }}</span>
              </div>
            </div>
            <div v-if="runForm.name === w.name" class="wp-run" :data-test="`workflow-run-form-${w.name}`">
              <div v-if="argEntries(w).length === 0" class="wp-run-hint">该工作流无参数，直接运行。</div>
              <template v-else>
                <div v-for="[argName, decl] in argEntries(w)" :key="argName" class="wp-run-row">
                  <label class="wp-run-label" :title="decl.description || argName">
                    {{ argName }}<template v-if="decl.required"> *</template>
                  </label>
                  <input
                    v-if="decl.type === 'string' || decl.type === 'number'"
                    v-model="runForm.values[argName]"
                    :type="decl.type === 'number' ? 'number' : 'text'"
                    :data-test="`workflow-arg-${argName}`"
                    :placeholder="'default' in decl ? `默认 ${defaultLabel(decl.default)}` : ''"
                    style="flex:1; min-width: 160px"
                  />
                  <label v-else-if="decl.type === 'boolean'" class="wp-run-check">
                    <input v-model="runForm.values[argName]" type="checkbox" :data-test="`workflow-arg-${argName}`" />
                    {{ runForm.values[argName] ? '是' : '否' }}
                  </label>
                  <textarea
                    v-else
                    v-model="runForm.values[argName]"
                    :data-test="`workflow-arg-${argName}`"
                    rows="2"
                    placeholder='JSON 值，如 {"base": "main"}'
                    style="flex:1; min-width: 200px"
                  ></textarea>
                </div>
              </template>
              <div v-if="runForm.error" class="wp-run-error">{{ runForm.error }}</div>
              <div class="wp-run-actions">
                <button class="wp-btn" @click="runForm.name = ''">取消</button>
                <button class="wp-btn primary" data-test="workflow-run-send" :disabled="runForm.sending" @click="onRun(w)">
                  {{ runForm.sending ? '发送中…' : '发送到当前会话运行' }}
                </button>
              </div>
            </div>
            <div v-if="scriptShown[w.name] !== undefined" class="wp-script">
              <div v-if="scriptShown[w.name] === ''" class="wp-script-loading">读取中…</div>
              <pre v-else class="wp-script-body">{{ scriptShown[w.name] }}</pre>
              <div class="wp-script-path" :title="w.path">{{ w.path }}</div>
            </div>
            <div v-if="runsShown[w.name]" class="wp-history" :data-test="`workflow-history-box-${w.name}`">
              <div class="wp-history-head">
                <span>最近运行（{{ (store.workflowRuns[w.name] || []).length }} 条）</span>
                <button class="wp-btn" :data-test="`workflow-clear-runs-${w.name}`" @click="store.clearWorkflowRuns(w.name)">清空历史</button>
              </div>
              <div v-if="liveLines(w).length" class="wp-live" :data-test="`workflow-live-${w.name}`">
                <div v-for="(line, i) in liveLines(w)" :key="i" class="wp-live-line">{{ line }}</div>
              </div>
              <div v-if="(store.workflowRuns[w.name] || []).length === 0" class="wp-history-empty">暂无运行记录</div>
              <div v-for="r in store.workflowRuns[w.name] || []" :key="r.id" class="wp-history-row" :title="r.error || r.summary || r.artifact_dir || ''">
                <span class="wp-history-time">{{ r.started_at }}</span>
                <span class="wp-chip" :class="runStatusClass(r.status)">{{ runStatusLabel(r.status) }}</span>
                <span class="wp-history-dur">{{ runDuration(r) }}</span>
                <span v-if="r.args && Object.keys(r.args).length" class="wp-history-args">{{ argsPreview(r.args) }}</span>
                <span v-if="r.summary" class="wp-history-summary">{{ r.summary }}</span>
              </div>
            </div>
          </div>
        </div>
      </div>

      <!-- 项目组：仅当前打开的项目（自由对话下不显示该组） -->
      <div v-if="inProject" class="wp-group" data-test="workflow-group-project">
        <div class="wp-group-head">
          <span class="wp-group-name">{{ projectLabel }}</span>
          <span class="wp-chip">对当前项目可见</span>
          <span class="wp-flex"></span>
          <button class="wp-btn primary" data-test="workflow-create-project" @click="onCreateViaChat()">＋ 通过对话创建</button>
        </div>
        <div v-if="projectWorkflows.length === 0" class="wp-group-card">
          <div class="wp-group-card-text">
            当前项目里还没有保存的工作流。<br />
            在对话里让星瑶设计工作流，跑通之后再让它保存到项目里。
          </div>
          <button class="wp-btn primary" data-test="workflow-create-project-empty" @click="onCreateViaChat()">＋ 通过对话创建</button>
        </div>
        <div v-else class="wp-list">
          <div v-for="w in projectWorkflows" :key="w.name" class="wp-card" :data-test="`workflow-card-${w.name}`">
            <div class="wp-card-top">
              <span class="wp-name">{{ w.name }}</span>
              <span class="wp-flex"></span>
              <button class="wp-btn" :data-test="`workflow-script-${w.name}`" @click="toggleScript(w)">
                {{ scriptShown[w.name] !== undefined ? '收起脚本' : '查看脚本' }}
              </button>
              <button class="wp-btn" :data-test="`workflow-history-${w.name}`" @click="toggleRuns(w)">
                {{ runsShown[w.name] ? '收起历史' : '运行历史' }}
              </button>
              <button class="wp-btn" :data-test="`workflow-edit-${w.name}`" title="去对话里修改这个工作流" @click="onEditViaChat(w)">修改</button>
              <button class="wp-btn danger" :data-test="`workflow-delete-${w.name}`" @click="onDelete(w)">删除</button>
              <button class="wp-btn primary" :data-test="`workflow-run-${w.name}`" @click="openRun(w)">运行…</button>
            </div>
            <div class="wp-desc">{{ w.description }}</div>
            <div v-if="w.when_to_use" class="wp-when" title="何时使用">适用：{{ w.when_to_use }}</div>
            <div v-if="argEntries(w).length" class="wp-args">
              <div class="wp-args-title">参数</div>
              <div v-for="[argName, decl] in argEntries(w)" :key="argName" class="wp-arg-row">
                <span class="wp-arg-name">{{ argName }}</span>
                <span class="wp-arg-type">{{ decl.type }}{{ decl.required ? ' *' : '' }}</span>
                <span v-if="'default' in decl" class="wp-arg-default" :title="'默认值'">默认 {{ defaultLabel(decl.default) }}</span>
                <span v-if="decl.description" class="wp-arg-desc">{{ decl.description }}</span>
              </div>
            </div>
            <div v-if="runForm.name === w.name" class="wp-run" :data-test="`workflow-run-form-${w.name}`">
              <div v-if="argEntries(w).length === 0" class="wp-run-hint">该工作流无参数，直接运行。</div>
              <template v-else>
                <div v-for="[argName, decl] in argEntries(w)" :key="argName" class="wp-run-row">
                  <label class="wp-run-label" :title="decl.description || argName">
                    {{ argName }}<template v-if="decl.required"> *</template>
                  </label>
                  <input
                    v-if="decl.type === 'string' || decl.type === 'number'"
                    v-model="runForm.values[argName]"
                    :type="decl.type === 'number' ? 'number' : 'text'"
                    :data-test="`workflow-arg-${argName}`"
                    :placeholder="'default' in decl ? `默认 ${defaultLabel(decl.default)}` : ''"
                    style="flex:1; min-width: 160px"
                  />
                  <label v-else-if="decl.type === 'boolean'" class="wp-run-check">
                    <input v-model="runForm.values[argName]" type="checkbox" :data-test="`workflow-arg-${argName}`" />
                    {{ runForm.values[argName] ? '是' : '否' }}
                  </label>
                  <textarea
                    v-else
                    v-model="runForm.values[argName]"
                    :data-test="`workflow-arg-${argName}`"
                    rows="2"
                    placeholder='JSON 值，如 {"base": "main"}'
                    style="flex:1; min-width: 200px"
                  ></textarea>
                </div>
              </template>
              <div v-if="runForm.error" class="wp-run-error">{{ runForm.error }}</div>
              <div class="wp-run-actions">
                <button class="wp-btn" @click="runForm.name = ''">取消</button>
                <button class="wp-btn primary" data-test="workflow-run-send" :disabled="runForm.sending" @click="onRun(w)">
                  {{ runForm.sending ? '发送中…' : '发送到当前会话运行' }}
                </button>
              </div>
            </div>
            <div v-if="scriptShown[w.name] !== undefined" class="wp-script">
              <div v-if="scriptShown[w.name] === ''" class="wp-script-loading">读取中…</div>
              <pre v-else class="wp-script-body">{{ scriptShown[w.name] }}</pre>
              <div class="wp-script-path" :title="w.path">{{ w.path }}</div>
            </div>
            <div v-if="runsShown[w.name]" class="wp-history" :data-test="`workflow-history-box-${w.name}`">
              <div class="wp-history-head">
                <span>最近运行（{{ (store.workflowRuns[w.name] || []).length }} 条）</span>
                <button class="wp-btn" :data-test="`workflow-clear-runs-${w.name}`" @click="store.clearWorkflowRuns(w.name)">清空历史</button>
              </div>
              <div v-if="liveLines(w).length" class="wp-live" :data-test="`workflow-live-${w.name}`">
                <div v-for="(line, i) in liveLines(w)" :key="i" class="wp-live-line">{{ line }}</div>
              </div>
              <div v-if="(store.workflowRuns[w.name] || []).length === 0" class="wp-history-empty">暂无运行记录</div>
              <div v-for="r in store.workflowRuns[w.name] || []" :key="r.id" class="wp-history-row" :title="r.error || r.summary || r.artifact_dir || ''">
                <span class="wp-history-time">{{ r.started_at }}</span>
                <span class="wp-chip" :class="runStatusClass(r.status)">{{ runStatusLabel(r.status) }}</span>
                <span class="wp-history-dur">{{ runDuration(r) }}</span>
                <span v-if="r.args && Object.keys(r.args).length" class="wp-history-args">{{ argsPreview(r.args) }}</span>
                <span v-if="r.summary" class="wp-history-summary">{{ r.summary }}</span>
              </div>
            </div>
          </div>
        </div>
      </div>

      <!-- 坏文件 -->
      <div v-if="store.workflowInvalid.length" class="wp-invalid">
        <div class="wp-invalid-title">解析失败（不参与执行，可修复或删除）</div>
        <div v-for="bad in store.workflowInvalid" :key="bad.name" class="wp-invalid-row" :title="bad.path || ''">
          <span class="wp-invalid-name">{{ bad.name }}</span>
          <span class="wp-invalid-err">{{ bad.error }}</span>
        </div>
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, reactive } from 'vue';
import { useAppStore } from '../stores/appStore';
import type { WorkflowArgDecl, WorkflowInfo } from '../types/pywebview';

const store = useAppStore();

// name → 脚本原文；undefined = 未展开，'' = 展开但还在读取
const scriptShown = reactive<Record<string, string>>({});

const runForm = reactive({
  name: '',
  values: {} as Record<string, any>,
  error: '',
  sending: false,
});

const importing = reactive({ busy: false });

// 分组：全局 / 当前项目（自由对话没有项目组——未打开的项目不出现在这里）
const userWorkflows = computed(() => store.workflows.filter(w => w.scope === 'user'));
const projectWorkflows = computed(() => store.workflows.filter(w => w.scope === 'project'));
const inProject = computed(() => !!store.workdir && store.workdir !== 'GLOBAL');
const projectLabel = computed(() => {
  const norm = String(store.workdir || '').replace(/[\\/]+$/, '');
  const idx = Math.max(norm.lastIndexOf('\\'), norm.lastIndexOf('/'));
  return idx >= 0 ? norm.slice(idx + 1) : norm || '项目';
});

function argEntries(w: WorkflowInfo): Array<[string, WorkflowArgDecl]> {
  return Object.entries(w.args || {});
}

function defaultLabel(value: unknown): string {
  if (typeof value === 'string') return value;
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

// 通过对话创建：预填当前会话输入框并回到对话，由用户补全需求后发送；
// 星瑶会用 workflow_save 落成 .dwf.ts 文件并生成同名技能
function onCreateViaChat() {
  store.prefillComposer('帮我创建一个工作流：');
  store.closeSubpage();
}

// 通过对话修改：带出工作流名与查看/保存路径，用户补全修改要求
function onEditViaChat(w: WorkflowInfo) {
  store.prefillComposer(
    `请修改工作流「${w.name}」（可先用 workflow_list 查看它的定义，改完用 workflow_save 以 overwrite 保存）：我的修改要求：`,
  );
  store.closeSubpage();
}

async function onImport() {
  const paths = await store.pickWorkflowFiles();
  if (!paths) return; // 取消 / 未选
  const scope = inProject.value ? 'project' : 'user';
  importing.busy = true;
  try {
    let result = await store.importWorkflows(paths, scope);
    // 同名已存在：确认后对这部分文件覆盖重导
    const exists = (result.failed || []).filter(f => f.exists);
    if (exists.length && result.failed) {
      const ok = await store.askConfirm(
        '覆盖同名工作流',
        `导入 ${scope === 'project' ? '项目' : '全局'}档`,
        `${exists.map(f => f.name).join('、')} 已存在，覆盖后星瑶侧的修改将丢失，是否继续？`,
      );
      if (ok) {
        const retry = await store.importWorkflows(paths, scope, true);
        result = retry;
      }
    }
    const importedCount = result.imported.length;
    const realFailed = (result.failed || []).filter(f => !f.exists || !importedCount);
    const lines: string[] = [];
    if (importedCount) lines.push(`导入 ${importedCount} 个：${result.imported.map(i => i.name).join('、')}（${scope === 'project' ? '项目' : '全局'}档）`);
    const failText = realFailed.length
      ? realFailed.map(f => `${f.name}：${f.error}`).join('\n')
      : (exists.length && !importedCount ? '同名文件未覆盖（已取消）' : '');
    if (failText) lines.push(failText);
    if (lines.length) store.notifyModal('星流工作流', lines.join('\n'));
  } finally {
    importing.busy = false;
  }
}

async function onDelete(w: WorkflowInfo) {
  const ok = await store.askConfirm(
    '删除工作流',
    w.name,
    `将删除${w.scope === 'project' ? '项目' : '全局'}档的工作流「${w.name}」及其生成的同名技能。`,
  );
  if (ok) void store.deleteWorkflow(w.name, w.scope);
}

async function toggleScript(w: WorkflowInfo) {
  if (scriptShown[w.name] !== undefined) {
    delete scriptShown[w.name];
    return;
  }
  scriptShown[w.name] = '';
  const detail = await store.loadWorkflowDetail(w.name);
  scriptShown[w.name] = detail && detail.body ? detail.body : '（无脚本文本）';
}

// 运行历史：展开时按需拉取台账（workflow_run 登记 / workflow_finish 回写）
const runsShown = reactive<Record<string, boolean>>({});

function toggleRuns(w: WorkflowInfo) {
  runsShown[w.name] = !runsShown[w.name];
  if (runsShown[w.name]) void store.loadWorkflowRuns(w.name);
}

function runStatusLabel(status: string): string {
  const map: Record<string, string> = {
    running: '运行中', completed: '已完成', failed: '失败', interrupted: '已中断',
  };
  return map[status] || status;
}

function runStatusClass(status: string): string {
  if (status === 'completed') return 'ok';
  if (status === 'failed') return 'bad';
  if (status === 'running') return 'busy';
  return '';
}

function runDuration(r: { started_at: string; finished_at: string }): string {
  if (!r.finished_at) return '—';
  const start = Date.parse(r.started_at.replace(' ', 'T'));
  const end = Date.parse(r.finished_at.replace(' ', 'T'));
  if (Number.isNaN(start) || Number.isNaN(end)) return '—';
  const seconds = Math.max(0, Math.round((end - start) / 1000));
  if (seconds < 60) return `${seconds}s`;
  return `${Math.floor(seconds / 60)}m${seconds % 60}s`;
}

function argsPreview(args: Record<string, any>): string {
  const parts = Object.entries(args).map(([k, v]) => `${k}=${typeof v === 'string' ? v : JSON.stringify(v)}`);
  const text = parts.join(' ');
  return text.length > 60 ? text.slice(0, 59) + '…' : text;
}

// 实时事件条：最新一次运行仍在 running 时，把解释执行器的节点事件翻成一行行进度
function liveLines(w: WorkflowInfo): string[] {
  const runs = store.workflowRuns[w.name] || [];
  const active = runs.find(r => r.status === 'running');
  if (!active) return [];
  const events = store.workflowLiveEvents[active.id] || [];
  return events.slice(-6).map(e => eventLine(e.kind, e.payload));
}

function eventLine(kind: string, p: Record<string, any>): string {
  switch (kind) {
    case 'phase_started': return `▶ 阶段 (${p.index}/${p.total})：${p.name}`;
    case 'agent_started': return `⚙ 派发子代理 ${p.name}`;
    case 'agent_completed': return `✔ 子代理 ${p.name} 完成`;
    case 'agent_failed': return `✘ 子代理 ${p.name} 失败`;
    case 'command_started': return `$ ${(p.argv || []).join(' ')}`;
    case 'command_completed': return `✔ 命令完成`;
    case 'command_skipped': return `⚠ 命令跳过：${p.reason || ''}`;
    case 'report_item': return `📦 [${p.label}] ${p.value}`;
    case 'log_line': return String(p.message || '');
    case 'loop_iteration': return `↻ 第 ${p.iteration}/${p.max} 轮`;
    case 'fallback_used': return `⚠ 回退分支：${p.error || ''}`;
    case 'run_completed': return '✔ 工作流执行完成';
    case 'run_failed': return `✘ 工作流失败：${p.error || ''}`;
    default: return kind;
  }
}

function openRun(w: WorkflowInfo) {
  runForm.name = w.name;
  runForm.error = '';
  runForm.values = {};
  for (const [argName, decl] of argEntries(w)) {
    if (decl.type === 'boolean') {
      // 布尔没有「留空」态：有默认用默认，否则 false
      runForm.values[argName] = decl.default === true;
    } else {
      // 有默认值的参数留空（输入框 placeholder 展示默认值），
      // 留空 = 按声明的 default 生效，不显式注入 prompt
      runForm.values[argName] = '';
    }
  }
}

function formatArgValue(decl: WorkflowArgDecl, raw: unknown): string {
  if (typeof raw === 'boolean') return raw ? 'true' : 'false';
  const text = String(raw ?? '').trim();
  if (decl.type === 'json' && text) {
    try {
      return JSON.stringify(JSON.parse(text));
    } catch {
      return JSON.stringify(text); // 非 JSON 文本按字符串字面量传入
    }
  }
  return text;
}

async function onRun(w: WorkflowInfo) {
  const parts: string[] = [];
  for (const [argName, decl] of argEntries(w)) {
    const raw = runForm.values[argName];
    const text = typeof raw === 'boolean' ? (raw ? 'true' : 'false') : String(raw ?? '').trim();
    if (decl.required && !text) {
      runForm.error = `参数 ${argName} 必填`;
      return;
    }
    if ('default' in decl && !text) continue; // 留空走声明默认值
    if (text) parts.push(`${argName}=${formatArgValue(decl, raw)}`);
  }
  const prompt = parts.length
    ? `请运行星瑶工作流「${w.name}」，参数：${parts.join('；')}`
    : `请运行星瑶工作流「${w.name}」`;
  runForm.sending = true;
  try {
    await store.sendMessage(prompt);
    store.closeSubpage();
  } finally {
    runForm.sending = false;
  }
}
</script>

<style scoped>
.wp-wrap {
  max-width: 960px;
  margin: 0 auto;
  display: flex;
  flex-direction: column;
  gap: 16px;
}
.wp-toolbar {
  display: flex;
  align-items: center;
  gap: 8px;
}
.wp-toolbar-label { font-size: 12.5px; color: var(--text-dim); }
.wp-flex { flex: 1; }
.wp-empty {
  color: var(--text-faint);
  font-size: 12.5px;
  text-align: center;
  padding: 30px 0;
  line-height: 2;
}
.wp-group { display: flex; flex-direction: column; gap: 10px; }
.wp-group-head { display: flex; align-items: center; gap: 8px; }
.wp-group-name { font-size: 13px; font-weight: 600; }
.wp-group-empty {
  color: var(--text-faint);
  font-size: 12px;
  line-height: 1.8;
  padding: 2px 0;
}
.wp-group-card {
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 26px 16px;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 12px;
}
.wp-group-card-text {
  color: var(--text-faint);
  font-size: 12px;
  line-height: 1.9;
  text-align: center;
}
.wp-list { display: flex; flex-direction: column; gap: 12px; }
.wp-card {
  background: var(--bg1);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 14px 16px;
}
.wp-card-top { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.wp-name { font-size: 13.5px; font-weight: 600; font-family: var(--mono); }
.wp-chip {
  font-size: 10.5px;
  padding: 1px 8px;
  border-radius: 6px;
  background: var(--bg3);
  color: var(--text-dim);
  flex-shrink: 0;
}
.wp-desc { font-size: 12.5px; margin-top: 6px; line-height: 1.6; }
.wp-when {
  font-size: 11.5px;
  color: var(--text-faint);
  margin-top: 4px;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.wp-args {
  margin-top: 10px;
  border-top: 1px dashed var(--border);
  padding-top: 8px;
}
.wp-args-title { font-size: 11px; color: var(--text-faint); margin-bottom: 4px; }
.wp-arg-row {
  display: flex;
  align-items: baseline;
  gap: 8px;
  font-size: 11.5px;
  padding: 2px 0;
  flex-wrap: wrap;
}
.wp-arg-name { font-family: var(--mono); color: var(--text-dim); }
.wp-arg-type { color: var(--accent); flex-shrink: 0; }
.wp-arg-default { color: var(--text-faint); flex-shrink: 0; }
.wp-arg-desc { color: var(--text-faint); flex: 1; min-width: 120px; }
.wp-run {
  margin-top: 10px;
  background: var(--bg2);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 10px 12px;
}
.wp-run-hint { font-size: 11.5px; color: var(--text-faint); }
.wp-run-row { display: flex; gap: 8px; align-items: center; margin-bottom: 8px; flex-wrap: wrap; }
.wp-run-label {
  font-size: 12px;
  color: var(--text-dim);
  font-family: var(--mono);
  min-width: 90px;
  flex-shrink: 0;
}
.wp-run-check {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: var(--text-dim);
  flex-shrink: 0;
}
.wp-run-error {
  color: var(--red);
  font-size: 11.5px;
  margin-bottom: 6px;
}
.wp-run-actions { display: flex; gap: 6px; justify-content: flex-end; }
.wp-script {
  margin-top: 10px;
  border-top: 1px dashed var(--border);
  padding-top: 8px;
}
.wp-script-loading { font-size: 11.5px; color: var(--text-faint); }
.wp-script-body {
  margin: 0;
  background: var(--bg2);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 10px 12px;
  font-family: var(--mono);
  font-size: 11.5px;
  line-height: 1.6;
  max-height: 280px;
  overflow: auto;
  white-space: pre-wrap;
  word-break: break-all;
}
.wp-script-path {
  font-size: 10.5px;
  color: var(--text-faint);
  margin-top: 4px;
  font-family: var(--mono);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.wp-history {
  margin-top: 10px;
  border-top: 1px dashed var(--border);
  padding-top: 10px;
}
.wp-history-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: 11.5px;
  color: var(--text-dim);
  margin-bottom: 8px;
}
.wp-history-empty { font-size: 11.5px; color: var(--text-faint); padding: 4px 0; }
.wp-history-row {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 11.5px;
  padding: 4px 0;
  color: var(--text-dim);
  flex-wrap: wrap;
}
.wp-history-time { font-family: var(--mono); color: var(--text-faint); }
.wp-history-dur { color: var(--text-faint); }
.wp-history-args { font-family: var(--mono); color: var(--text-faint); }
.wp-history-summary {
  color: var(--text-dim);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  max-width: 360px;
}
.wp-chip.ok { background: var(--accent-soft); color: var(--accent); }
.wp-chip.bad { background: rgba(248, 81, 73, 0.12); color: var(--red); }
.wp-chip.busy { background: rgba(210, 153, 34, 0.14); color: var(--yellow); }
.wp-live {
  background: var(--bg2);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 8px 12px;
  margin-bottom: 8px;
  font-family: var(--mono);
  font-size: 11.5px;
  line-height: 1.7;
  color: var(--text-dim);
  max-height: 180px;
  overflow: hidden;
  display: flex;
  flex-direction: column;
  justify-content: flex-end;
}
.wp-invalid {
  background: rgba(248, 81, 73, 0.06);
  border: 1px solid rgba(248, 81, 73, 0.25);
  border-radius: 10px;
  padding: 10px 14px;
}
.wp-invalid-title { font-size: 11.5px; color: var(--red); margin-bottom: 6px; }
.wp-invalid-row {
  display: flex;
  gap: 10px;
  font-size: 11.5px;
  padding: 2px 0;
  flex-wrap: wrap;
}
.wp-invalid-name { font-family: var(--mono); color: var(--text-dim); }
.wp-invalid-err { color: var(--text-faint); }
.wp-wrap input,
.wp-wrap select,
.wp-wrap textarea {
  background: var(--bg2);
  border: 1px solid var(--border);
  border-radius: 7px;
  color: var(--text);
  padding: 7px 10px;
  font-size: 12.5px;
  outline: none;
  font-family: inherit;
}
.wp-wrap textarea { resize: vertical; line-height: 1.5; font-family: var(--mono); }
.wp-wrap input:focus, .wp-wrap textarea:focus { border-color: var(--accent); }
.wp-btn {
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
  flex-shrink: 0;
}
.wp-btn:hover { border-color: var(--text-faint); color: var(--text); }
.wp-btn.danger:hover { border-color: var(--red); color: var(--red); background: rgba(248, 81, 73, 0.1); }
.wp-btn.primary {
  background: var(--accent);
  border-color: transparent;
  color: #fff;
  font-weight: 600;
}
.wp-btn:disabled { opacity: 0.6; cursor: not-allowed; }
</style>

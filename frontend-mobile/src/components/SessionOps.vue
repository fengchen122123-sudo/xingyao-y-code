<template>
  <!-- 会话操作（对齐 preview：模型+思考强度/权限/更多操作/重命名/清空/导出/删除，列表长按与详情 ⋯ 共用） -->
  <div>
    <van-popup :show="show" position="bottom" round @update:show="v => !v && emit('close')">
      <div class="sheet">
        <div class="sh-h"><span>会话操作</span><span class="x" @click="emit('close')">✕</span></div>
        <div class="menui" @click="openModel"><svg class="i lg"><use href="#i-chip" /></svg>切换模型<span class="cur mono">{{ shortModel }} <svg class="i sm"><use href="#i-chev-r" /></svg></span></div>
        <div class="menui" @click="openPerm"><svg class="i lg"><use href="#i-shield" /></svg>切换权限档位<span class="cur mono">{{ store.permissionMode }} <svg class="i sm"><use href="#i-chev-r" /></svg></span></div>
        <div v-if="canInsert" class="menui" @click="openMore"><svg class="i lg"><use href="#i-more" /></svg>更多操作<span class="cur">技能 · MCP · 文件 <svg class="i sm"><use href="#i-chev-r" /></svg></span></div>
        <div class="menui" @click="startRename"><svg class="i lg"><use href="#i-pencil" /></svg>重命名</div>
        <div class="menui" @click="doClear"><svg class="i lg"><use href="#i-broom" /></svg>清空历史</div>
        <div class="menui" @click="doExport"><svg class="i lg"><use href="#i-download" /></svg>导出 Markdown</div>
        <div class="menui" @click="switchProject"><svg class="i lg"><use href="#i-folder" /></svg>切换绑定的项目</div>
        <div class="menui bad" style="border-top:1px solid var(--line)" @click="doDelete"><svg class="i lg"><use href="#i-trash" /></svg>删除会话</div>
      </div>
    </van-popup>

    <!-- 切换模型（含思考强度，对齐客户端） -->
    <van-popup v-model:show="modelDlg" position="bottom" round :style="{ maxHeight: '72%' }">
      <div class="sheet">
        <div class="sh-h"><span class="bk" @click="backToMain">‹</span><span>切换模型</span><span class="x" @click="modelDlg = false">✕</span></div>
        <div v-for="m in store.models" :key="m.id" class="opt" :class="{ on: m.id === store.activeModelId }" @click="pickModel(m.id)">
          <span class="mono">{{ m.name || m.id }}</span>
          <span v-if="m.id === store.activeModelId" class="cur mono"><svg class="i sm"><use href="#i-check" /></svg>当前</span>
        </div>
        <van-empty v-if="!store.models.length" description="暂无可用模型" />
        <div
          v-if="store.reasoning.visible"
          class="opt reasonrow"
          :class="{ disabled: store.reasoning.disabled }"
          @click="onReasonRow"
        >
          <svg class="i lg"><use href="#i-brain" /></svg>思考强度
          <span class="cur mono">{{ reasonLabel }} <svg v-if="!store.reasoning.disabled" class="i sm"><use href="#i-chev-r" /></svg></span>
        </div>
        <div v-if="store.reasoning.disabled && store.reasoning.disabled_reason" class="hintline">{{ store.reasoning.disabled_reason }}</div>
      </div>
    </van-popup>

    <!-- 思考强度 -->
    <van-popup v-model:show="reasonDlg" position="bottom" round>
      <div class="sheet">
        <div class="sh-h"><span class="bk" @click="backToModel">‹</span><span>思考强度</span><span class="x" @click="reasonDlg = false">✕</span></div>
        <div v-for="o in store.reasoning.options" :key="o.value" class="opt" :class="{ on: o.value === store.reasoning.choice }" @click="pickReason(o.value)">
          <span>{{ o.label }}</span>
          <span v-if="o.value === store.reasoning.choice" class="cur mono"><svg class="i sm"><use href="#i-check" /></svg>当前</span>
        </div>
      </div>
    </van-popup>

    <!-- 权限档位（对齐客户端四档） -->
    <van-popup v-model:show="permDlg" position="bottom" round>
      <div class="sheet">
        <div class="sh-h"><span class="bk" @click="backToMain">‹</span><span>权限档位</span><span class="x" @click="permDlg = false">✕</span></div>
        <div v-for="p in PERM_MODES" :key="p.value" class="opt" :class="{ on: p.value === store.permissionMode }" @click="pickPerm(p.value)">
          <span class="mono">{{ p.value }}</span><span class="d">{{ p.label }}</span>
          <span v-if="p.value === store.permissionMode" class="cur mono"><svg class="i sm"><use href="#i-check" /></svg>当前</span>
        </div>
      </div>
    </van-popup>

    <!-- 更多操作 -->
    <van-popup v-model:show="moreDlg" position="bottom" round>
      <div class="sheet">
        <div class="sh-h"><span class="bk" @click="backToMain">‹</span><span>更多操作</span><span class="x" @click="moreDlg = false">✕</span></div>
        <div class="menui" @click="openSkill"><svg class="i lg"><use href="#i-zap" /></svg>调用技能<span class="cur">/技能名 <svg class="i sm"><use href="#i-chev-r" /></svg></span></div>
        <div class="menui" @click="openMcp"><svg class="i lg"><use href="#i-plug" /></svg>调用 MCP<span class="cur">/mcp:名称 <svg class="i sm"><use href="#i-chev-r" /></svg></span></div>
        <div class="menui" @click="openFiles"><svg class="i lg"><use href="#i-folder" /></svg>引用文件<span class="cur">@路径 <svg class="i sm"><use href="#i-chev-r" /></svg></span></div>
        <div class="menui bad" style="border-top:1px solid var(--line)" @click="doClear"><svg class="i lg"><use href="#i-broom" /></svg>清空历史</div>
      </div>
    </van-popup>

    <!-- 调用技能 -->
    <van-popup v-model:show="skillDlg" position="bottom" round :style="{ maxHeight: '70%' }">
      <div class="sheet">
        <div class="sh-h"><span class="bk" @click="backToMore">‹</span><span>调用技能</span><span class="x" @click="skillDlg = false">✕</span></div>
        <div v-for="s in enabledSkills" :key="s.short_id" class="opt" @click="insertText('/' + s.short_id + ' ')">
          <span class="mono">/{{ s.short_id }}</span><span class="d">{{ s.description || s.name }}</span>
        </div>
        <van-empty v-if="!enabledSkills.length" description="暂无可用技能" />
      </div>
    </van-popup>

    <!-- 调用 MCP -->
    <van-popup v-model:show="mcpDlg" position="bottom" round :style="{ maxHeight: '70%' }">
      <div class="sheet">
        <div class="sh-h"><span class="bk" @click="backToMore">‹</span><span>调用 MCP</span><span class="x" @click="mcpDlg = false">✕</span></div>
        <div v-for="m in enabledMcps" :key="m.name" class="opt" @click="insertText('/mcp:' + m.name + ' ')">
          <span class="mono">/mcp:{{ m.name }}</span><span class="d">{{ m.tools_count ? m.tools_count + ' 个工具' : 'MCP 服务' }}</span>
        </div>
        <van-empty v-if="!enabledMcps.length" description="暂无可用 MCP" />
      </div>
    </van-popup>

    <!-- 引用文件 -->
    <van-popup v-model:show="fileDlg" position="bottom" round :style="{ maxHeight: '70%' }">
      <div class="sheet">
        <div class="sh-h"><span class="bk" @click="backToMore">‹</span><span>引用文件</span><span class="x" @click="fileDlg = false">✕</span></div>
        <div class="frow"><input v-model="fileQuery" placeholder="搜索工作区文件…" /></div>
        <div v-for="f in fileResults" :key="f" class="opt" @click="insertText('@' + f + ' ')"><span class="mono">{{ f }}</span></div>
        <van-empty v-if="!fileSearching && !fileResults.length" description="无匹配文件" />
      </div>
    </van-popup>

    <!-- 重命名 -->
    <van-popup v-model:show="renDlg" position="bottom" round>
      <div class="sheet">
        <div class="sh-h"><span class="bk" @click="backToMain">‹</span><span>重命名会话</span><span class="x" @click="renDlg = false">✕</span></div>
        <div class="frow"><input v-model="renTitle" maxlength="120" /></div>
        <div class="btnrow">
          <button class="btn-g" @click="renDlg = false">取消</button>
          <button class="btn-p" @click="doRename">保存</button>
        </div>
      </div>
    </van-popup>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, watch, type Ref } from 'vue';
import { showConfirmDialog, showFailToast, showToast } from 'vant';
import { useAppStore, type SessionItem } from '../stores/app';

const props = defineProps<{ show: boolean; target: SessionItem | null; canInsert?: boolean }>();
const emit = defineEmits<{
  (e: 'close'): void;
  (e: 'reopen'): void;
  (e: 'deleted'): void;
  (e: 'insert', text: string): void;
}>();

const store = useAppStore();

const PERM_MODES = [
  { value: 'confirm', label: '变更前确认' },
  { value: 'accept_edits', label: '自动编辑' },
  { value: 'plan', label: '计划模式' },
  { value: 'full', label: '完全访问' },
];

const modelDlg = ref(false);
const reasonDlg = ref(false);
const permDlg = ref(false);
const moreDlg = ref(false);
const skillDlg = ref(false);
const mcpDlg = ref(false);
const fileDlg = ref(false);
const renDlg = ref(false);
const renTitle = ref('');
const fileQuery = ref('');
const fileResults = ref<string[]>([]);
const fileSearching = ref(false);

const shortModel = computed(() => (store.activeModelName || store.activeModelId || '默认').slice(0, 14));
const reasonLabel = computed(() => {
  const r = store.reasoning;
  if (r.kind === 'fixed') return '开启';
  return r.options.find((o) => o.value === r.choice)?.label || r.choice;
});
const enabledSkills = computed(() =>
  (store.skills || []).filter((s: any) => s.enabled !== false && s.short_id),
);
const enabledMcps = computed(() => (store.mcps || []).filter((m: any) => m.enabled));

function closeSubs() {
  modelDlg.value = false;
  reasonDlg.value = false;
  permDlg.value = false;
  moreDlg.value = false;
  skillDlg.value = false;
  mcpDlg.value = false;
  fileDlg.value = false;
  renDlg.value = false;
}

/** 打开子弹层：收起主弹层，返回时再 emit('reopen') 复原 */
function openChild(target: Ref<boolean>) {
  closeSubs();
  target.value = true;
  emit('close');
}
function backToMain() {
  closeSubs();
  emit('reopen');
}
function openModel() { openChild(modelDlg); }
function openPerm() { openChild(permDlg); }
function openMore() { openChild(moreDlg); }
function openSkill() { openChild(skillDlg); }
function openMcp() { openChild(mcpDlg); }
function backToMore() {
  skillDlg.value = false;
  mcpDlg.value = false;
  fileDlg.value = false;
  moreDlg.value = true;
}
function backToModel() {
  reasonDlg.value = false;
  modelDlg.value = true;
}

// 菜单每次打开时同步最新会话列表；主弹层被用户关闭（无子弹层）时收起子弹层
watch(() => props.show, (v) => {
  if (v) { void store.refreshSessions(); return; }
  const anySub = modelDlg.value || reasonDlg.value || permDlg.value || moreDlg.value
    || skillDlg.value || mcpDlg.value || fileDlg.value || renDlg.value;
  if (!anySub) closeSubs();
});

async function pickModel(id: string) {
  // 保持弹层打开：下方「思考强度」行随新模型刷新
  try {
    await store.setModel(id);
    showToast('模型已切换');
  } catch (e: any) {
    showFailToast(e?.message || '切换失败');
  }
}

function onReasonRow() {
  if (store.reasoning.disabled) { showToast(store.reasoning.disabled_reason || '该模型始终开启思考'); return; }
  openChild(reasonDlg);
}

async function pickReason(choice: string) {
  backToModel();
  try {
    const msg = await store.setReasoning(choice);
    showToast(msg || '思考强度已调整');
  } catch (e: any) {
    showFailToast(e?.message || '设置失败');
  }
}

async function pickPerm(mode: string) {
  permDlg.value = false;
  try {
    await store.setPermissionMode(mode);
    showToast('权限档位已切换');
  } catch (e: any) {
    showFailToast(e?.message || '切换失败');
  }
}

// ---- 更多操作 ----
function openFiles() {
  openChild(fileDlg);
  fileQuery.value = '';
  void runFileSearch('');
}

let fileTimer: ReturnType<typeof setTimeout> | undefined;
watch(fileQuery, (q) => {
  clearTimeout(fileTimer);
  fileTimer = setTimeout(() => void runFileSearch(q), 250);
});

async function runFileSearch(q: string) {
  fileSearching.value = true;
  try {
    fileResults.value = await store.searchWorkspaceFiles(q, 20);
  } catch {
    fileResults.value = [];
  } finally {
    fileSearching.value = false;
  }
}

/** 选中技能 / MCP / 文件：插入输入框并收起整个会话操作 */
function insertText(text: string) {
  emit('insert', text);
  closeSubs();
  emit('close');
}

function startRename() {
  if (!props.target) return;
  renTitle.value = props.target.display_name;
  openChild(renDlg);
}

async function doRename() {
  if (!props.target) return;
  const newName = renTitle.value.trim();
  if (!newName || newName === props.target.display_name) { renDlg.value = false; return; }
  try {
    await store.renameSession(props.target.name, newName);
    renDlg.value = false;
    emit('close');
    showToast('已重命名');
  } catch (e: any) {
    showFailToast(e?.message || '重命名失败');
  }
}

async function doClear() {
  if (!props.target) return;
  closeSubs();
  try {
    await showConfirmDialog({ title: '清空历史', message: `清空会话「${props.target.display_name}」的全部历史？` });
  } catch { return; }
  try {
    await store.clearHistory(props.target.name);
    emit('close');
    showToast('已清空');
  } catch (e: any) {
    showFailToast(e?.message || '清空失败');
  }
}

async function doExport() {
  if (!props.target) return;
  try {
    if (props.target.name !== store.currentSession) await store.switchSession(props.target.name);
    const p = await store.exportSession();
    emit('close');
    showToast(p || '已导出');
  } catch (e: any) {
    showFailToast(e?.message || '导出失败');
  }
}

function switchProject() {
  // 移动端保持轻量：切绑定项目回列表页长按后建新会话绑项目，或后续在
  // 此处内嵌目录浏览器；先给出明确提示避免静默无效
  emit('close');
  showToast('新建会话时可绑定项目；已存在会话暂不支持改绑');
}

async function doDelete() {
  if (!props.target) return;
  try {
    await showConfirmDialog({ title: '删除会话', message: `删除会话「${props.target.display_name}」？不可恢复。` });
  } catch { return; }
  try {
    await store.deleteSession(props.target.name);
    emit('close');
    emit('deleted');
    showToast('已删除');
  } catch (e: any) {
    showFailToast(e?.message || '删除失败');
  }
}
</script>

<style scoped>
.sheet { padding: 14px 16px 20px; }
.sh-h { display: flex; align-items: center; gap: 8px; padding: 0; margin-bottom: 10px; font-size: 13px; font-weight: 600; }
.sh-h .x { font-size: 12px; color: var(--text-3); font-weight: 400; padding: 4px 8px; margin-left: auto; }
.sh-h .bk { color: var(--text-2); font-weight: 400; font-size: 19px; line-height: 1; cursor: pointer; padding: 0 4px 0 0; }
.sh-h .bk:active { color: var(--accent); }
.menui { padding: 12px 16px; font-size: 14px; cursor: pointer; display: flex; gap: 11px; align-items: center; }
.menui:active { background: var(--bg-inset); }
.menui.bad { color: var(--danger); }
.menui .cur { margin-left: auto; font-size: 10px; color: var(--accent); display: inline-flex; gap: 4px; align-items: center; }
.menui .i { color: var(--text-2); }
.menui.bad .i { color: var(--danger); }
.opt { padding: 12px 16px; font-size: 13.5px; cursor: pointer; display: flex; gap: 10px; align-items: baseline; }
.opt:active { background: var(--bg-inset); }
.opt .d { font-size: 11px; color: var(--text-3); }
.opt.on { color: var(--accent); }
.opt .cur { margin-left: auto; font-size: 10px; color: var(--accent); display: inline-flex; gap: 4px; align-items: center; }
.reasonrow { border-top: 1px solid var(--line); align-items: center; color: var(--text-1); }
.reasonrow .i { color: var(--accent); }
.reasonrow.disabled { color: var(--text-3); cursor: default; }
.reasonrow.disabled .i { color: var(--text-3); }
.hintline { padding: 4px 16px 6px; font-size: 10.5px; color: var(--text-3); line-height: 1.6; }
.frow { display: flex; flex-direction: column; gap: 4px; margin: 8px 0 0; }
.frow input { padding: 9px 11px; border-radius: 8px; border: 1px solid var(--line-strong); background: var(--bg-inset); color: var(--text-1); font-size: 13px; outline: none; font-family: inherit; }
.btnrow { display: flex; gap: 8px; margin: 16px 0 0; }
.btnrow button { flex: 1; padding: 10px; border-radius: 8px; font-size: 13px; }
.btn-g { border: 1px solid var(--line-strong); background: none; color: var(--text-2); }
.btn-p { border: none; background: var(--accent); color: var(--accent-text); font-weight: 600; }
</style>

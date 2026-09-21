<template>
  <!-- 会话操作（对齐 preview：模型/权限/重命名/清空/导出/删除，列表长按与详情 ⋯ 共用） -->
  <div>
    <van-popup :show="show" position="bottom" round @update:show="v => !v && emit('close')">
      <div class="sheet">
        <div class="sh-h"><span>会话操作</span><span class="x" @click="emit('close')">✕</span></div>
        <div class="menui" @click="modelDlg = true"><svg class="i lg"><use href="#i-chip" /></svg>切换模型<span class="cur mono">{{ shortModel }} <svg class="i sm"><use href="#i-chev-r" /></svg></span></div>
        <div class="menui" @click="permDlg = true"><svg class="i lg"><use href="#i-shield" /></svg>切换权限档位<span class="cur mono">{{ store.permissionMode }} <svg class="i sm"><use href="#i-chev-r" /></svg></span></div>
        <div class="menui" @click="startRename"><svg class="i lg"><use href="#i-pencil" /></svg>重命名</div>
        <div class="menui" @click="doClear"><svg class="i lg"><use href="#i-broom" /></svg>清空历史</div>
        <div class="menui" @click="doExport"><svg class="i lg"><use href="#i-download" /></svg>导出 Markdown</div>
        <div class="menui" @click="switchProject"><svg class="i lg"><use href="#i-folder" /></svg>切换绑定的项目</div>
        <div class="menui bad" style="border-top:1px solid var(--line)" @click="doDelete"><svg class="i lg"><use href="#i-trash" /></svg>删除会话</div>
      </div>
    </van-popup>

    <!-- 模型切换 -->
    <van-popup v-model:show="modelDlg" position="bottom" round :style="{ maxHeight: '60%' }">
      <div class="sheet">
        <div class="sh-h"><span>切换模型</span><span class="x" @click="modelDlg = false">✕</span></div>
        <div v-for="m in store.models" :key="m.id" class="opt" :class="{ on: m.id === store.activeModelId }" @click="pickModel(m.id)">
          <span class="mono">{{ m.name || m.id }}</span>
          <span v-if="m.id === store.activeModelId" class="cur mono"><svg class="i sm"><use href="#i-check" /></svg>当前</span>
        </div>
        <van-empty v-if="!store.models.length" description="暂无可用模型" />
      </div>
    </van-popup>

    <!-- 权限档位（对齐客户端四档） -->
    <van-popup v-model:show="permDlg" position="bottom" round>
      <div class="sheet">
        <div class="sh-h"><span>权限档位</span><span class="x" @click="permDlg = false">✕</span></div>
        <div v-for="p in PERM_MODES" :key="p.value" class="opt" :class="{ on: p.value === store.permissionMode }" @click="pickPerm(p.value)">
          <span class="mono">{{ p.value }}</span><span class="d">{{ p.label }}</span>
          <span v-if="p.value === store.permissionMode" class="cur mono"><svg class="i sm"><use href="#i-check" /></svg>当前</span>
        </div>
      </div>
    </van-popup>

    <!-- 重命名 -->
    <van-popup v-model:show="renDlg" position="bottom" round>
      <div class="sheet">
        <div class="sh-h"><span>重命名会话</span><span class="x" @click="renDlg = false">✕</span></div>
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
import { computed, ref, watch } from 'vue';
import { showConfirmDialog, showFailToast, showToast } from 'vant';
import { useAppStore, type SessionItem } from '../stores/app';

const props = defineProps<{ show: boolean; target: SessionItem | null }>();
const emit = defineEmits<{ (e: 'close'): void; (e: 'deleted'): void }>();

const store = useAppStore();

const PERM_MODES = [
  { value: 'confirm', label: '变更前确认' },
  { value: 'accept_edits', label: '自动编辑' },
  { value: 'plan', label: '计划模式' },
  { value: 'full', label: '完全访问' },
];

const modelDlg = ref(false);
const permDlg = ref(false);
const renDlg = ref(false);
const renTitle = ref('');

const shortModel = computed(() => (store.activeModelName || store.activeModelId || '默认').slice(0, 14));

// 菜单每次打开时同步最新会话列表（长按目标可能已被删除）
watch(() => props.show, (v) => { if (v) void store.refreshSessions(); });

async function pickModel(id: string) {
  modelDlg.value = false;
  try {
    await store.setModel(id);
    showToast('模型已切换');
  } catch (e: any) {
    showFailToast(e?.message || '切换失败');
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

function startRename() {
  if (!props.target) return;
  renTitle.value = props.target.display_name;
  renDlg.value = true;
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
.sh-h { display: flex; align-items: center; justify-content: space-between; padding: 0; margin-bottom: 10px; font-size: 13px; font-weight: 600; }
.sh-h .x { font-size: 12px; color: var(--text-3); font-weight: 400; padding: 4px 8px; }
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
.frow { display: flex; flex-direction: column; gap: 4px; margin: 8px 0 0; }
.frow input { padding: 9px 11px; border-radius: 8px; border: 1px solid var(--line-strong); background: var(--bg-inset); color: var(--text-1); font-size: 13px; outline: none; font-family: inherit; }
.btnrow { display: flex; gap: 8px; margin: 16px 0 0; }
.btnrow button { flex: 1; padding: 10px; border-radius: 8px; font-size: 13px; }
.btn-g { border: 1px solid var(--line-strong); background: none; color: var(--text-2); }
.btn-p { border: none; background: var(--accent); color: var(--accent-text); font-weight: 600; }
</style>

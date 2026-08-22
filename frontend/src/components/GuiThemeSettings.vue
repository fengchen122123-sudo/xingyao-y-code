<template>
  <div data-test="gui-theme-settings" class="gui-theme-settings">
    <aside class="theme-list">
      <div class="list-title">主题库</div>
      <div v-if="store.guiThemeListError" data-test="theme-list-error" class="theme-list-error">
        主题列表读取失败：{{ store.guiThemeListError.message }}
      </div>
      <button
        v-for="theme in store.guiThemes"
        :key="theme.id"
        :data-test="`theme-${theme.id.replaceAll('.', '-')}`"
        class="theme-item"
        :class="{ active: theme.id === activeThemeId, builtin: theme.builtin }"
        :disabled="busy"
        :title="theme.builtin ? '内置主题：只能启用或复制' : theme.id"
        @click="selectTheme(theme)"
      >
        <span class="theme-item-name">{{ theme.name }}</span>
        <span v-if="theme.id === activeThemeId" class="theme-item-tag">使用中</span>
        <span v-else-if="theme.builtin" class="theme-item-tag">内置</span>
      </button>
      <button
        data-test="import-theme"
        class="theme-import"
        :disabled="busy"
        type="button"
        @click="importThemeZip"
      >导入主题 ZIP…</button>
      <p class="list-hint">内置主题只能启用或复制；用户主题支持编辑、重命名、删除和导出。</p>
    </aside>

    <section v-if="draft" class="theme-editor">
      <header class="theme-editor-head">
        <div class="theme-id-row">
          <label class="head-label">主题名称</label>
          <input
            v-model="draft.name"
            data-test="theme-name"
            class="theme-name-input"
            type="text"
            :disabled="isBuiltin || busy"
            spellcheck="false"
          />
          <span class="theme-id mono">{{ draft.id }}</span>
        </div>
        <div class="theme-actions">
          <template v-if="isBuiltin">
            <span class="builtin-hint">内置主题为只读</span>
            <button
              data-test="copy-theme"
              class="primary-button"
              type="button"
              :disabled="busy"
              @click="openCopyDialog"
            >复制为自定义主题</button>
          </template>
          <template v-else>
            <button data-test="rename-theme" type="button" :disabled="busy" @click="openRenameDialog">重命名</button>
            <button data-test="export-theme" type="button" :disabled="busy" @click="exportTheme">导出 ZIP</button>
            <button data-test="delete-theme" type="button" :disabled="busy" class="danger" @click="deleteActiveTheme">删除</button>
            <button data-test="save-theme" class="primary-button" type="button" :disabled="busy || !fieldsValid" @click="saveTheme">保存</button>
            <button data-test="cancel-theme" type="button" :disabled="busy" @click="cancelEdits">取消</button>
          </template>
          <button data-test="toggle-preview" type="button" :disabled="busy" @click="togglePreview">
            预览效果
          </button>
        </div>
      </header>

      <div v-if="actionError" data-test="theme-action-error" class="theme-action-error">{{ actionError }}</div>

      <nav class="region-tabs">
        <button
          data-test="region-global"
          type="button"
          :class="{ active: selectedRegion === 'global' }"
          @click="selectedRegion = 'global'"
        >全局颜色</button>
        <button
          v-for="region in REGION_ENTRIES"
          :key="region.id"
          :data-test="`region-${region.id}`"
          type="button"
          :class="{ active: selectedRegion === region.id }"
          @click="selectedRegion = region.id"
        >{{ region.label }}</button>
        <button
          data-test="region-native-titlebar"
          type="button"
          :class="{ active: selectedRegion === 'native_titlebar' }"
          @click="selectedRegion = 'native_titlebar'"
        >系统标题栏</button>
      </nav>

      <ThemeRegionEditor
        v-if="selectedRegion === 'global'"
        :key="`global:${draft.id}`"
        mode="global"
        :model-value="draft.tokens"
        :app-image="draft.app_image"
        :disabled="isBuiltin"
        @update:model-value="onTokensChange"
        @update:app-image="onAppImageChange"
        @choose-image="chooseImage"
        @remove-image="removeImage"
        @clear-overrides="clearGlobalInherit"
        @validity="fieldsValid = $event"
      />
      <ThemeRegionEditor
        v-else-if="selectedRegion === 'native_titlebar'"
        :key="`native:${draft.id}`"
        mode="native"
        :model-value="draft.native_titlebar"
        :disabled="isBuiltin"
        @update:model-value="onNativeChange"
        @validity="fieldsValid = $event"
      />
      <ThemeRegionEditor
        v-else
        :key="`region:${draft.id}:${selectedRegion}`"
        mode="region"
        :model-value="draft.regions[selectedRegion as ThemeRegionId]"
        :disabled="isBuiltin"
        @update:model-value="onRegionChange"
        @choose-image="chooseImage"
        @remove-image="removeImage"
        @clear-overrides="clearRegionInherit"
        @validity="fieldsValid = $event"
      />
    </section>

    <!-- 复制内置主题对话框 -->
    <div v-if="copyDialog" class="modal-mask" @click.self="copyDialog = null">
      <form data-test="copy-modal" class="theme-modal" @submit.prevent="confirmCopy">
        <div class="modal-head"><h3>复制为自定义主题</h3><button type="button" @click="copyDialog = null">×</button></div>
        <label class="head-label">新主题 ID（小写字母、数字、连字符）</label>
        <input v-model="copyDialog.newId" data-test="copy-id" class="text-field mono" type="text" spellcheck="false" />
        <label class="head-label">主题名称</label>
        <input v-model="copyDialog.newName" data-test="copy-name" class="text-field" type="text" />
        <div class="modal-actions">
          <button type="button" class="secondary-button" @click="copyDialog = null">取消</button>
          <button type="button" data-test="confirm-copy" class="primary-button" :disabled="busy" @click="confirmCopy">复制并启用</button>
        </div>
      </form>
    </div>

    <!-- 重命名对话框 -->
    <div v-if="renameDialog" class="modal-mask" @click.self="renameDialog = null">
      <form data-test="rename-modal" class="theme-modal" @submit.prevent="confirmRename">
        <div class="modal-head"><h3>重命名主题</h3><button type="button" @click="renameDialog = null">×</button></div>
        <label class="head-label">新主题 ID（小写字母、数字、连字符）</label>
        <input v-model="renameDialog.newId" data-test="rename-id" class="text-field mono" type="text" spellcheck="false" />
        <label class="head-label">主题名称</label>
        <input v-model="renameDialog.newName" data-test="rename-name" class="text-field" type="text" />
        <div class="modal-actions">
          <button type="button" class="secondary-button" @click="renameDialog = null">取消</button>
          <button type="button" data-test="confirm-rename" class="primary-button" :disabled="busy" @click="confirmRename">重命名</button>
        </div>
      </form>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, shallowRef } from 'vue';
import ThemeRegionEditor from './theme/ThemeRegionEditor.vue';
import { useAppStore } from '../stores/appStore';
import type {
  GuiTheme,
  GuiThemeSummary,
  NativeTitlebarTheme,
  ThemeRegion,
  ThemeRegionId,
} from '../themes/types';

const REGION_ENTRIES: Array<{ id: ThemeRegionId; label: string }> = [
  { id: 'app_header', label: '顶部应用栏' },
  { id: 'sidebar', label: '左侧栏' },
  { id: 'conversation_toolbar', label: '会话工具栏' },
  { id: 'workspace', label: '主工作区' },
  { id: 'composer', label: '输入区' },
  { id: 'right_panel', label: '右侧面板' },
  { id: 'statusbar', label: '状态栏' },
];

const THEME_ID_PATTERN = /^[a-z0-9][a-z0-9-]{0,63}$/;

const store = useAppStore();

// 编辑器本地草稿：所有字段编辑先落在这里；预览/保存把草稿快照送进 store。
// Pinia state 是 reactive 代理，structuredClone 会抛错，克隆一律走 JSON。
function clone<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T;
}

const activeThemeId = computed(() => store.savedGuiTheme?.id ?? '');
const isBuiltin = computed(() => draft.value?.builtin === true);
const hasLocalChanges = computed(() => {
  if (!draft.value || !store.savedGuiTheme) return false;
  return JSON.stringify(draft.value) !== JSON.stringify(store.savedGuiTheme);
});

// 草稿在 setup 阶段同步初始化，保证设置页打开时编辑器首帧可见。
const initialDraft = store.guiThemeDraft ?? store.savedGuiTheme;
const draft = ref<GuiTheme | null>(initialDraft ? clone(initialDraft) : null);

const selectedRegion = ref<'global' | ThemeRegionId | 'native_titlebar'>('workspace');
const fieldsValid = ref(true);
const busy = ref(false);
const actionError = ref('');
const copyDialog = ref<{ newId: string; newName: string } | null>(null);
const renameDialog = ref<{ newId: string; newName: string } | null>(null);

function syncDraftFromStore() {
  const source = store.guiThemeDraft ?? store.savedGuiTheme;
  draft.value = source ? clone(source) : null;
}

// ── 即时预览合并：同一时刻最多一个预览请求在执行；执行期间的最新草稿
//    挂起，当前预览结束后补发一次。保存/取消走显式草稿参数，不依赖这里。──
const previewPending = shallowRef<GuiTheme | null>(null);
const previewRunning = ref(false);
let disposed = false;

function schedulePreview() {
  if (!draft.value || disposed) return;
  const snapshot = clone(draft.value);
  if (previewRunning.value) {
    previewPending.value = snapshot;
    return;
  }
  void runPreview(snapshot);
}

async function runPreview(snapshot: GuiTheme) {
  previewRunning.value = true;
  try {
    const result = await store.previewGuiTheme(snapshot);
    if (disposed) return;
    actionError.value = result.ok ? '' : result.error.message;
  } finally {
    previewRunning.value = false;
    if (disposed) return;
    const pending = previewPending.value;
    previewPending.value = null;
    if (pending) void runPreview(pending);
  }
}

onMounted(() => {
  if (!store.guiThemes.length && window.pywebview?.api) void store.refreshGuiThemes();
});

onUnmounted(() => {
  disposed = true;
});

// ── 预览：收起设置遮罩，直接看真实首页（草稿已实时渲染到真实 DOM）。
//    顶部浮条由 App.vue 在 themePreviewMode 时显示，提供返回编辑。──
function togglePreview() {
  if (!draft.value) return;
  store.themePreviewMode = true;
  store.settingsOpen = false;
}

// 设置页用 v-show 常驻本组件，切换分区不丢草稿；关闭设置页前用它做脏检查。
defineExpose({
  isDirty: () => hasLocalChanges.value || store.themeDirty,
});

// ── 字段编辑（编辑器 emit 的模型是三种形态的联合，按当前分区收窄） ──
type EditorModel = ThemeRegion | Record<string, string | number> | NativeTitlebarTheme;

function onRegionChange(value: EditorModel) {
  if (!draft.value) return;
  draft.value.regions[selectedRegion.value as ThemeRegionId] = value as ThemeRegion;
  schedulePreview();
}

function onTokensChange(value: EditorModel) {
  if (!draft.value) return;
  draft.value.tokens = value as Record<string, string | number>;
  schedulePreview();
}

function onNativeChange(value: EditorModel) {
  if (!draft.value) return;
  draft.value.native_titlebar = value as NativeTitlebarTheme;
  schedulePreview();
}

function onAppImageChange(value: GuiTheme['app_image']) {
  if (!draft.value) return;
  draft.value.app_image = value;
  schedulePreview();
}

// ── 主题切换（含脏编辑守卫：保存 / 放弃 / 留在当前） ──
async function selectTheme(summary: GuiThemeSummary) {
  if (busy.value || summary.id === activeThemeId.value) return;
  store.themePreviewMode = false;
  busy.value = true;
  try {
    if (hasLocalChanges.value) {
      const saveFirst = await store.askConfirm(
        '未保存的主题修改', '', '当前主题有未保存的修改，是否先保存？', '保存修改',
      );
      if (saveFirst) {
        if (!fieldsValid.value) {
          actionError.value = '存在无效字段，请修正后再保存';
          return;
        }
        const saved = await store.saveGuiThemeDraft(clone(draft.value!));
        if (!saved.ok) {
          actionError.value = saved.error.message;
          return;
        }
      } else {
        const discard = await store.askConfirm(
          '未保存的主题修改', '', `放弃未保存的修改并切换到「${summary.name}」？`, '放弃并切换',
        );
        if (!discard) return;
      }
    }
    const result = await store.activateGuiTheme(summary.id);
    if (result.ok) {
      actionError.value = '';
      syncDraftFromStore();
    } else {
      actionError.value = result.error.message;
    }
  } finally {
    busy.value = false;
  }
}

// ── 保存 / 取消 ──
async function saveTheme() {
  if (busy.value || !draft.value || !fieldsValid.value) return;
  store.themePreviewMode = false;
  busy.value = true;
  const snapshot = clone(draft.value);
  try {
    // 显式传最终草稿：预览排队未消化时也不丢快速输入的最后值。
    const result = await store.saveGuiThemeDraft(clone(snapshot));
    if (result.ok) {
      actionError.value = '';
      // 保存飞行中用户又编辑时保留草稿增量（基于已保存快照，可再保存）；
      // 只有飞行中无新编辑才整体采纳服务器返回的规范化主题。
      if (!draft.value) return;
      if (JSON.stringify(draft.value) === JSON.stringify(snapshot)) {
        draft.value = clone(result.theme);
      } else {
        draft.value.id = result.theme.id;
        draft.value.name = result.theme.name;
      }
    } else {
      actionError.value = result.error.message;
    }
  } finally {
    busy.value = false;
  }
}

async function cancelEdits() {
  if (busy.value) return;
  store.themePreviewMode = false;
  busy.value = true;
  try {
    const result = await store.cancelGuiThemePreview();
    if (result.ok) {
      actionError.value = '';
      draft.value = clone(result.theme ?? store.guiThemeDraft ?? store.savedGuiTheme);
    } else {
      actionError.value = result.error.message;
    }
  } finally {
    busy.value = false;
  }
}

// ── 复制内置主题 ──
function suggestCopyId(baseId: string): string {
  const stem = baseId.replace(/builtin\./g, '').replace(/[^a-z0-9-]/g, '').slice(0, 40) || 'theme';
  const taken = new Set(store.guiThemes.map(theme => theme.id));
  let candidate = `${stem}-copy`;
  let suffix = 2;
  while (taken.has(candidate)) candidate = `${stem}-copy-${suffix++}`;
  return candidate;
}

function openCopyDialog() {
  const source = store.savedGuiTheme;
  if (!source) return;
  copyDialog.value = {
    newId: suggestCopyId(source.id),
    newName: `${source.name} 副本`,
  };
}

async function confirmCopy() {
  const dialog = copyDialog.value;
  if (!dialog || busy.value || !store.savedGuiTheme) return;
  if (!THEME_ID_PATTERN.test(dialog.newId)) {
    actionError.value = '主题 ID 只能包含小写字母、数字和连字符';
    return;
  }
  if (!dialog.newName.trim()) {
    actionError.value = '主题名称不能为空';
    return;
  }
  busy.value = true;
  try {
    const result = await store.copyGuiTheme(
      store.savedGuiTheme.id, dialog.newId, dialog.newName.trim(),
    );
    if (!result.ok) {
      actionError.value = result.error.message;
      return;
    }
    copyDialog.value = null;
    actionError.value = '';
    const activated = await store.activateGuiTheme(result.theme.id);
    if (activated.ok) syncDraftFromStore();
    else actionError.value = activated.error.message;
  } finally {
    busy.value = false;
  }
}

// ── 重命名（活动用户主题） ──
function openRenameDialog() {
  if (!draft.value) return;
  renameDialog.value = { newId: draft.value.id, newName: draft.value.name };
}

async function confirmRename() {
  const dialog = renameDialog.value;
  if (!dialog || busy.value || !draft.value) return;
  if (!THEME_ID_PATTERN.test(dialog.newId)) {
    actionError.value = '主题 ID 只能包含小写字母、数字和连字符';
    return;
  }
  if (!dialog.newName.trim()) {
    actionError.value = '主题名称不能为空';
    return;
  }
  busy.value = true;
  try {
    const result = await store.renameGuiTheme(draft.value.id, dialog.newId, dialog.newName.trim());
    if (!result.ok) {
      actionError.value = result.error.message;
      return;
    }
    renameDialog.value = null;
    actionError.value = '';
    // store 已同步活动主题快照的 id/name；本地草稿只补 id/name，保留其余未保存编辑。
    if (draft.value) {
      draft.value.id = result.theme.id;
      draft.value.name = result.theme.name;
    }
  } finally {
    busy.value = false;
  }
}

// ── 删除 / 导入 / 导出 ──
async function deleteActiveTheme() {
  if (!draft.value || busy.value) return;
  const confirmed = await store.askConfirm(
    '删除主题', '', `确定要删除主题「${draft.value.name}」吗？此操作不可恢复。`,
  );
  if (!confirmed) return;
  busy.value = true;
  try {
    const result = await store.deleteGuiTheme(draft.value.id);
    if (result.ok) actionError.value = '';
    else actionError.value = result.error.message;
  } finally {
    busy.value = false;
  }
}

async function importThemeZip() {
  if (busy.value) return;
  busy.value = true;
  try {
    const result = await store.importGuiTheme();
    if (result.ok) {
      const activated = await store.activateGuiTheme(result.theme.id);
      if (activated.ok) syncDraftFromStore();
      else actionError.value = activated.error.message;
    } else if (result.error.code !== 'dialog_cancelled') {
      actionError.value = result.error.message;
    }
  } finally {
    busy.value = false;
  }
}

async function exportTheme() {
  if (!draft.value || busy.value) return;
  busy.value = true;
  try {
    const result = await store.exportGuiTheme(draft.value.id);
    if (result.ok) store.notifyModal('导出成功', `主题已导出到：${result.path}`);
    else if (result.error.code !== 'dialog_cancelled') actionError.value = result.error.message;
  } finally {
    busy.value = false;
  }
}

// ── 分区/全局背景图 ──
function imageScope(): string {
  return selectedRegion.value === 'global' ? 'app' : (selectedRegion.value as string);
}

async function chooseImage() {
  if (!draft.value || busy.value) return;
  const scope = imageScope();
  busy.value = true;
  try {
    const result = await store.importGuiThemeImage(draft.value.id, scope as ThemeRegionId);
    if (result.ok) {
      if (scope === 'app') draft.value.app_image.image = result.asset_path;
      else draft.value.regions[scope as ThemeRegionId].image = result.asset_path;
      actionError.value = '';
      schedulePreview();
    } else if (result.error.code !== 'dialog_cancelled') {
      actionError.value = result.error.message;
    }
  } finally {
    busy.value = false;
  }
}

async function removeImage() {
  if (!draft.value || busy.value) return;
  const scope = imageScope();
  busy.value = true;
  try {
    const result = await store.removeGuiThemeImage(draft.value.id, scope as ThemeRegionId);
    if (result.ok) {
      // 只清图片引用，保留该分区/全局其他未保存编辑；完整草稿随保存写回。
      if (scope === 'app') draft.value.app_image.image = null;
      else draft.value.regions[scope as ThemeRegionId].image = null;
      actionError.value = '';
      schedulePreview();
    } else {
      actionError.value = result.error.message;
    }
  } finally {
    busy.value = false;
  }
}

// ── 恢复继承值：把分区/全局令牌重置为基础内置主题的当前值 ──
const baseThemeCache = new Map<string, GuiTheme>();

async function loadBaseTheme(): Promise<GuiTheme | null> {
  if (!draft.value) return null;
  const extendsId = draft.value.extends;
  const cached = baseThemeCache.get(extendsId);
  if (cached) return cached;
  const result = await store.loadGuiTheme(extendsId);
  if (!result.ok) {
    actionError.value = result.error.message;
    return null;
  }
  baseThemeCache.set(extendsId, clone(result.theme));
  return result.theme;
}

async function clearRegionInherit() {
  if (!draft.value || busy.value) return;
  const regionId = selectedRegion.value as ThemeRegionId;
  busy.value = true;
  try {
    const base = await loadBaseTheme();
    if (base) {
      draft.value.regions[regionId] = clone(base.regions[regionId]);
      actionError.value = '';
      schedulePreview();
    }
  } finally {
    busy.value = false;
  }
}

async function clearGlobalInherit() {
  if (!draft.value || busy.value) return;
  busy.value = true;
  try {
    const base = await loadBaseTheme();
    if (base) {
      draft.value.tokens = clone(base.tokens);
      actionError.value = '';
      schedulePreview();
    }
  } finally {
    busy.value = false;
  }
}
</script>

<style scoped>
.gui-theme-settings { display: grid; grid-template-columns: 250px minmax(0, 1fr); gap: 20px; align-items: start; }
.theme-list { border: 1px solid var(--border); border-radius: 14px; background: var(--bg1); padding: 14px; display: flex; flex-direction: column; gap: 6px; }
.list-title { font-size: 12px; font-weight: 600; color: var(--text-faint); padding: 2px 6px 8px; }
.theme-list-error { padding: 9px 11px; border: 1px solid var(--border); border-radius: 9px; background: var(--bg2); color: var(--red); font-size: 11.5px; line-height: 1.5; margin-bottom: 6px; }
.theme-item { width: 100%; display: flex; align-items: center; gap: 8px; padding: 9px 11px; border: 1px solid transparent; border-radius: 9px; background: transparent; color: var(--text-dim); text-align: left; font-size: 13px; }
.theme-item:hover { background: var(--bg2); color: var(--text); }
.theme-item.active { background: var(--bg3); color: var(--text); border-color: var(--border); }
.theme-item-name { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.theme-item-tag { font-size: 10.5px; color: var(--text-faint); border: 1px solid var(--border); border-radius: 6px; padding: 1px 6px; }
.theme-item.builtin .theme-item-tag { color: var(--accent); border-color: color-mix(in srgb, var(--accent) 40%, var(--border)); }
.theme-import { margin-top: 8px; padding: 9px; border: 1px dashed var(--border); border-radius: 9px; background: transparent; color: var(--text-dim); font-size: 12.5px; }
.theme-import:hover { border-color: var(--accent); color: var(--accent); }
.list-hint { margin: 10px 4px 0; font-size: 11px; color: var(--text-faint); line-height: 1.6; }
.theme-editor { border: 1px solid var(--border); border-radius: 14px; background: var(--bg1); padding: 20px 22px; }
.theme-editor-head { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 14px; }
.theme-id-row { display: flex; align-items: center; gap: 10px; }
.head-label { font-size: 12px; color: var(--text-dim); }
.theme-name-input { min-height: 36px; width: 200px; padding: 0 12px; border: 1px solid var(--border); border-radius: 9px; background: var(--bg2); color: var(--text); font-size: 13px; }
.theme-id { font-size: 11px; color: var(--text-faint); }
.theme-actions { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.theme-actions button { min-height: 32px; padding: 0 12px; border: 1px solid var(--border); border-radius: 8px; background: var(--bg3); color: var(--text); font-size: 12px; }
.theme-actions button.danger { color: var(--red); }
.theme-actions button.primary-button { background: var(--accent); border-color: var(--accent); color: #ffffff; font-weight: 600; }
.theme-actions button:disabled { opacity: 0.5; cursor: not-allowed; }
.builtin-hint { font-size: 11.5px; color: var(--text-faint); }
.theme-action-error { margin-top: 14px; padding: 10px 12px; border: 1px solid color-mix(in srgb, var(--red) 45%, var(--border)); border-radius: 9px; background: color-mix(in srgb, var(--red) 8%, var(--bg2)); color: var(--red); font-size: 12px; }
.region-tabs { display: flex; flex-wrap: wrap; gap: 7px; margin: 18px 0 16px; }
.region-tabs button { padding: 7px 12px; border: 1px solid var(--border); border-radius: 9px; background: transparent; color: var(--text-dim); font-size: 12px; }
.region-tabs button:hover { color: var(--text); background: var(--bg2); }
.region-tabs button.active { background: var(--bg3); color: var(--text); border-color: var(--accent); }
.modal-mask { position: fixed; inset: 0; z-index: 5000; display: grid; place-items: center; background: rgba(0, 0, 0, .68); backdrop-filter: blur(8px); }
.theme-modal { width: min(460px, calc(100vw - 40px)); padding: 22px; box-sizing: border-box; border: 1px solid var(--border); border-radius: 15px; background: var(--bg2); box-shadow: 0 24px 80px rgba(0, 0, 0, .55); display: flex; flex-direction: column; gap: 8px; }
.modal-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 8px; }
.modal-head h3 { margin: 0; font-size: 15px; }
.modal-head button { border: 0; background: transparent; color: var(--text-dim); font-size: 20px; }
.text-field { min-height: 38px; padding: 0 12px; border: 1px solid var(--border); border-radius: 9px; background: var(--bg2); color: var(--text); font-size: 13px; }
.modal-actions { display: flex; justify-content: flex-end; gap: 10px; margin-top: 14px; }
.modal-actions .secondary-button { border: 1px solid var(--border); background: var(--bg3); color: var(--text); border-radius: 8px; min-height: 34px; padding: 0 14px; }
.modal-actions .primary-button { border: 1px solid var(--accent); background: var(--accent); color: #ffffff; border-radius: 8px; min-height: 34px; padding: 0 14px; font-weight: 600; }
.mono { font-family: var(--mono); }
@media (max-width: 1100px) {
  .gui-theme-settings { grid-template-columns: 1fr; }
}
</style>

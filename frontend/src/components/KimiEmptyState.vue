<template>
  <div class="kimi-empty-container">
    <div class="kimi-hero">
      <div class="kimi-title-row">
        <span class="kimi-title">让{{ store.agentName }}帮你完成任务</span>
      </div>
    </div>

    <div class="kimi-card" ref="kimiCardRef" :class="{ 'drop-hover': dragging }">
      <AttachmentChips :items="attachments" @remove="removeAttachment" @open="openAttachmentImage" />
      <!-- / 斜杠命令 与 @ 文件引用的自动补全面板 -->
      <InputSuggest
        :items="suggestItems"
        :active-index="suggestIndex"
        :empty-hint="suggestEmpty"
        @select="applySuggest"
        @hover="i => suggestIndex = i"
      />
      <!-- 富文本输入框：@ 引用与 / 技能渲染成胶囊（带图标、整块选中、退格一次全删） -->
      <div class="kimi-field">
        <ComposerEditor
          ref="editorRef"
          v-model="inputVal"
          :min-height="75"
          :skill-ids="skillIds"
          :command-ids="commandIds"
          placeholder="尽管问，或做个任务… / 技能 @ 文件"
          @keydown="handleKeydown"
          @input="detectToken"
          @click="detectToken"
          @keyup="detectToken"
          @paste="handlePaste"
        />
      </div>

      <div class="kimi-card-bar">
        <div ref="barLeftRef" class="kimi-bar-left">
          <button
            ref="attachBtnRef"
            data-test="attachment-button"
            class="kimi-pill-btn kimi-icon-btn"
            title="添加附件"
            @click.stop="onAttachClick"
          ><AppIcon name="paperclip" :size="14.5" :stroke-width="1.8" /></button>
          <!-- 屏幕截图：全屏框选屏幕任意区域；截图「发到输入框」后后端经
               snip_attachment_ready 桥事件回传路径，由 store 加入输入框附件 -->
          <button
            data-test="screen-snip-button"
            class="kimi-pill-btn kimi-icon-btn snip-pill"
            title="截图：框选屏幕任意区域"
            @click.stop="onScreenSnipClick"
          ><AppIcon name="scissors" :size="14.5" :stroke-width="1.8" /></button>
          <!-- 触摸设备隐藏文件入口：点击后由系统弹出拍照/相册/文件选择 -->
          <input ref="touchFileInputRef" type="file" multiple style="display:none" @change="onPickInputChange">
          <span class="composer-sep" aria-hidden="true"></span>
          <button
            ref="permBtnRef"
            data-test="permission-button"
            class="kimi-pill-btn permission-mode-btn"
            :class="`perm-${store.permissionMode}`"
            title="切换权限模式"
            @click.stop="togglePermDropdown"
          ><span class="perm-dot" aria-hidden="true"></span><AppIcon class="perm-icon" name="shield" :size="13" :stroke-width="2" /><span class="perm-label">{{ formatPermMode(store.permissionMode) }}</span></button>
          <!-- 沙盒限制开关（绿框位）：与输入框沙盒 chip 同一后端配置 -->
          <button
            data-test="hero-sandbox-button"
            class="kimi-pill-btn sandbox-pill"
            :class="{ 'sandbox-open': store.sandboxEnabled }"
            :title="store.sandboxEnabled ? '沙盒已解除：允许访问工作目录外文件（点击恢复限制）' : '沙盒已开启：仅允许访问工作目录内文件（点击解除限制）'"
            @click.stop="store.setSandbox(!store.sandboxEnabled)"
          ><span class="sb-dot" aria-hidden="true"></span><span class="sandbox-label-full">{{ store.sandboxEnabled ? '沙盒已解除' : '沙盒限制中' }}</span><span class="sandbox-label-short">{{ store.sandboxEnabled ? '开' : '限' }}</span></button>
        </div>

        <div ref="barRightRef" class="kimi-bar-right">
          <button
            ref="modelBtnRef"
            data-test="model-button"
            class="kimi-pill-btn model-chip"
            title="切换模型"
            @click.stop="toggleModelCascader"
          ><AppIcon class="model-icon" name="box" :size="13" :stroke-width="2" /><span class="model-label">{{ store.activeModelName || '选择模型' }}</span><span class="model-chevron" aria-hidden="true">▾</span></button>

          <ReasoningSelector
            ref="reasoningSelectorRef"
            variant="hero"
            @open="closeComposerMenus"
          />

          <button
            data-test="send-button"
            class="kimi-send-btn"
            :class="{ active: (inputVal.trim() || attachments.length) && !store.currentSessionRunning }"
            :disabled="store.currentSessionRunning"
            title="发送消息 (Enter)"
            @click="handleSend"
          ><AppIcon name="arrow-up" :size="14" :stroke-width="2.2" /></button>
        </div>
      </div>
    </div>

    <div
      v-if="!isSecretarySession"
      ref="workdirBtnRef"
      class="kimi-footer-workdir"
      @click.stop="toggleWorkdirDropdown"
    >
      <AppIcon name="folder" :size="13" :stroke-width="1.8" />
      <span class="workdir-label">{{ store.currentWorkdirLabel === 'GLOBAL' || store.currentWorkdirLabel === '对话' ? '进入项目工作区' : '当前工作区：' + store.currentWorkdirLabel }}</span>
      <span class="model-chevron" aria-hidden="true">▾</span>
    </div>

    <!-- 秘书会话快捷起手建议（跨项目分析、任务进度、个人记忆） -->
    <div
      v-if="isSecretarySession"
      class="kimi-project-prompts"
      data-test="secretary-prompt-suggestions"
    >
      <button
        v-for="item in secretaryPromptSuggestions"
        :key="item.id"
        type="button"
        class="kimi-prompt-chip"
        data-test="secretary-prompt-chip"
        :title="item.prompt"
        @click="applyProjectPrompt(item)"
      >{{ item.label }}</button>
    </div>

    <!-- 项目感知推荐提示词：仅在非秘书、绑定项目工作区且存在建议时出现，自由对话保持留白 -->
    <div
      v-if="!isSecretarySession && projectPromptSuggestions.length"
      class="kimi-project-prompts"
      data-test="project-prompt-suggestions"
    >
      <button
        v-for="item in projectPromptSuggestions"
        :key="item.id"
        type="button"
        class="kimi-prompt-chip"
        data-test="project-prompt-chip"
        :title="item.prompt"
        @click="applyProjectPrompt(item)"
      >{{ item.label }}</button>
    </div>

    <!-- 工作区切换下拉：可退出工作区回自由对话 / 快捷切换项目 / 引入新项目 -->
    <div v-if="showWorkdirDropdown" class="workdir-dropdown" :style="workdirDropdownStyle" @click.stop>
      <div
        v-if="!isGlobalWorkdir"
        class="workdir-item"
        @click="chooseGlobal"
      >
        <span class="wd-label">💬 自由对话（退出工作区）</span>
      </div>
      <div
        v-for="g in projectGroups"
        :key="g.folder_path"
        class="workdir-item"
        :class="{ active: g.folder_path === store.workdir }"
        @click="chooseProject(g.folder_path)"
      >
        <span class="wd-label"><AppIcon name="folder" :size="13" /> {{ g.folder_name }}</span>
        <span v-if="g.folder_path === store.workdir" class="wd-check"><AppIcon name="check" :size="13" :stroke-width="2" /></span>
      </div>
      <div class="workdir-sep"></div>
      <div class="workdir-item" @click="chooseOpenFolder">
        <span class="wd-label"><AppIcon name="plus" :size="13" /> 进入项目工作区…</span>
      </div>
    </div>

    <!-- 下拉弹出节点 -->
    <ModelCascader 
      :visible="showModelCascader" 
      :anchor-el="modelBtnRef" 
      @close="showModelCascader = false" 
    />
    <PermDropdown 
      :visible="showPermDropdown" 
      :anchor-el="permBtnRef" 
      @close="showPermDropdown = false" 
    />

    <!-- 触摸设备不再使用应用内附件菜单：点回形针直接唤起系统文件选择器 -->
  </div>
</template>

<script setup lang="ts">
import { ref, computed, watch, nextTick, onMounted, onUnmounted } from 'vue';
import { useAppStore } from '../stores/appStore';
import { useGitStore } from '../stores/gitStore';
import type { WorkspacePromptSuggestion } from '../types/pywebview';
import { formatPermMode } from '../utils/markdown';
import AppIcon from './AppIcon.vue';
import ModelCascader from './ModelCascader.vue';
import PermDropdown from './PermDropdown.vue';
import ReasoningSelector from './ReasoningSelector.vue';
import AttachmentChips from './AttachmentChips.vue';
import InputSuggest from './InputSuggest.vue';
import ComposerEditor from './ComposerEditor.vue';
import { useAttachments } from '../composables/useAttachments';
import { useIsTouch } from '../composables/useIsTouch';
import { useSuggest } from '../composables/useSuggest';

const store = useAppStore();
const gitStore = useGitStore();
const inputVal = computed<string>({
  get: () => store._getSessionRuntime().composerText || '',
  set: value => { store._getSessionRuntime().composerText = value; },
});
// 富文本输入框：@ 引用渲染成胶囊
const editorRef = ref<InstanceType<typeof ComposerEditor> | null>(null);
// 补全用的鸭子类型控件（与 textarea 同名成员，useSuggest 无需改动）
const composerControl = computed(() => editorRef.value?.control ?? null);

const showModelCascader = ref(false);
const showPermDropdown = ref(false);
const showWorkdirDropdown = ref(false);
const workdirDropdownStyle = ref<Record<string, string>>({});

const modelBtnRef = ref<HTMLElement | null>(null);
const permBtnRef = ref<HTMLElement | null>(null);
const workdirBtnRef = ref<HTMLElement | null>(null);
const reasoningSelectorRef = ref<{ close: () => void } | null>(null);

// 工作区下拉数据：非 GLOBAL 的项目分组（秘书分组不是工作区，选它会切到
// folder_path='SECRETARY' 这个并不存在的"目录"，故一并过滤）
const projectGroups = computed(() =>
  store.sessionGroups.filter(g => !g.is_global && !g.is_secretary && g.folder_path && g.folder_path !== 'GLOBAL')
);
const isGlobalWorkdir = computed(() =>
  !store.workdir || store.currentWorkdirLabel === '对话' || store.currentWorkdirLabel === 'GLOBAL'
);

const isSecretarySession = computed(() =>
  Boolean((store as { currentSessionIsSecretary?: boolean }).currentSessionIsSecretary),
);
const secretaryPromptSuggestions = [
  {
    id: 'sec-analyze-history',
    label: '分析近期历史会话与进展',
    prompt: '帮我盘点所有项目最近的活跃历史会话，汇总已完成事项、遗留问题和下一步建议。',
  },
  {
    id: 'sec-list-dispatches',
    label: '查看近期派发任务状态',
    prompt: '查询最近派出去的任务（list_dispatches）现在的状态、完成结论或需要我拍板的事项。',
  },
  {
    id: 'sec-manage-memory',
    label: '查看或更新个人偏好记忆',
    prompt: '查看你当前记下的跨项目个人偏好记忆，看看有哪些信息需要补充或更新。',
  },
];
const projectPromptSuggestions = computed(() =>
  isGlobalWorkdir.value || isSecretarySession.value ? [] : store.workspacePromptSuggestions,
);

function normalizeDirKey(path: string): string {
  return String(path || '').replace(/\//g, '\\').replace(/\\+$/, '').toLowerCase();
}

watch(
  [
    () => store.workdir,
    () => isGlobalWorkdir.value,
    () => gitStore.workdir,
    () => gitStore.snapshot?.branch_name,
    () => gitStore.snapshot?.staged.length ?? 0,
    () => gitStore.snapshot?.unstaged.length ?? 0,
    () => gitStore.snapshot?.untracked.length ?? 0,
    () => gitStore.snapshot?.conflicts.length ?? 0,
    () => gitStore.snapshot?.ahead ?? 0,
  ],
  () => {
    if (isGlobalWorkdir.value || !store.workdir) {
      void store.loadWorkspacePromptSuggestions('GLOBAL');
      return;
    }
    const matchingSnapshot =
      gitStore.snapshot && normalizeDirKey(gitStore.workdir) === normalizeDirKey(store.workdir)
        ? gitStore.snapshot
        : null;
    void store.loadWorkspacePromptSuggestions(store.workdir, matchingSnapshot);
  },
  { immediate: true },
);

function applyProjectPrompt(item: WorkspacePromptSuggestion) {
  inputVal.value = item.prompt;
  void nextTick(() => {
    const ctrl = composerControl.value;
    if (ctrl) {
      ctrl.focus();
      ctrl.setSelectionRange(item.prompt.length, item.prompt.length);
    }
  });
}

function toggleWorkdirDropdown() {
  reasoningSelectorRef.value?.close();
  showWorkdirDropdown.value = !showWorkdirDropdown.value;
  if (showWorkdirDropdown.value && workdirBtnRef.value) {
    const r = workdirBtnRef.value.getBoundingClientRect();
    workdirDropdownStyle.value = {
      position: 'fixed',
      bottom: `${window.innerHeight - r.top + 6}px`,
      left: `${r.left}px`,
    };
  }
}

function chooseGlobal() {
  showWorkdirDropdown.value = false;
  store.exitWorkspaceToGlobal();
}

function chooseProject(folderPath: string) {
  showWorkdirDropdown.value = false;
  if (folderPath !== store.workdir) {
    store.switchToWorkspaceGroup(folderPath);
  }
}

function chooseOpenFolder() {
  showWorkdirDropdown.value = false;
  store.openWorkspaceFolder();
}

function onGlobalClickForWorkdir(e: MouseEvent) {
  if (showWorkdirDropdown.value && workdirBtnRef.value && !workdirBtnRef.value.contains(e.target as Node)) {
    showWorkdirDropdown.value = false;
  }
}
onMounted(() => document.addEventListener('click', onGlobalClickForWorkdir));
onUnmounted(() => document.removeEventListener('click', onGlobalClickForWorkdir));

function toggleModelCascader() {
  reasoningSelectorRef.value?.close();
  showPermDropdown.value = false;
  showModelCascader.value = !showModelCascader.value;
}

function togglePermDropdown() {
  reasoningSelectorRef.value?.close();
  showModelCascader.value = false;
  showPermDropdown.value = !showPermDropdown.value;
}

function closeComposerMenus() {
  showModelCascader.value = false;
  showPermDropdown.value = false;
  showWorkdirDropdown.value = false;
}

const { attachments, pickAttachments, removeAttachment, clearAttachments, handlePaste, dragging, bindDrop, uploadFiles, openAttachmentImage } = useAttachments();
const { suggestItems, suggestIndex, suggestEmpty, skillIds, commandIds, detectToken, applySuggest, onKeydown, clearSuggest } = useSuggest(inputVal, composerControl);

// 输入卡片容器：拖拽文件进入时高亮提示，drop 由原生事件桥（pywebview）提供完整路径
const kimiCardRef = ref<HTMLElement | null>(null);
onMounted(() => { if (kimiCardRef.value) bindDrop(kimiCardRef.value); });

// 权限胶囊要不要收成纯图标，改由实测溢出决定：`.kimi-bar-left` 允许收缩到 0 宽
// 并裁掉溢出内容，于是「完整形态需要多宽 / 实际可用多宽」一次量得准。
// 固定容器阈值只能按最宽的模型名估算，正常宽度下也会先把权限文字收掉。
const barLeftRef = ref<HTMLElement | null>(null);
const barRightRef = ref<HTMLElement | null>(null);
let barMeasureFrame = 0;
let barObserver: ResizeObserver | null = null;

function measureBarFit() {
  const el = barLeftRef.value;
  if (!el) return;
  // 收起态量不到真实需求宽度：先摘掉 is-tight 同步量一次，再按结果决定是否戴回
  if (el.classList.contains('is-tight')) el.classList.remove('is-tight');
  if (el.scrollWidth > el.clientWidth + 1) el.classList.add('is-tight');
}

function scheduleBarMeasure() {
  if (barMeasureFrame) cancelAnimationFrame(barMeasureFrame);
  barMeasureFrame = requestAnimationFrame(() => {
    barMeasureFrame = 0;
    measureBarFit();
  });
}

onMounted(() => {
  scheduleBarMeasure();
  if (typeof ResizeObserver === 'undefined') return;
  // 卡片变窄、或右侧模型名变长都会挤占可用宽度，两处都要跟着重量
  barObserver = new ResizeObserver(() => scheduleBarMeasure());
  if (kimiCardRef.value) barObserver.observe(kimiCardRef.value);
  if (barRightRef.value) barObserver.observe(barRightRef.value);
});

onUnmounted(() => {
  barObserver?.disconnect();
  barObserver = null;
  if (barMeasureFrame) cancelAnimationFrame(barMeasureFrame);
});

onUnmounted(() => clearSuggest());

async function handlePickFiles() {
  await pickAttachments();
}

// 触摸设备（手机/平板）：点回形针直接触发隐藏文件入口，由系统弹出拍照/相册/文件选择；
// 桌面仍走系统文件对话框
const isTouch = useIsTouch();
const touchFileInputRef = ref<HTMLInputElement | null>(null);

function onAttachClick() {
  if (!isTouch.value) { handlePickFiles(); return; }
  touchFileInputRef.value?.click();
}

// 屏幕截图：调后端打开全屏框选遮罩窗口；截图结果经桥事件
// snip_attachment_ready 回传（见 appStore._attachSnipToComposer）
async function onScreenSnipClick() {
  const api = window.pywebview?.api;
  if (!api?.screen_snip_start) {
    store.notifyChat('桌面桥未就绪，无法使用截图');
    return;
  }
  try {
    const res = await api.screen_snip_start();
    if (res && !res.ok) store.notifyChat(res.msg || '截图启动失败');
  } catch {
    store.notifyChat('截图启动失败：桥接调用异常');
  }
}

async function onPickInputChange(e: Event) {
  const input = e.target as HTMLInputElement;
  const files = Array.from(input.files || []);
  input.value = '';  // 允许重复选同一文件
  if (files.length) await uploadFiles(files);
}

function handleSend() {
  if (store.currentSessionRunning) return;
  // 补全面板打开时由 onKeydown 拦截，不会走到这里
  const text = inputVal.value;
  // 拖拽的附件还在等路径时不允许发送，避免把占位的假路径发给后端
  if (attachments.value.some(a => a.loading)) {
    store.notifyChat('附件路径加载中，请稍候…');
    return;
  }
  const paths = attachments.value.map(a => a.path);
  if (!text.trim() && !paths.length) return;
  inputVal.value = '';
  clearAttachments();
  store.sendMessage(text, paths);
}

// 独立的 Enter 发送处理：onKeydown 没拦截时（补全未打开）才发送
function handleKeydown(e: KeyboardEvent) {
  onKeydown(e);
  // 补全已处理（导航键/选中/关闭），不需要进一步处理
  if (e.defaultPrevented) return;
  // Enter 发送（补全关闭时）
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    handleSend();
  }
}
</script>

<style scoped>
/* 输入框字体：与改动前的 .kimi-card textarea 保持一致（15px / 1.6） */
.kimi-field {
  font-size: 15px;
  line-height: 1.6;
}
.workdir-dropdown {
  z-index: 999;
  min-width: 220px;
  background: var(--float-bg);
  border: 1px solid var(--border);
  border-radius: 12px;
  box-shadow: 0 16px 40px rgba(0, 0, 0, 0.55);
  padding: 6px;
  backdrop-filter: blur(14px);
  animation: fadein 0.15s cubic-bezier(0.16, 1, 0.3, 1);
}
/* 沙盒开关与权限圆点（对齐极简设计稿 v2） */
.composer-sep {
  width: 1px;
  height: 14px;
  margin: 0 2px;
  background: var(--xy-border, var(--border));
  flex-shrink: 0;
}
.perm-dot,
.sb-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: currentColor;
  flex-shrink: 0;
}
.kimi-icon-btn {
  width: 28px;
  height: 28px;
  padding: 0 !important;
}
.sandbox-pill {
  background: var(--xy-green-soft, color-mix(in srgb, var(--green) 12%, transparent));
  color: var(--xy-green, var(--green));
  font-weight: 500;
}
.sandbox-pill:hover {
  background: color-mix(in srgb, var(--xy-green, var(--green)) 18%, transparent);
  color: var(--xy-green, var(--green));
}
.sandbox-pill.sandbox-open {
  background: var(--xy-amber-soft, color-mix(in srgb, var(--orange) 12%, transparent));
  color: var(--xy-amber, var(--orange));
}
.sandbox-pill.sandbox-open:hover {
  background: color-mix(in srgb, var(--xy-amber, var(--orange)) 18%, transparent);
  color: var(--xy-amber, var(--orange));
}
.workdir-item {
  padding: 9px 12px;
  border-radius: 8px;
  cursor: pointer;
  font-size: 13px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  color: var(--text-dim);
  transition: 0.15s;
  user-select: none;
}
.workdir-item:hover,
.workdir-item.active {
  background: var(--bg3);
  color: var(--text);
}
.workdir-item .wd-label {
  flex: 1;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  display: flex;
  align-items: center;
  gap: 6px;
}
.workdir-item .wd-check {
  color: var(--accent);
  margin-left: 8px;
}
/* 模型/工作区按钮上的小箭头（对齐旧 10px 文字箭头的视觉重量） */
.model-chevron {
  color: var(--text-faint);
}
.workdir-sep {
  height: 1px;
  background: var(--border);
  margin: 5px 6px;
}
/* 紧凑形态元素（图标）仅手机端显示，桌面端隐藏 */
.perm-icon,
.model-icon,
.sandbox-label-short {
  display: none;
}

/* ── 宽度不足时的图标化收纳（容器查询，对齐手机版形态）──
   侧栏/右侧栏展开挤窄首页卡片时禁止按钮换行，按 .kimi-card 容器宽度分档，
   档位与对话中输入栏一致：≤660 权限转图标 / ≤560 沙盒收成 开/限 /
   ≤520 模型转图标（模型名优先保留，只有真挤不下才退化）。
   阈值必须大于对应形态的实际所需宽度（完整文字约 610px：左组 附件+截图+
   完全访问+沙盒: 开 ≈264、右组 模型名+思考档位+发送 ≈317、卡片内边距 28）；
   阈值偏小会留出“文字完整但放不下”的空档，此时 .kimi-bar-left 的
   overflow:hidden 会把末尾的沙盒胶囊裁得只剩一条边框（成小方块） */
.kimi-card {
  container-type: inline-size;
  container-name: composer;
}
/* 卡片标题区按首页实际宽度分档（侧栏 + 右侧栏同时展开时中间列只剩两百多像素） */
.kimi-empty-container {
  container-type: inline-size;
  container-name: empty-page;
}
/* 卡片尽量吃满主区宽度（全局 .kimi-empty-container 左右各留 20px）：
   卡片宽出来这 20px，正好是权限文字与「只显示图标」之间的差距 */
.kimi-empty-container {
  padding-left: 10px;
  padding-right: 10px;
}
.kimi-empty-container .kimi-title {
  line-height: 1.25;
}
@container empty-page (max-width: 460px) {
  .kimi-empty-container .kimi-title {
    font-size: 20px;
  }
  .kimi-empty-container .kimi-hero {
    margin-bottom: 16px;
  }
}
/* 左右按钮组：左侧工具类可收缩（必要时整组裁剪），右侧模型/发送恒完整可见。
   选择器带上 .kimi-card-bar 是为了盖过全局 `.kimi-card-bar > div{flex-shrink:0}` */
.kimi-card-bar .kimi-bar-left {
  display: flex;
  align-items: center;
  gap: 5px;
  flex: 1 1 auto;
  min-width: 0;
  overflow: hidden;
}
.kimi-card-bar .kimi-bar-right {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-shrink: 0;
}
/* 工具栏内边距 */
.kimi-card-bar .kimi-bar-left .kimi-pill-btn,
.kimi-card-bar .kimi-bar-right .kimi-pill-btn {
  padding-left: 8px;
  padding-right: 8px;
}
.kimi-card-bar .kimi-bar-left .permission-mode-btn,
.kimi-card-bar .kimi-bar-left .sandbox-pill {
  padding-left: 10px;
  padding-right: 10px;
}
/* 工作区标签：窄容器下省略号收尾，不折行把卡片撑高 */
.workdir-label {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
/* 权限文案只在真放不下时让位 */
.kimi-card-bar .kimi-bar-left.is-tight .permission-mode-btn .perm-dot,
.kimi-card-bar .kimi-bar-left.is-tight .permission-mode-btn .perm-label,
.kimi-card-bar .kimi-bar-left.is-tight .permission-mode-btn .permission-chevron {
  display: none;
}
.kimi-card-bar .kimi-bar-left.is-tight .perm-icon {
  display: inline-flex;
}
/* 真挤不下才把模型收成图标：此时权限已图标化、沙盒多半也收成 开/限，
   完整「模型名 + 思考档位 + 发送」仍放不下才退化为纯图标形态 */
@container composer (max-width: 520px) {
  .model-chip .model-label,
  .model-chip .model-chevron {
    display: none;
  }
  .model-icon {
    display: inline-flex;
  }
}
@container composer (max-width: 560px) {
  .sandbox-label-full {
    display: none;
  }
  .sandbox-label-short {
    display: inline;
  }
}
/* ── 极窄档：侧栏与右侧栏同时展开时中间列只剩两百来像素 ──
   按“重要性由低到高”依次收纳次要入口，保证任何档位都是单行、
   发送按钮永远留在卡片内：≤400 截图隐藏、思考档位只留图标 /
   ≤340 沙盒隐藏 / ≤290 思考档位隐藏 / ≤210 工具按钮收紧内边距。
   隐藏的开关仍可在设置页与对话中输入栏调整 */
@container composer (max-width: 470px) {
  .snip-pill {
    display: none;
  }
  .kimi-bar-right :deep(.reasoning-label),
  .kimi-bar-right :deep(.reasoning-chevron) {
    display: none;
  }
}
@container composer (max-width: 380px) {
  .sandbox-pill {
    display: none;
  }
}
@container composer (max-width: 310px) {
  .kimi-bar-right :deep(.reasoning-button) {
    display: none;
  }
}
@container composer (max-width: 240px) {
  .kimi-card-bar .kimi-bar-left {
    gap: 6px;
  }
  .kimi-bar-left .kimi-pill-btn {
    padding-left: 8px;
    padding-right: 8px;
  }
}

@media (max-width: 768px) {
  /* 手机端收窄按钮：权限/模型只显示图标（颜色仍跟随状态），对齐对话中输入栏样式 */
  .permission-mode-btn .perm-dot,
  .permission-mode-btn .perm-label,
  .permission-mode-btn .permission-chevron,
  .model-chip .model-label,
  .model-chip .model-chevron,
  .sandbox-label-full,
  .sb-dot {
    display: none;
  }
  .perm-icon,
  .model-icon {
    display: inline-flex;
  }
  .sandbox-label-short {
    display: inline;
  }
  /* 手机端沙盒只留 开/限 */
  .sandbox-pill {
    font-size: 12px;
    padding: 0 9px;
  }
}
</style>

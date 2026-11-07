<template>
  <!-- 项目状态（特性 1）：三节内容的只读展示 + 每节可编辑保存。
       数据一律来自后端 core.project_profile（写在 ~/.ycode/memory/projects/），
       前端不拼路径、不本地兜底写入。 -->
  <div class="project-wrap">
    <div v-if="phase === 'loading'" class="project-hint" data-test="project-loading">
      正在读取项目状态…
    </div>

    <div v-else-if="phase === 'error'" class="project-hint" data-test="project-error">
      <p class="project-hint-text">{{ errorText }}</p>
      <button class="chip-btn" data-test="project-retry" @click="load()">重试</button>
    </div>

    <div v-else-if="phase === 'empty'" class="project-hint" data-test="project-empty">
      <p class="project-hint-title">{{ emptyTitle }}</p>
      <p class="project-hint-text">{{ emptyDesc }}</p>
      <button
        v-if="workdir"
        class="chip-btn"
        data-test="project-create"
        @click="startEditing()"
      >开始记录项目状态</button>
    </div>

    <template v-else>
      <div class="project-head">
        <span class="project-path" :title="workdir" data-test="project-workdir">{{ workdir }}</span>
        <button class="chip-btn" data-test="project-refresh" @click="load()">刷新</button>
      </div>

      <p v-if="updatedText" class="project-meta" data-test="project-updated">
        最近更新：{{ updatedText }}<span v-if="stale" class="project-stale">（状态可能已过期）</span>
      </p>

      <section
        v-for="section in SECTIONS"
        :key="section"
        class="project-card"
        data-test="project-section"
      >
        <header class="project-card-head">
          <h4 class="project-card-title">{{ section }}</h4>
          <div class="project-card-actions">
            <button
              v-if="editing !== section"
              class="chip-btn"
              data-test="project-edit"
              @click="beginEdit(section)"
            >编辑</button>
            <template v-else>
              <button
                class="chip-btn primary"
                data-test="project-save"
                :disabled="saving"
                @click="save(section)"
              >{{ saving ? '保存中…' : '保存' }}</button>
              <button
                class="chip-btn"
                data-test="project-cancel"
                :disabled="saving"
                @click="cancelEdit()"
              >取消</button>
            </template>
          </div>
        </header>
        <textarea
          v-if="editing === section"
          v-model="draft"
          class="project-editor"
          data-test="project-editor"
          rows="6"
          :disabled="saving"
          placeholder="每条建议一行；技术栈 / 架构决策 / 硬约束会在每次对话时自动带给模型"
        ></textarea>
        <pre v-else class="project-body" data-test="project-body">{{ sectionText(section) || '（未记录）' }}</pre>
      </section>

      <p v-if="saveHint" class="project-hint-ok" data-test="project-hint">{{ saveHint }}</p>
      <p v-if="saveError" class="project-hint-error" data-test="project-save-error">{{ saveError }}</p>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import { useAppStore } from '../stores/appStore';

// 激活态由 RightPanel 传入（页签可见、面板展开且停在本工作台）。
// 只在「激活 → 拉一次」时取数，不做轮询。
const props = defineProps<{ active: boolean }>();

const store = useAppStore();

// 与后端 core.project_profile.INJECT_SECTIONS 同源同序（三节自动注入）
const SECTIONS = ['技术栈', '架构决策', '硬约束'] as const;
type SectionName = typeof SECTIONS[number];
type Phase = 'loading' | 'ready' | 'empty' | 'error';

const STALE_MS = 30 * 24 * 60 * 60 * 1000;

const phase = ref<Phase>('loading');
const errorText = ref('');
const exists = ref(false);
const updated = ref('');
const sections = ref<Record<string, string>>({});
const editing = ref<SectionName | ''>('');
const draft = ref('');
const saving = ref(false);
const saveHint = ref('');
const saveError = ref('');

// 桥接访问：缺桥时给出可读错误（页签只显示内联提示，不弹模态）
function bridgeApi() {
  try {
    return window.pywebview?.api ?? null;
  } catch {
    return null;
  }
}

// 项目目录取当前会话工作目录；GLOBAL（自由对话）没有项目归属
const workdir = computed(() => {
  const wd = String(store.workdir || '').trim();
  return wd === 'GLOBAL' ? '' : wd;
});

const emptyTitle = computed(() =>
  workdir.value ? '该项目还没有记录项目状态' : '还没有进入项目目录',
);
const emptyDesc = computed(() =>
  workdir.value
    ? '项目状态记在 ~/.ycode/memory/projects/ 下，不会写进项目目录。点「开始记录」写下技术栈 / 架构决策 / 硬约束，之后每个会话都会自动带上这三节。'
    : '项目状态按项目目录归属。先在对话页引入一个项目文件夹，再回来看这里。',
);
const updatedText = computed(() => String(updated.value || '').trim());
// 与后端 STATE_STALE_DAYS = 30 同口径：只做展示提示，不拦截编辑
const stale = computed(() => {
  const raw = updatedText.value;
  if (!raw) return false;
  const ts = Date.parse(raw.replace(' ', 'T'));
  return Number.isFinite(ts) && Date.now() - ts > STALE_MS;
});

function sectionText(name: SectionName): string {
  return String(sections.value[name] ?? '').trim();
}

function hasAnyContent(): boolean {
  return SECTIONS.some((name) => sectionText(name) !== '');
}

function applyPayload(payload: any) {
  exists.value = payload?.exists === true;
  updated.value = String(payload?.updated ?? '');
  const raw = payload?.sections;
  const next: Record<string, string> = {};
  for (const name of SECTIONS) {
    next[name] = raw && typeof raw === 'object' ? String((raw as Record<string, unknown>)[name] ?? '') : '';
  }
  sections.value = next;
}

async function load() {
  if (!workdir.value) {
    phase.value = 'empty';
    exists.value = false;
    updated.value = '';
    sections.value = {};
    return;
  }
  const api = bridgeApi();
  if (!api || typeof api.get_project_profile !== 'function') {
    phase.value = 'error';
    errorText.value = '后端连接不可用：pywebview API 尚未就绪';
    return;
  }
  // 已有内容时静默重拉，避免切目录时闪白
  if (!exists.value && !hasAnyContent()) phase.value = 'loading';
  saveError.value = '';
  try {
    const res = await api.get_project_profile(workdir.value);
    if (!res || typeof res !== 'object') {
      phase.value = 'error';
      errorText.value = '后端返回了空的项目状态，无法展示';
      return;
    }
    if (res.ok === false || res.error) {
      phase.value = 'error';
      errorText.value = String(res.error || '读取项目状态失败');
      return;
    }
    applyPayload(res);
    editing.value = '';
    phase.value = hasAnyContent() ? 'ready' : 'empty';
  } catch (err) {
    phase.value = 'error';
    errorText.value = err instanceof Error ? err.message : String(err);
  }
}

// 空态里直接开始记录：先进入可编辑视图，内容为空由用户填写
function startEditing() {
  phase.value = 'ready';
  beginEdit('技术栈');
}

function beginEdit(name: SectionName) {
  editing.value = name;
  draft.value = sectionText(name);
  saveHint.value = '';
  saveError.value = '';
}

function cancelEdit() {
  editing.value = '';
  draft.value = '';
  saveError.value = '';
}

// 保存：整节覆盖；成功后用后端返回的结构化状态覆盖本地（不做前端自算）
async function save(name: SectionName) {
  if (saving.value) return;
  const api = bridgeApi();
  if (!api || typeof api.save_project_profile !== 'function') {
    saveError.value = '后端连接不可用：pywebview API 尚未就绪';
    return;
  }
  saving.value = true;
  saveHint.value = '';
  saveError.value = '';
  try {
    const res = await api.save_project_profile(workdir.value, name, draft.value);
    if (!res || typeof res !== 'object') {
      saveError.value = '保存失败：后端没有返回结果';
      return;
    }
    if (res.ok === false || res.error) {
      saveError.value = String(res.error || '保存失败，请稍后重试');
      return;
    }
    applyPayload(res);
    editing.value = '';
    draft.value = '';
    phase.value = 'ready';
    saveHint.value = '已保存';
  } catch (err) {
    saveError.value = err instanceof Error ? err.message : String(err);
  } finally {
    saving.value = false;
  }
}

// 页签激活时拉一次；切换工作目录后作废本地状态并重拉
watch(() => props.active, (active) => {
  if (!active) return;
  void load();
}, { immediate: true });

watch(() => store.workdir, () => {
  editing.value = '';
  draft.value = '';
  saveHint.value = '';
  saveError.value = '';
  exists.value = false;
  updated.value = '';
  sections.value = {};
  if (props.active) void load();
});
</script>

<style scoped>
/* 深色主题跟随全局 CSS 变量（--text / --border / --accent…），不引入新 UI 库 */
.project-wrap {
  display: flex;
  flex-direction: column;
  gap: 10px;
  height: 100%;
  min-height: 0;
  padding: 12px;
  overflow-y: auto;
  color: var(--text);
  font-size: 12.5px;
}

.project-head { display: flex; align-items: center; gap: 8px; }
.project-path {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--text-dim);
  font-size: 11.5px;
}
.project-meta { margin: 0; color: var(--text-dim); font-size: 11.5px; }
.project-stale { color: var(--warning, #d08b2c); }

.project-card {
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 8px 10px;
  background: var(--surface-2, transparent);
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.project-card-head { display: flex; align-items: center; gap: 8px; }
.project-card-title { margin: 0; font-size: 12.5px; font-weight: 600; flex: 1; }
.project-card-actions { display: flex; gap: 6px; }
.project-body {
  margin: 0;
  white-space: pre-wrap;
  word-break: break-word;
  font-family: inherit;
  font-size: 12px;
  line-height: 1.55;
  color: var(--text);
}
.project-editor {
  width: 100%;
  min-height: 96px;
  resize: vertical;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--surface-1, transparent);
  color: var(--text);
  font-family: inherit;
  font-size: 12px;
  line-height: 1.55;
  padding: 6px 8px;
  box-sizing: border-box;
}

.project-hint {
  display: flex;
  flex-direction: column;
  gap: 8px;
  align-items: flex-start;
  color: var(--text-dim);
  line-height: 1.6;
}
.project-hint-title { margin: 0; color: var(--text); font-weight: 600; }
.project-hint-text { margin: 0; }
.project-hint-ok { margin: 0; color: var(--accent, #4f9dff); }
.project-hint-error { margin: 0; color: var(--danger, #e05252); }
</style>

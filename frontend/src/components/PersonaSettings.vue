<template>
  <div class="persona-settings" data-test="persona-settings-page">
    <header class="persona-header">
      <div>
        <h1>人设设置</h1>
        <p>自定义智能体的名称与人设；人设内容留空表示使用内置默认人设。</p>
      </div>
      <button class="refresh-button" type="button" title="重新加载" @click="loadPersona">
        <AppIcon name="refresh" :size="14" />
      </button>
    </header>

    <section class="persona-card">
      <label class="field-label" for="persona-agent-name">智能体名称</label>
      <input
        id="persona-agent-name"
        v-model="agentNameDraft"
        data-test="persona-agent-name"
        class="text-field"
        type="text"
        :maxlength="50"
        placeholder="如：星瑶"
        :disabled="loading || saving"
      />

      <label class="field-label" for="persona-user-title">智能体对你的称呼</label>
      <input
        id="persona-user-title"
        v-model="userTitleDraft"
        data-test="persona-user-title"
        class="text-field"
        type="text"
        :maxlength="50"
        placeholder="如：大哥、老板；留空则不使用特定称呼"
        :disabled="loading || saving"
      />

      <label class="field-label" for="persona-text">智能体人设</label>
      <textarea
        id="persona-text"
        v-model="personaDraft"
        data-test="persona-text"
        class="persona-textarea"
        :maxlength="20000"
        rows="9"
        placeholder="描述智能体的身份、性格与说话风格；填写后取代内置默认人设（安全规范保留），清空后自动恢复默认。"
        :disabled="loading || saving"
      ></textarea>
      <div class="persona-meta">
        <span>{{ personaDraft.length }} / 20000</span>
        <span v-if="dirtyHint" class="persona-dirty">有未保存的修改</span>
      </div>

      <div class="persona-actions">
        <button
          data-test="persona-save"
          class="primary-button"
          type="button"
          :disabled="loading || saving || !canSave"
          @click="savePersona"
        >{{ saving ? '保存中…' : '保存' }}</button>
        <button
          data-test="persona-reset"
          class="secondary-button"
          type="button"
          :disabled="loading || saving"
          @click="resetDrafts"
        >放弃修改</button>
      </div>

      <p v-if="errorMsg" data-test="persona-error" class="persona-error">{{ errorMsg }}</p>
      <p v-else-if="savedTip" data-test="persona-saved" class="persona-saved">{{ savedTip }}</p>
      <p class="persona-note">人设内容与称呼会随名称一起注入会话提示词：填写后取代内置默认人设中的身份与性格设定（安全红线与工具规范保留），清空后自动恢复内置默认人设，不会丢失。</p>
      <!-- 会话级稳定段落快照：人设属于注入点 2，进行中的会话已在首请求定型目录字节
           以保住前缀缓存；因此保存的改动只对新建会话生效。 -->
      <p class="persona-note" data-test="session-snapshot-note">已打开的会话将在新会话中生效：进行中的会话沿用其启动时的目录快照，以保证前缀缓存稳定。</p>
    </section>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue';

import AppIcon from './AppIcon.vue';
import { useAppStore } from '../stores/appStore';

const store = useAppStore();
const loading = ref(false);
const saving = ref(false);
const errorMsg = ref('');
const savedTip = ref('');

const agentNameDraft = ref('');
const personaDraft = ref('');
const userTitleDraft = ref('');
const agentNameSaved = ref('');
const personaSaved = ref('');
const userTitleSaved = ref('');

let savedTipTimer: ReturnType<typeof setTimeout> | null = null;

const dirtyHint = computed(
  () => agentNameDraft.value !== agentNameSaved.value
    || personaDraft.value !== personaSaved.value
    || userTitleDraft.value !== userTitleSaved.value,
);
const canSave = computed(() => agentNameDraft.value.trim().length > 0 && dirtyHint.value);

function applySnapshot(agentName: string, persona: string, userTitle: string) {
  agentNameSaved.value = agentName;
  personaSaved.value = persona;
  userTitleSaved.value = userTitle;
  agentNameDraft.value = agentName;
  personaDraft.value = persona;
  userTitleDraft.value = userTitle;
}

async function loadPersona() {
  loading.value = true;
  errorMsg.value = '';
  try {
    const api = (window as any).pywebview?.api;
    if (!api?.get_persona_settings) {
      // 后端未就绪（如测试环境）：显示占位但不报错
      applySnapshot('星瑶', '', '');
      return;
    }
    const res = await api.get_persona_settings();
    if (res?.ok) {
      applySnapshot(
        String(res.agent_name ?? '星瑶'),
        String(res.persona ?? ''),
        String(res.user_title ?? ''),
      );
    } else {
      errorMsg.value = res?.msg || '加载人设设置失败';
    }
  } catch {
    errorMsg.value = '加载人设设置失败';
  } finally {
    loading.value = false;
  }
}

function resetDrafts() {
  agentNameDraft.value = agentNameSaved.value;
  personaDraft.value = personaSaved.value;
  userTitleDraft.value = userTitleSaved.value;
  errorMsg.value = '';
}

async function savePersona() {
  if (!canSave.value || saving.value) return;
  saving.value = true;
  errorMsg.value = '';
  try {
    const api = (window as any).pywebview?.api;
    if (!api?.set_persona_settings) {
      errorMsg.value = '后端接口未就绪，无法保存';
      return;
    }
    const res = await api.set_persona_settings(
      agentNameDraft.value.trim(),
      personaDraft.value,
      userTitleDraft.value.trim(),
    );
    if (res?.ok) {
      applySnapshot(
        String(res.agent_name ?? agentNameDraft.value.trim()),
        String(res.persona ?? personaDraft.value),
        String(res.user_title ?? userTitleDraft.value.trim()),
      );
      // 名称/称呼即时同步全局：界面文案、执行过程、思考提示立刻切换，无需重启
      store.applyPersonaSettings({ agent_name: res.agent_name, user_title: res.user_title });
      savedTip.value = res.msg || '已保存';
      if (savedTipTimer) clearTimeout(savedTipTimer);
      savedTipTimer = setTimeout(() => { savedTip.value = ''; }, 2500);
    } else {
      errorMsg.value = res?.msg || '保存失败';
    }
  } catch {
    errorMsg.value = '保存失败';
  } finally {
    saving.value = false;
  }
}

onMounted(loadPersona);
</script>

<style scoped>
.persona-settings { max-width: 920px; margin: 0 auto; }
.persona-header { display: flex; align-items: flex-start; justify-content: space-between; gap: 18px; margin-bottom: 22px; }
.persona-header h1 { margin: 0 0 7px; font-size: 25px; }
.persona-header p { margin: 0; color: var(--text-faint); font-size: 13px; }
.refresh-button { width: 32px; height: 32px; display: grid; place-items: center; border: 1px solid var(--border); border-radius: 8px; background: var(--bg2); color: var(--text-dim); flex: none; }
.refresh-button:hover { color: var(--text); background: var(--bg3); }
.persona-card { border-top: 1px solid var(--border); padding-top: 4px; }
.field-label { display: block; margin: 18px 0 7px; color: var(--text-dim); font-size: 12px; }
.text-field, .persona-textarea { width: 100%; box-sizing: border-box; border: 1px solid var(--border); border-radius: 9px; background: var(--bg2); color: var(--text); font-size: 13px; }
.text-field { min-height: 42px; padding: 0 13px; }
.persona-textarea { padding: 11px 13px; resize: vertical; min-height: 220px; line-height: 1.7; font-family: inherit; }
.text-field:focus, .persona-textarea:focus { outline: none; border-color: var(--accent); box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent) 15%, transparent); }
.text-field:disabled, .persona-textarea:disabled { cursor: wait; opacity: .7; }
.persona-meta { display: flex; justify-content: flex-end; gap: 14px; margin-top: 6px; color: var(--text-faint); font-size: 11px; }
.persona-dirty { color: var(--warning); }
.persona-actions { display: flex; gap: 10px; margin-top: 14px; }
.primary-button, .secondary-button { min-height: 38px; margin-top: 0; padding: 0 18px; border-radius: 9px; border: 1px solid var(--border); font-weight: 600; cursor: pointer; }
.primary-button { background: var(--text); color: var(--bg0); }
.primary-button:disabled { opacity: .5; cursor: not-allowed; }
.secondary-button { background: var(--bg3); color: var(--text); }
.persona-error { margin: 12px 0 0; color: var(--danger); font-size: 12px; }
.persona-saved { margin: 12px 0 0; color: var(--green); font-size: 12px; }
.persona-note { margin: 14px 0 0; color: var(--text-faint); font-size: 12px; line-height: 1.7; }

@media (max-width: 768px) {
  .persona-header { flex-direction: column; }
  .persona-actions { flex-wrap: wrap; }
}
</style>

<template>
  <div class="msg" :class="[msg.role === 'user' ? 'msg-user' : 'msg-assistant']">
    <!-- 用户消息 -->
    <div v-if="msg.role === 'user'" class="bubble user-bubble">
      {{ msg.content }}
    </div>

    <!-- 助手消息（无头像图标，仅显示“星瑶”名称） -->
    <div v-else-if="msg.role === 'assistant'" class="avatar-row">
      <div class="assistant-body">
        <div class="assistant-name">
          <b>星瑶</b> · <span style="font-size:11px;color:var(--text-faint)">{{ store.activeModelName || '模型' }}</span>
          <span v-if="msg.cancelled" class="cancel-badge">已取消</span>
        </div>

        <!-- 思考过程 Block -->
        <div v-if="msg.reasoning_content && msg.reasoning_content.trim()" class="think-block">
          <div class="tk-head" @click="thinkingOpen = !thinkingOpen">
            <span>思考过程</span>
            <span style="margin-left:auto">{{ thinkingOpen ? '▾' : '▸' }}</span>
          </div>
          <div v-show="thinkingOpen" class="tk-cnt">
            {{ msg.reasoning_content }}
          </div>
        </div>

        <!-- 工具执行时间线（含绑定/未绑定的文件变更 Diff） -->
        <ToolExecutionTrace
          v-if="(msg.tool_calls && msg.tool_calls.length > 0) || (msg.legacy_diffs && msg.legacy_diffs.length > 0)"
          :calls="msg.tool_calls || []"
          :legacy-diffs="msg.legacy_diffs || []"
          :state="msg.execution_state || 'completed'"
        />

        <!-- Markdown 文本内容 -->
        <div v-if="msg.content && msg.content.trim()" class="md-body" v-html="renderMarkdown(msg.content)"></div>

        <!-- 等待模型响应期间的动态反馈（对齐 CLI 的“星瑶正在干活”） -->
        <div v-if="showWorking" class="working-line">
          <span class="spin"></span>
          <span class="wl-text">星瑶正在干活<span class="wl-dots"><i>.</i><i>.</i><i>.</i></span></span>
          <span v-if="elapsed >= 3" class="wl-elapsed">已等待 {{ elapsed }}s</span>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, watch, onUnmounted } from 'vue';
import { useAppStore, type ChatMessage } from '../stores/appStore';
import { renderMarkdown } from '../utils/markdown';
import ToolExecutionTrace from './ToolExecutionTrace.vue';

const props = defineProps<{
  msg: ChatMessage;
}>();

// 当前气泡是“本轮正在等待响应”的空助手气泡时，显示动态干活反馈。
// 任何实际内容（思考流 / 工具调用 / 正文 token）到达后自动隐藏。
const store = useAppStore();
const showWorking = computed(() => {
  if (props.msg.role !== 'assistant' || !store.running) return false;
  const msgs = store.messages;
  if (!msgs.length || msgs[msgs.length - 1] !== props.msg) return false;
  const m = props.msg;
  if (m.content && m.content.trim()) return false;
  if (m.reasoning_content && m.reasoning_content.trim()) return false;
  if (m.tool_calls && m.tool_calls.length) return false;
  return true;
});

// 等待时长计时：等待超过 3 秒后显示秒数，让用户明确知道仍在处理而不是卡死
const elapsed = ref(0);
let elapsedTimer: number | undefined;
watch(showWorking, (v) => {
  if (v) {
    const start = Date.now();
    elapsed.value = 0;
    elapsedTimer = window.setInterval(() => {
      elapsed.value = Math.floor((Date.now() - start) / 1000);
    }, 1000);
  } else if (elapsedTimer) {
    window.clearInterval(elapsedTimer);
    elapsedTimer = undefined;
  }
}, { immediate: true });
onUnmounted(() => {
  if (elapsedTimer) window.clearInterval(elapsedTimer);
});

// 思考块默认跟随全局 store.showThinking；用户在本条消息上手动切换过一次后，
// 该条消息就"钉住"为用户选择的状态，不再随全局按钮变化。
const localThinkOverride = ref<boolean | null>(null);
const thinkingOpen = computed({
  get: () => (localThinkOverride.value !== null ? localThinkOverride.value : store.showThinking),
  set: (v: boolean) => { localThinkOverride.value = v; },
});
</script>

<style scoped>
.msg {
  max-width: 880px;
  margin: 0 auto 18px;
}
.msg-user {
  display: flex;
  justify-content: flex-end;
}
.msg-user .bubble {
  background: var(--bg2);
  border: 1px solid var(--border);
  border-radius: 12px 12px 4px 12px;
  padding: 10px 14px;
  max-width: 70%;
  line-height: 1.65;
}
.avatar-row {
  display: flex;
}
.assistant-body {
  flex: 1;
  min-width: 0;
}
.assistant-name {
  font-size: 12px;
  color: var(--text-faint);
  margin-bottom: 4px;
}
.assistant-name b {
  color: var(--text-dim);
}
.cancel-badge {
  margin-left: 8px;
  font-size: 10.5px;
  color: var(--orange);
  background: rgba(240, 136, 62, 0.12);
  border-radius: 6px;
  padding: 1px 7px;
}

.think-block {
  border-left: 2px solid var(--purple);
  background: rgba(188, 140, 255, 0.06);
  border-radius: 0 8px 8px 0;
  padding: 8px 12px;
  margin: 8px 0;
  font-size: 12.5px;
  color: var(--purple);
}
.tk-head {
  display: flex;
  align-items: center;
  cursor: pointer;
  user-select: none;
  font-weight: 600;
}
.tk-cnt {
  margin-top: 6px;
  white-space: pre-wrap;
  font-family: var(--mono);
  font-size: 12px;
  line-height: 1.6;
}

.spin {
  width: 9px;
  height: 9px;
  border: 2px solid var(--orange);
  border-top-color: transparent;
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
}
@keyframes spin {
  to { transform: rotate(360deg); }
}

/* 等待响应期间的动态反馈行 */
.working-line {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 4px;
  font-size: 12.5px;
  color: var(--text-dim);
}
.working-line .spin {
  border-color: var(--accent);
  border-top-color: transparent;
}
.wl-dots i {
  display: inline-block;
  font-style: normal;
  animation: wl-bounce 1.2s ease-in-out infinite;
}
.wl-dots i:nth-child(2) { animation-delay: 0.2s; }
.wl-dots i:nth-child(3) { animation-delay: 0.4s; }
@keyframes wl-bounce {
  0%, 60%, 100% { transform: translateY(0); opacity: 0.5; }
  30% { transform: translateY(-3px); opacity: 1; }
}
.wl-elapsed {
  color: var(--text-faint);
  font-size: 11.5px;
}

.md-body {
  margin-top: 8px;
  line-height: 1.7;
}
</style>

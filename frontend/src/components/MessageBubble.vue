<template>
  <div class="msg" :class="msg.role === 'user' ? 'msg-user' : msg.role === 'error' ? 'msg-error' : msg.role === 'notice' ? 'msg-notice' : 'msg-assistant'">
    <!-- 用户消息（文本 + 可选多张图片缩略图，点击放大） -->
    <div v-if="msg.role === 'user'" class="bubble user-bubble">
      <div v-if="msg.content" class="user-text">{{ msg.content }}</div>
      <div
        v-if="msg.images && msg.images.length"
        class="img-grid"
        :class="{ single: msg.images.length === 1, duo: msg.images.length === 2 }"
      >
        <img
          v-for="(img, i) in msg.images"
          :key="i"
          :src="img"
          class="msg-img"
          alt="用户发送的图片"
          @click="store.openLightbox(msg.images!, i)"
        >
      </div>
      <div
        v-if="msg.attachment_notes && msg.attachment_notes.length"
        class="attachment-notes"
      >
        <div
          v-for="(note, i) in msg.attachment_notes"
          :key="i"
          class="attachment-note"
        >{{ note }}</div>
      </div>
    </div>

    <!-- 助手消息（无头像图标，仅显示“星瑶”名称） -->
    <div v-else-if="msg.role === 'assistant'" class="avatar-row">
      <div class="assistant-body">
        <div class="assistant-name">
          <b>星瑶</b> · <span style="font-size:11px;color:var(--text-faint)">{{ msg.model_name || store.activeModelName || '模型' }}</span>
          <span v-if="msg.cancelled" class="cancel-badge">已取消</span>
        </div>

        <!-- 思考过程 Block（FoldCard 统一折叠体系，箭头在标题右侧） -->
        <div v-if="msg.reasoning_content && msg.reasoning_content.trim()" class="think-block">
          <FoldCard v-model:open="thinkingOpen" title="思考过程" caret="end">
            <div class="tk-cnt">
              {{ msg.reasoning_content }}
            </div>
          </FoldCard>
        </div>

        <!-- Markdown 文本内容 -->
        <div v-if="msg.content && msg.content.trim()" class="md-body" v-html="renderMarkdown(msg.content)"></div>

        <!-- 工具执行时间线（含绑定/未绑定的文件变更 Diff） -->
        <ToolExecutionTrace
          v-if="(msg.tool_calls && msg.tool_calls.length > 0) || (msg.legacy_diffs && msg.legacy_diffs.length > 0)"
          :calls="msg.tool_calls || []"
          :legacy-diffs="msg.legacy_diffs || []"
          :state="msg.execution_state || 'completed'"
        />

        <!-- 回合进行中的非终态错误（挂在助手消息上，不插队成独立 error 消息） -->
        <div v-if="msg.error_note" class="error-note">
          <AppIcon name="triangle-alert" :size="13" /> {{ msg.error_note }}
        </div>

        <!-- 等待模型响应期间的动态反馈（对齐 CLI 的“星瑶正在干活”） -->
        <div v-if="showWorking" class="working-line">
          <span class="spin"></span>
          <span class="wl-text">星瑶正在干活<span class="wl-dots"><i>.</i><i>.</i><i>.</i></span></span>
          <span v-if="elapsed >= 3" class="wl-elapsed">已等待 {{ elapsed }}s</span>
        </div>
      </div>
    </div>

    <!-- 错误提示（余额不足 / 模型异常等运行错误，写入对话流而非弹窗） -->
    <div v-else-if="msg.role === 'error'" class="error-card">
      <span class="error-icon"><AppIcon name="triangle-alert" :size="16" /></span>
      <div class="error-body">
        <div class="error-title">运行错误</div>
        <div class="error-text">{{ msg.content }}</div>
      </div>
    </div>

    <!-- 对话内系统提示（取代原 Toast：跟随对话流，不自动消失） -->
    <div v-else-if="msg.role === 'notice'" class="notice-card">
      <span class="notice-icon"><AppIcon name="info" :size="15" /></span>
      <div class="notice-body">
        <span class="notice-title">提示</span>
        <span class="notice-text">{{ msg.content }}</span>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, watch, onUnmounted } from 'vue';
import { useAppStore, type ChatMessage } from '../stores/appStore';
import { renderMarkdown } from '../utils/markdown';
import AppIcon from './AppIcon.vue';
import FoldCard from './FoldCard.vue';
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
  max-width: var(--chat-col-width);
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
.user-text {
  white-space: pre-wrap;
  word-break: break-word;
}
/* 用户消息图片：1 张大图 / 2 张并排 / 3 张及以上三列网格 */
.img-grid {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 6px;
  margin-top: 8px;
}
.img-grid.single {
  grid-template-columns: 1fr;
}
.img-grid.duo {
  grid-template-columns: repeat(2, 1fr);
}
.user-text + .img-grid {
  margin-top: 8px;
}
.img-grid:first-child {
  margin-top: 0;
}
.msg-img {
  width: 100%;
  aspect-ratio: 1;
  object-fit: cover;
  border-radius: 8px;
  cursor: zoom-in;
  border: 1px solid var(--border);
  transition: filter 0.15s;
}
.msg-img:hover {
  filter: brightness(1.1);
}
.img-grid.single .msg-img,
.img-grid.duo .msg-img {
  aspect-ratio: auto;
  max-height: 300px;
  object-fit: contain;
  background: var(--bg3);
}
.attachment-notes {
  margin-top: 8px;
  padding-top: 7px;
  border-top: 1px solid var(--border);
  color: var(--text-faint);
  font-size: 11.5px;
  line-height: 1.5;
}
.attachment-note {
  white-space: pre-wrap;
  word-break: break-word;
}
.attachment-note + .attachment-note {
  margin-top: 2px;
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
/* 思考块内的折叠头跟随块体紫色（覆盖 FoldCard 默认的 faint 灰） */
.think-block :deep(.fold-head) {
  color: inherit;
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

/* 挂在助手消息上的非终态错误提示 */
.error-note {
  display: flex;
  align-items: flex-start;
  gap: 6px;
  margin-top: 8px;
  padding: 8px 12px;
  font-size: 12.5px;
  line-height: 1.6;
  color: var(--red);
  background: color-mix(in srgb, var(--red) 8%, var(--bg2));
  border: 1px solid color-mix(in srgb, var(--red) 40%, var(--border));
  border-left: 3px solid var(--red);
  border-radius: 8px;
  white-space: pre-wrap;
  word-break: break-word;
}

.msg-error {
  display: flex;
  justify-content: flex-start;
}
.error-card {
  width: 100%;
  display: flex;
  align-items: flex-start;
  gap: 10px;
  padding: 12px 14px;
  border: 1px solid color-mix(in srgb, var(--red) 40%, var(--border));
  border-left: 3px solid var(--red);
  border-radius: 10px;
  background: color-mix(in srgb, var(--red) 8%, var(--bg2));
  font-size: 13px;
  line-height: 1.6;
}
.error-icon {
  flex-shrink: 0;
  color: var(--red);
  font-size: 16px;
  line-height: 1.6;
}
.error-body {
  flex: 1;
  min-width: 0;
}
.error-title {
  font-weight: 600;
  color: var(--red);
  margin-bottom: 2px;
}
.error-text {
  color: var(--text);
  white-space: pre-wrap;
  word-break: break-word;
}

/* 对话内系统提示条（取代原 Toast）：主色调、与错误卡区分 */
.msg-notice {
  display: flex;
  justify-content: flex-start;
}
.notice-card {
  width: 100%;
  display: flex;
  align-items: flex-start;
  gap: 10px;
  padding: 10px 14px;
  border: 1px solid color-mix(in srgb, var(--accent) 35%, var(--border));
  border-left: 3px solid var(--accent);
  border-radius: 10px;
  background: color-mix(in srgb, var(--accent) 7%, var(--bg2));
  font-size: 13px;
  line-height: 1.6;
}
.notice-icon {
  flex-shrink: 0;
  color: var(--accent);
  font-size: 15px;
  line-height: 1.6;
}
.notice-body {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 4px 8px;
}
.notice-title {
  flex-shrink: 0;
  font-weight: 600;
  color: var(--accent);
}
.notice-text {
  flex: 1;
  min-width: 0;
  color: var(--text);
  white-space: pre-wrap;
  word-break: break-word;
}
</style>

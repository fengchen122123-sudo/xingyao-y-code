<template>
  <!-- codex 风格：权限询问以卡片形式内联在对话流末尾，不再用模态弹窗打断 -->
  <div v-if="store.permRequest.show" class="perm-card">
    <div class="pc-head">
      <span class="pc-ico"><AppIcon name="triangle-alert" :size="16" /></span>
      <div class="pc-title">
        <b>权限请求</b>
        <span class="pc-src">
          {{ store.agentName }}请求执行以下操作{{ store.permRequest.source ? '（来源: ' + store.permRequest.source + '）' : '' }}
        </span>
      </div>
    </div>
    <div class="pc-body">{{ store.permRequest.summary }}</div>
    <!-- ask_user 澄清问题：选项列表，每个选项一行 -->
    <div v-if="store.permRequest.functionName === 'ask_user' && store.permRequest.options.length" class="pc-choices">
      <button
        v-for="(opt, i) in store.permRequest.options"
        :key="i"
        class="pc-choice"
        @click="store.respondPermissionChoice(i)"
      >
        <span class="pc-choice-letter">{{ String.fromCharCode(65 + i) }}</span>
        <span class="pc-choice-text">{{ opt }}</span>
      </button>
      <!-- 第四项：自定义回答输入框，选项都不合适时直接输入告诉星瑶 -->
      <div class="pc-choice pc-custom">
        <span class="pc-choice-letter">{{ String.fromCharCode(65 + store.permRequest.options.length) }}</span>
        <input
          v-model="customAnswer"
          class="pc-custom-input"
          placeholder="以上都不合适？输入你的回答..."
          @keydown.enter.prevent="submitCustom"
        >
        <button
          class="pc-custom-btn"
          :disabled="!customAnswer.trim()"
          @click="submitCustom"
        ><AppIcon name="send" :size="14" /></button>
      </div>
    </div>
    <!-- 标准权限请求：完全按后端下发的稳定 key 渲染。 -->
    <div v-else-if="store.permRequest.permissionOptions.length" class="pc-foot">
      <button
        v-for="option in store.permRequest.permissionOptions"
        :key="option.key"
        data-test="permission-option"
        class="pc-btn"
        :class="{ primary: option.key === 'allow_once', danger: option.key === 'deny' }"
        @click="store.respondPermissionOption(option.key)"
      >{{ option.label }}</button>
    </div>
    <!-- 兼容旧后端事件，完整升级后可移除。 -->
    <div v-else class="pc-foot">
      <button class="pc-btn primary" @click="store.respondPermission(true, false)">允许一次</button>
      <button
        v-if="store.permRequest.allowAlways"
        class="pc-btn"
        @click="store.respondPermission(true, true)"
      >{{ store.permRequest.functionName === 'sandbox_outside_access' ? '本任务内放行沙盒外访问' : '总是允许此类操作' }}</button>
      <button class="pc-btn danger" @click="store.respondPermission(false, false)">拒绝</button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, watch } from 'vue';
import AppIcon from './AppIcon.vue';
import { useAppStore } from '../stores/appStore';
const store = useAppStore();

// ask_user 第四项：自定义回答（选项都不合适时直接输入告诉星瑶）
const customAnswer = ref('');
function submitCustom() {
  const text = customAnswer.value.trim();
  if (!text) return;
  customAnswer.value = '';
  store.respondPermissionChoice(-1, text);
}

// 新提问到达时清空上一题的未提交草稿：卡片常驻不销毁（根节点 v-if 只控制
// 显隐），残留草稿会被误发给下一个问题
watch(() => store.permRequest.reqId, () => {
  customAnswer.value = '';
});
</script>

<style scoped>
.perm-card {
  max-width: var(--chat-col-width);
  margin: 10px auto;
  background: var(--bg1);
  border: 1px solid var(--border);
  border-left: 3px solid var(--orange);
  border-radius: 12px;
  overflow: hidden;
}
.pc-head {
  padding: 12px 16px 8px;
  display: flex;
  align-items: center;
  gap: 10px;
}
.pc-ico {
  width: 30px;
  height: 30px;
  border-radius: 8px;
  background: rgba(240, 136, 62, 0.15);
  color: var(--orange);
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 15px;
  flex-shrink: 0;
}
.pc-title {
  display: flex;
  flex-direction: column;
  gap: 2px;
  font-size: 14px;
}
.pc-src {
  font-size: 12px;
  color: var(--text-faint);
}
.pc-body {
  margin: 0 16px 12px;
  padding: 10px 14px;
  font-family: var(--mono);
  font-size: 12.5px;
  background: var(--code-bg);
  border-radius: 8px;
  white-space: pre-wrap;
  word-break: break-all;
  line-height: 1.65;
  color: var(--text-dim);
  max-height: 220px;
  overflow-y: auto;
}
.pc-foot {
  padding: 0 16px 13px;
  display: flex;
  gap: 9px;
  justify-content: flex-end;
}
.pc-btn {
  border-radius: 8px;
  padding: 6px 15px;
  font-size: 12.5px;
  border: 1px solid var(--border);
  background: var(--bg3);
  color: var(--text);
  transition: 0.15s;
  cursor: pointer;
}
.pc-btn:hover {
  border-color: var(--text-faint);
}
.pc-btn.primary {
  background: var(--accent);
  border: none;
  color: #fff;
  font-weight: 600;
}
.pc-btn.danger {
  color: var(--red);
}
.pc-btn.danger:hover {
  border-color: var(--red);
  background: rgba(248, 81, 73, 0.08);
}
.pc-choices {
  padding: 0 16px 14px;
  display: flex;
  flex-direction: column;
  gap: 7px;
}
.pc-choice {
  width: 100%;
  display: flex;
  align-items: flex-start;
  gap: 10px;
  padding: 10px 13px;
  border: 1px solid var(--border);
  border-radius: 9px;
  background: var(--bg2);
  color: var(--text);
  font-size: 13px;
  line-height: 1.5;
  text-align: left;
  cursor: pointer;
  transition: 0.15s;
}
.pc-choice:hover {
  border-color: var(--accent);
  background: var(--bg3);
}
.pc-choice-letter {
  flex-shrink: 0;
  width: 22px;
  height: 22px;
  border-radius: 6px;
  background: var(--accent-soft);
  color: var(--accent);
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 12px;
  font-weight: 700;
}
.pc-choice-text {
  flex: 1;
  min-width: 0;
  white-space: pre-wrap;
  word-break: break-word;
}
/* 第四项自定义输入行：沿用选项行视觉，右侧替换为输入框 + 发送按钮 */
.pc-custom {
  cursor: default;
  align-items: center;
}
.pc-custom-input {
  flex: 1;
  min-width: 0;
  background: transparent;
  border: none;
  outline: none;
  color: var(--text);
  font-size: 13px;
  font-family: inherit;
  line-height: 1.5;
  padding: 0;
}
.pc-custom-input::placeholder {
  color: var(--text-faint);
}
.pc-custom-btn {
  flex-shrink: 0;
  width: 28px;
  height: 28px;
  border-radius: 7px;
  border: 1px solid var(--border);
  background: var(--bg3);
  color: var(--text-dim);
  display: flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  transition: 0.15s;
}
.pc-custom-btn:hover:not(:disabled) {
  border-color: var(--accent);
  color: var(--accent);
}
.pc-custom-btn:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}
</style>

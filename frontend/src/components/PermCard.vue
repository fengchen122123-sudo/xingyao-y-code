<template>
  <!-- 内联交互卡片：对齐极简 v2（方案A）表面阶梯，覆盖权限审批、按回合撤销确认与 ask_user 澄清选择 -->
  <div
    v-if="store.permRequest.show"
    class="perm-card"
    :class="{ 'is-question-card': isAskUser }"
    data-test="perm-card"
    role="region"
    :aria-label="headerTitle"
  >
    <div class="pc-head">
      <div class="pc-head-l">
        <span class="pc-ico" :class="iconToneClass">
          <AppIcon :name="headerIconName" :size="14.5" :stroke-width="2" />
        </span>
        <span class="pc-title">
          <b>{{ headerTitle }}</b>
          <span v-if="headerAction" class="pc-action-tag">{{ headerAction }}</span>
        </span>
        <span v-if="store.permRequest.source" class="pc-src">· {{ store.permRequest.source }}</span>
      </div>
      <span class="pc-head-status">
        <span class="pc-status-dot" :class="iconToneClass" aria-hidden="true"></span>
        <span>{{ isAskUser ? '等待回答' : '等待确认' }}</span>
      </span>
    </div>

    <!-- ask_user：清爽对话体提问 + 选项列表 -->
    <div v-if="isAskUser" class="pc-ask-wrap">
      <div class="pc-question" data-test="perm-question-body">{{ askUserQuestion }}</div>
      <div class="pc-choices">
        <button
          v-for="(opt, i) in store.permRequest.options"
          :key="i"
          type="button"
          class="pc-choice"
          data-test="perm-question-choice"
          @click="store.respondPermissionChoice(i)"
        >
          <span class="pc-choice-letter">{{ String.fromCharCode(65 + i) }}</span>
          <span class="pc-choice-text">{{ opt }}</span>
          <AppIcon class="pc-choice-arrow" name="chevron-right" :size="14" :stroke-width="2" />
        </button>
        <div class="pc-choice pc-custom" @click="focusCustomInput">
          <span class="pc-choice-letter">{{ String.fromCharCode(65 + store.permRequest.options.length) }}</span>
          <input
            ref="customInputRef"
            v-model="customAnswer"
            class="pc-custom-input"
            data-test="perm-custom-input"
            placeholder="以上都不合适？输入你的回答..."
            @keydown.enter.prevent="submitCustom"
          >
          <button
            type="button"
            class="pc-custom-btn"
            data-test="perm-custom-submit"
            title="发送回答"
            :disabled="!customAnswer.trim()"
            @click.stop="submitCustom"
          ><AppIcon name="send" :size="13" :stroke-width="2" /></button>
        </div>
      </div>
    </div>

    <!-- 标准权限 / 撤销：中间内容带直接呈现命令或路径，不再套双层嵌套卡片 -->
    <template v-else>
      <div v-if="commandParts" class="pc-body is-command">
        <div v-if="commandParts.shellNote" class="pc-cmd-meta">{{ commandParts.shellNote }}</div>
        <pre class="pc-cmd-code">{{ commandParts.command }}</pre>
      </div>
      <div v-else-if="sandboxParts" class="pc-body is-sandbox">
        <div class="pc-kv-row">
          <span class="pc-kv-k">路径</span>
          <code class="pc-kv-v">{{ sandboxParts.path }}</code>
        </div>
        <div v-if="sandboxParts.op" class="pc-kv-row">
          <span class="pc-kv-k">操作</span>
          <span class="pc-kv-text">{{ sandboxParts.op }}</span>
        </div>
      </div>
      <div v-else-if="showStandardBody && actionTargetParts" class="pc-body is-target">
        <code class="pc-target-code">{{ actionTargetParts.target }}</code>
      </div>
      <div v-else-if="showStandardBody" class="pc-body">{{ store.permRequest.summary }}</div>

      <!-- 按回合撤销：undo_turn 卡片展示服务预检的影响摘要与警告 -->
      <div v-if="isUndoTurn && undoImpact" class="pc-undo-impact">
        <template v-if="undoImpact.available">
          <div class="pc-undo-line">
            回合 {{ undoImpact.short_id }}：共 {{ undoImpact.file_count }} 个文件 —
            恢复原内容 {{ undoImpact.restore_content_count }}，
            删除本轮新建 {{ undoImpact.delete_created_count }}，
            恢复本轮删除 {{ undoImpact.restore_deleted_count }}
          </div>
          <div v-if="(undoImpact.created_files || []).length" class="pc-undo-line">
            将删除的新建文件：{{ undoImpact.created_files.join('、') }}
          </div>
          <div class="pc-undo-line muted">聊天历史保留，{{ store.agentName }}将记住文件结果已失效。</div>
          <div v-if="undoImpact.has_uncovered_command_effects" class="pc-undo-line warn">
            ⚠️ 本轮执行过通用命令：仅恢复受控文件，命令的其他副作用不撤销。
          </div>
          <div v-if="(undoImpact.subsequent_completed_turns || 0) > 0" class="pc-undo-line warn">
            ⚠️ 目标回合之后还有 {{ undoImpact.subsequent_completed_turns }} 个已完成回合，可能存在语义依赖，撤销后请重新检查或测试。
          </div>
          <div v-if="(undoImpact.conflicts || []).length" class="pc-undo-line warn">
            ⚠️ 预检发现冲突文件，批准后将拒绝整轮撤销、零文件写入。
          </div>
        </template>
        <template v-else>
          <div class="pc-undo-line warn">⚠️ 预检未通过：{{ undoImpact.message || '预检不可用' }}（批准后执行会拒绝写入）</div>
        </template>
      </div>

      <!-- 标准权限底部操作栏：左放拒绝（克制弱化），右聚合所有放行动作 -->
      <div v-if="store.permRequest.permissionOptions.length" class="pc-foot">
        <div v-if="denyOptions.length" class="pc-foot-left">
          <button
            v-for="option in denyOptions"
            :key="option.key"
            type="button"
            data-test="permission-option"
            :data-option-key="option.key"
            class="pc-btn danger"
            @click="store.respondPermissionOption(option.key)"
          >{{ option.label }}</button>
        </div>
        <div class="pc-foot-right">
          <button
            v-for="option in secondaryAllowOptions"
            :key="option.key"
            type="button"
            data-test="permission-option"
            :data-option-key="option.key"
            class="pc-btn"
            @click="store.respondPermissionOption(option.key)"
          >{{ option.label }}</button>
          <button
            v-for="option in primaryAllowOptions"
            :key="option.key"
            type="button"
            data-test="permission-option"
            :data-option-key="option.key"
            class="pc-btn primary"
            @click="store.respondPermissionOption(option.key)"
          >{{ option.label }}</button>
        </div>
      </div>

      <!-- 兼容旧后端事件，完整升级后可移除。 -->
      <div v-else class="pc-foot">
        <div class="pc-foot-left">
          <button type="button" class="pc-btn danger" @click="store.respondPermission(false, false)">拒绝</button>
        </div>
        <div class="pc-foot-right">
          <button
            v-if="store.permRequest.allowAlways"
            type="button"
            class="pc-btn"
            @click="store.respondPermission(true, true)"
          >{{ store.permRequest.functionName === 'sandbox_outside_access' ? '本任务内放行沙盒外访问' : '总是允许此类操作' }}</button>
          <button type="button" class="pc-btn primary" @click="store.respondPermission(true, false)">允许一次</button>
        </div>
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import AppIcon from './AppIcon.vue';
import type { IconName } from './iconPaths';
import { useAppStore } from '../stores/appStore';

const store = useAppStore();

const isAskUser = computed(
  () => store.permRequest.functionName === 'ask_user' && store.permRequest.options.length > 0,
);
const isUndoTurn = computed(() => store.permRequest.functionName === 'undo_turn');
const isSandboxAccess = computed(() => store.permRequest.functionName === 'sandbox_outside_access');
const undoImpact = computed(() => store.permRequest.undoImpact ?? null);

const commandParts = computed(() => {
  if (store.permRequest.functionName !== 'execute_command') return null;
  const raw = String(store.permRequest.summary || '').trim();
  const match = raw.match(/^执行命令(?:\n([^\n：:]+))?[：:]\s*\n([\s\S]+)$/);
  if (!match) return null;
  return {
    shellNote: match[1] ? match[1].trim() : '',
    command: match[2],
  };
});

const sandboxParts = computed(() => {
  if (!isSandboxAccess.value) return null;
  const raw = String(store.permRequest.summary || '').trim();
  const pathMatch = raw.match(/(?:^|\n)路径[：:]\s*([^\n]+)/);
  if (!pathMatch) return null;
  const opMatch = raw.match(/(?:^|\n)操作[：:]\s*([^\n]+)/);
  return {
    path: pathMatch[1].trim(),
    op: opMatch ? opMatch[1].trim() : '',
  };
});

const actionTargetParts = computed(() => {
  if (isAskUser.value || commandParts.value || sandboxParts.value) return null;
  const raw = String(store.permRequest.summary || '').trim();
  if (!raw || raw.includes('\n')) return null;
  const match = raw.match(/^([^：\n]{2,16})[：:]\s*(.+)$/);
  if (!match) return null;
  return {
    action: match[1].trim(),
    target: match[2].trim(),
  };
});

const headerIconName = computed<IconName>(() => {
  if (isAskUser.value) return 'message-square';
  if (isUndoTurn.value) return 'undo';
  if (isSandboxAccess.value) return 'triangle-alert';
  if (store.permRequest.functionName === 'execute_command') return 'square-terminal';
  return 'shield';
});

const iconToneClass = computed(() => (isAskUser.value ? 'tone-question' : 'tone-perm'));

const headerTitle = computed(() => {
  if (isAskUser.value) return '需要你确认';
  if (isUndoTurn.value) return '撤销确认';
  return '权限请求';
});

const headerAction = computed(() => {
  if (isAskUser.value) return '';
  if (isUndoTurn.value) return '回退本轮修改';
  if (isSandboxAccess.value) return '访问沙盒外路径';
  if (commandParts.value) return '执行命令';
  if (actionTargetParts.value) return actionTargetParts.value.action;
  return '';
});

const askUserQuestion = computed(() => {
  const raw = String(store.permRequest.summary || '').trim();
  const stripped = raw.replace(/^等待用户回答[：:]\s*/, '').trim();
  return stripped || raw;
});

const showStandardBody = computed(() => {
  if (isUndoTurn.value && undoImpact.value && store.permRequest.summary === '使用工具 undo_turn') {
    return false;
  }
  return Boolean(String(store.permRequest.summary || '').trim());
});

// 按钮分层归组：拒绝放左侧克制呈现，次要放行与主放行聚合在右侧
const denyOptions = computed(() =>
  store.permRequest.permissionOptions.filter((o) => o.key === 'deny'),
);
const primaryAllowOptions = computed(() =>
  store.permRequest.permissionOptions.filter((o) => o.key === 'allow_once'),
);
const secondaryAllowOptions = computed(() =>
  store.permRequest.permissionOptions.filter((o) => o.key !== 'deny' && o.key !== 'allow_once'),
);

// ask_user 自定义回答输入
const customAnswer = ref('');
const customInputRef = ref<HTMLInputElement | null>(null);

function focusCustomInput() {
  customInputRef.value?.focus();
}

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
  margin: 10px auto 12px;
  background: var(--xy-surface, var(--bg2));
  border: 1px solid var(--xy-border-strong, var(--border));
  border-radius: 12px;
  box-shadow: var(--xy-shadow-sm, none);
  overflow: hidden;
  animation: fadein 0.16s cubic-bezier(0.16, 1, 0.3, 1);
}

.pc-head {
  padding: 10px 14px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}
.pc-head-l {
  display: flex;
  align-items: center;
  gap: 7px;
  min-width: 0;
  flex: 1;
}
.pc-ico {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
}
.pc-ico.tone-perm {
  color: var(--xy-amber, var(--orange));
}
.pc-ico.tone-question {
  color: var(--xy-accent, var(--accent));
}

.pc-title {
  display: inline-flex;
  align-items: baseline;
  gap: 6px;
  min-width: 0;
  flex-wrap: wrap;
}
.pc-title b {
  font-size: 13px;
  font-weight: 600;
  line-height: 1.35;
  color: var(--xy-text, var(--text-bright));
}
.pc-action-tag {
  font-size: 12.5px;
  font-weight: 500;
  color: var(--xy-text-2, var(--text-dim));
}
.pc-src {
  flex-shrink: 0;
  font-size: 12px;
  color: var(--xy-text-3, var(--text-faint));
}

.pc-head-status {
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 11.5px;
  color: var(--xy-text-3, var(--text-faint));
  white-space: nowrap;
}
.pc-status-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
}
.pc-status-dot.tone-perm {
  background: var(--xy-amber, var(--orange));
}
.pc-status-dot.tone-question {
  background: var(--xy-accent, var(--accent));
}

/* 中间内容带：全宽贴合容器，铺设画布底色，避免嵌套圆角框的臃肿感 */
.pc-body {
  padding: 11px 14px;
  background: var(--xy-canvas, var(--bg1));
  border-top: 1px solid var(--xy-border, var(--border));
  font-family: var(--sans);
  font-size: 13px;
  line-height: 1.65;
  color: var(--xy-text, var(--text));
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  word-break: break-word;
  max-height: 220px;
  overflow-y: auto;
}
.pc-body.is-command {
  display: flex;
  flex-direction: column;
  gap: 5px;
  white-space: normal;
}
.pc-cmd-meta {
  font-size: 11.5px;
  color: var(--xy-text-3, var(--text-faint));
}
.pc-cmd-code {
  margin: 0;
  font-family: "JetBrains Mono", "Cascadia Code", "Consolas", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
  font-size: 12.5px;
  line-height: 1.6;
  color: var(--xy-text, var(--text));
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  word-break: break-word;
}

.pc-body.is-target {
  white-space: normal;
}
.pc-target-code {
  font-family: "JetBrains Mono", "Cascadia Code", "Consolas", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
  font-size: 12.5px;
  line-height: 1.6;
  color: var(--xy-text, var(--text));
  overflow-wrap: anywhere;
  word-break: break-word;
}

.pc-body.is-sandbox {
  display: flex;
  flex-direction: column;
  gap: 5px;
  white-space: normal;
}
.pc-kv-row {
  display: flex;
  align-items: baseline;
  gap: 10px;
  font-size: 12.5px;
  line-height: 1.55;
}
.pc-kv-k {
  flex-shrink: 0;
  color: var(--xy-text-3, var(--text-faint));
}
.pc-kv-v {
  min-width: 0;
  font-family: "JetBrains Mono", "Cascadia Code", "Consolas", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
  color: var(--xy-text, var(--text));
  overflow-wrap: anywhere;
  word-break: break-word;
}
.pc-kv-text {
  color: var(--xy-text-2, var(--text-dim));
}

/* 按回合撤销：undo_turn 影响摘要 */
.pc-undo-impact {
  padding: 11px 14px;
  background: var(--xy-canvas, var(--bg1));
  border-top: 1px solid var(--xy-border, var(--border));
  font-size: 12.5px;
  line-height: 1.65;
  color: var(--xy-text-2, var(--text-dim));
  display: flex;
  flex-direction: column;
  gap: 3px;
}
.pc-undo-line {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  word-break: break-word;
}
.pc-undo-line.warn {
  color: var(--xy-amber, var(--orange));
}
.pc-undo-line.muted {
  color: var(--xy-text-3, var(--text-faint));
}

/* 标准权限底部操作栏：28px 轻量按钮，左弱化拒绝、右聚合放行 */
.pc-foot {
  padding: 9px 14px;
  background: var(--xy-surface, var(--bg2));
  border-top: 1px solid var(--xy-border, var(--border));
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
  justify-content: space-between;
}
.pc-foot-left,
.pc-foot-right {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 7px;
}
.pc-foot-right {
  margin-left: auto;
}

.pc-btn {
  height: 28px;
  border-radius: 7px;
  padding: 0 11px;
  font-size: 12px;
  font-weight: 500;
  line-height: 1;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: 1px solid var(--xy-border-strong, var(--border));
  background: var(--xy-card, var(--bg1));
  color: var(--xy-text-2, var(--text-dim));
  white-space: nowrap;
  cursor: pointer;
  transition: background 0.14s ease, border-color 0.14s ease, color 0.14s ease, transform 0.1s ease, filter 0.14s ease;
}
.pc-btn:hover {
  background: var(--xy-hover, var(--bg3));
  color: var(--xy-text, var(--text-bright));
}
.pc-btn:active {
  transform: scale(0.98);
}
.pc-btn:focus-visible {
  outline: 2px solid var(--xy-accent, var(--accent));
  outline-offset: 2px;
}
.pc-btn.primary {
  padding: 0 13px;
  background: var(--xy-accent, var(--accent));
  border-color: transparent;
  color: var(--xy-accent-fg, #ffffff);
  box-shadow: var(--xy-shadow-sm, none);
}
.pc-btn.primary:hover {
  background: var(--xy-accent, var(--accent));
  border-color: transparent;
  color: var(--xy-accent-fg, #ffffff);
  filter: brightness(0.93);
}
.pc-btn.danger {
  background: transparent;
  border-color: transparent;
  color: var(--xy-text-3, var(--text-faint));
}
.pc-btn.danger:hover {
  border-color: color-mix(in srgb, var(--xy-rose, var(--red)) 30%, transparent);
  background: var(--xy-rose-soft, rgba(248, 81, 73, 0.08));
  color: var(--xy-rose, var(--red));
}

/* ask_user 澄清选择区域 */
.pc-ask-wrap {
  padding: 12px 14px 14px;
  background: var(--xy-canvas, var(--bg1));
  border-top: 1px solid var(--xy-border, var(--border));
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.pc-question {
  font-family: var(--sans);
  font-size: 13.5px;
  line-height: 1.65;
  color: var(--xy-text, var(--text));
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  word-break: break-word;
  max-height: 220px;
  overflow-y: auto;
}
.pc-choices {
  display: flex;
  flex-direction: column;
  gap: 7px;
}
.pc-choice {
  width: 100%;
  display: flex;
  align-items: flex-start;
  gap: 10px;
  padding: 9px 12px;
  border: 1px solid var(--xy-border, var(--border));
  border-radius: 9px;
  background: var(--xy-surface, var(--bg2));
  color: var(--xy-text, var(--text));
  font-size: 13px;
  line-height: 1.5;
  text-align: left;
  cursor: pointer;
  outline: none;
  transition: background 0.15s ease, border-color 0.15s ease, transform 0.1s ease, box-shadow 0.15s ease;
}
.pc-choice:not(.pc-custom):hover {
  border-color: color-mix(in srgb, var(--xy-accent, var(--accent)) 42%, var(--xy-border, var(--border)));
  background: color-mix(in srgb, var(--xy-accent, var(--accent)) 6%, var(--xy-card, var(--bg1)));
}
.pc-choice:not(.pc-custom):active {
  transform: scale(0.995);
}
.pc-choice:focus-visible {
  outline: 2px solid var(--xy-accent, var(--accent));
  outline-offset: 1px;
}

.pc-choice-letter {
  flex-shrink: 0;
  width: 22px;
  height: 22px;
  border-radius: 6px;
  background: var(--xy-accent-soft, var(--accent-soft));
  color: var(--xy-accent, var(--accent));
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-family: var(--mono);
  font-size: 11.5px;
  font-weight: 600;
  transition: background 0.15s ease, color 0.15s ease;
}
.pc-choice:not(.pc-custom):hover .pc-choice-letter {
  background: var(--xy-accent, var(--accent));
  color: var(--xy-accent-fg, #ffffff);
}

.pc-choice-text {
  flex: 1;
  min-width: 0;
  padding-top: 1px;
  white-space: pre-wrap;
  word-break: break-word;
}
.pc-choice-arrow {
  flex-shrink: 0;
  margin-top: 4px;
  color: var(--xy-text-3, var(--text-faint));
  opacity: 0;
  transform: translateX(-3px);
  transition: opacity 0.15s ease, transform 0.15s ease, color 0.15s ease;
}
.pc-choice:not(.pc-custom):hover .pc-choice-arrow {
  opacity: 0.9;
  transform: translateX(0);
  color: var(--xy-accent, var(--accent));
}

.pc-custom {
  cursor: text;
  align-items: center;
  padding: 6px 7px 6px 12px;
  background: var(--xy-card, var(--bg1));
  border-color: var(--xy-border-strong, var(--border));
}
.pc-custom .pc-choice-letter {
  background: var(--xy-hover, var(--bg3));
  color: var(--xy-text-3, var(--text-faint));
}
.pc-custom:focus-within {
  border-color: var(--xy-accent, var(--accent));
  box-shadow: 0 0 0 3px var(--xy-accent-soft, var(--accent-soft));
}
.pc-custom:focus-within .pc-choice-letter {
  background: var(--xy-accent-soft, var(--accent-soft));
  color: var(--xy-accent, var(--accent));
}

.pc-custom-input {
  flex: 1;
  min-width: 0;
  background: transparent;
  border: none;
  outline: none;
  color: var(--xy-text, var(--text));
  font-size: 13px;
  font-family: inherit;
  line-height: 1.5;
  padding: 0;
}
.pc-custom-input::placeholder {
  color: var(--xy-text-3, var(--text-faint));
}

.pc-custom-btn {
  flex-shrink: 0;
  width: 26px;
  height: 26px;
  border-radius: 9999px;
  border: none;
  background: var(--xy-accent, var(--accent));
  color: var(--xy-accent-fg, #ffffff);
  display: flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  transition: background 0.15s ease, color 0.15s ease, filter 0.15s ease, opacity 0.15s ease;
}
.pc-custom-btn:hover:not(:disabled) {
  filter: brightness(0.92);
}
.pc-custom-btn:focus-visible {
  outline: 2px solid var(--xy-accent, var(--accent));
  outline-offset: 2px;
}
.pc-custom-btn:disabled {
  background: var(--xy-hover, var(--bg3));
  color: var(--xy-text-3, var(--text-faint));
  opacity: 0.7;
  cursor: not-allowed;
}

@media (max-width: 768px) {
  .pc-btn {
    min-height: 34px;
  }
}
</style>

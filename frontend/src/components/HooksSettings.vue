<template>
  <div class="hooks-settings">
    <!-- 表单视图：新建/编辑钩子（替换列表） -->
    <div v-if="hookForm.mode !== 'closed'" class="hook-form-page">
      <button class="hook-back" @click="closeHookForm"><AppIcon name="arrow-left" :size="12" /> 返回列表</button>
      <div class="hook-form-head">
        <h3>{{ hookForm.mode === 'edit' ? '编辑钩子' : '新建钩子' }}</h3>
        <p>在任务生命周期事件发生时自动执行命令。</p>
      </div>
      <div class="hook-form-card">
        <div class="hf-row">
          <label class="hf-field">
            <span class="hf-label">事件</span>
            <select v-model="hookForm.event" data-test="hook-form-event" title="触发该钩子的生命周期事件">
              <option value="SessionStart">SessionStart</option>
              <option value="PreToolUse">PreToolUse</option>
              <option value="PostToolUse">PostToolUse</option>
              <option value="Stop">Stop</option>
            </select>
          </label>
          <label class="hf-field">
            <span class="hf-label">作用域</span>
            <select v-if="hookForm.mode === 'create'" v-model="hookForm.scope" data-test="hook-form-scope" title="新建钩子的保存位置">
              <option value="user">用户</option>
              <option value="project">项目</option>
            </select>
            <!-- 编辑时作用域随原条目不迁移：只读展示 -->
            <span v-else class="hf-readonly" data-test="hook-form-scope">{{ hookForm.scope === 'user' ? '用户级' : '项目级' }}</span>
          </label>
        </div>
        <div class="hf-row">
          <label class="hf-field">
            <span class="hf-label">匹配器</span>
            <input v-model="hookForm.matcher" data-test="hook-form-matcher" placeholder="例如 write_file | edit_file（留空匹配全部）" />
            <span class="hf-hint">仅对 PreToolUse / PostToolUse 生效；留空或 * 匹配全部工具。</span>
          </label>
          <label class="hf-field">
            <span class="hf-label">命令</span>
            <input v-model="hookForm.command" class="hf-mono" data-test="hook-form-command" placeholder="例如 python ./scripts/guard.py" />
          </label>
        </div>
        <details class="hf-advanced">
          <summary>高级</summary>
          <div class="hf-row">
            <label class="hf-field">
              <span class="hf-label">超时时间（秒）</span>
              <input v-model.number="hookForm.timeoutSec" type="number" min="1" max="300" style="width:140px" data-test="hook-form-timeout" />
              <span class="hf-hint">默认 60 秒，上限 300 秒；超时按失败处理，不阻断主流程。</span>
            </label>
            <label class="hf-field">
              <span class="hf-label">状态消息</span>
              <input v-model="hookForm.statusMessage" data-test="hook-form-status-message" placeholder="例如 正在检查工作区" />
            </label>
          </div>
        </details>
        <div class="hf-actions">
          <button v-if="hookForm.mode === 'edit'" class="hf-delete" data-test="hook-form-delete" @click="onDeleteHook">删除</button>
          <span style="flex:1"></span>
          <button class="btn primary" data-test="hook-form-save" :disabled="hookForm.saving" @click="submitHookForm">保存</button>
          <button class="btn" data-test="hook-form-cancel" @click="closeHookForm">取消</button>
        </div>
      </div>
    </div>

    <!-- 列表视图 -->
    <template v-else>
      <div class="sub-toolbar">
        <div class="src-filter">
          <button class="seg-btn" :class="{ active: hookScopeFilter === 'all' }" data-test="hook-scope-filter-all" @click="hookScopeFilter = 'all'">全部</button>
          <button class="seg-btn" :class="{ active: hookScopeFilter === 'user' }" data-test="hook-scope-filter-user" @click="hookScopeFilter = 'user'">用户</button>
          <button class="seg-btn" :class="{ active: hookScopeFilter === 'project' }" data-test="hook-scope-filter-project" @click="hookScopeFilter = 'project'">项目</button>
        </div>
        <input v-model="hookQuery" class="hook-search" data-test="hook-search-input" placeholder="搜索钩子…" />
        <span class="toolbar-spacer"></span>
        <button
          class="chip-btn"
          data-test="refresh-hooks-btn"
          title="重新读取用户级与项目级 Hook"
          :disabled="store.hooksLoading"
          @click="store.refreshHooks()"
        >
          <AppIcon name="refresh" :size="12" :class="{ 'icon-spin': store.hooksLoading }" />
          {{ store.hooksLoading ? '刷新中…' : '刷新' }}
        </button>
        <button class="chip-btn" data-test="hook-new-btn" title="新建钩子" @click="openHookCreate">
          <AppIcon name="plus" :size="12" /> 新建钩子
        </button>
      </div>
      <div class="hook-list">
        <div v-if="store.hooks.length === 0" class="hook-empty">
          <div class="he-title">尚未配置钩子</div>
          <div class="he-desc">新建钩子，以在任务生命周期事件中运行命令。（用户级 ~/.ycode/hooks.json、项目级 .ycode/hooks.json）</div>
          <button class="btn primary" data-test="hook-empty-new-btn" @click="openHookCreate">新建钩子</button>
        </div>
        <div v-else-if="filteredHooks.length === 0" class="sub-empty">没有符合筛选条件的钩子</div>
        <div
          v-for="h in filteredHooks"
          :key="h.digest"
          class="hook-row"
          :class="{ 'is-disabled': h.disabled === true }"
          data-test="hook-row"
          @click="openHookEditor(h)"
        >
          <div class="hr-ico"><AppIcon name="zap" :size="15" /></div>
          <div class="hr-main">
            <div class="hr-line">
              <span class="hr-event">{{ h.event }}</span>
              <span class="pl-src" :class="h.scope === 'user' ? 'user' : 'builtin'">{{ h.scope === 'user' ? '用户' : '项目' }}</span>
              <span class="hook-status" :class="h.status">{{ hookStatusText(h.status) }}</span>
              <span v-if="h.disabled === true" class="hook-status disabled-badge">已禁用</span>
            </div>
            <div class="hr-cmd">{{ h.command }}</div>
            <div class="hr-meta">matcher: {{ h.matcher }} · 来源: {{ h.source_path }}</div>
          </div>
          <div class="hr-actions" @click.stop>
            <!-- 待确认/配置已变更：需先信任才会执行，拒绝则显式屏蔽 -->
            <button
              v-if="h.status === 'pending' || h.status === 'stale'"
              class="btn primary sc-trust-btn"
              data-test="hook-action-grant"
              title="确认内容后允许该 Hook 命令执行"
              @click="store.grantHookTrust(h.digest)"
            >信任</button>
            <button
              v-if="h.status === 'pending' || h.status === 'stale'"
              class="btn sc-trust-btn"
              data-test="hook-action-deny"
              title="拒绝后该 Hook 命令不再执行"
              @click="store.denyHookTrust(h.digest)"
            >拒绝</button>
            <button
              v-if="h.status === 'trusted'"
              class="btn sc-trust-btn"
              data-test="hook-action-revoke"
              title="撤销信任，该 Hook 回到待确认"
              @click="store.revokeHookTrust(h.digest)"
            >撤销信任</button>
            <button
              v-if="h.status === 'blocked' || h.status === 'revoked'"
              class="btn sc-trust-btn"
              data-test="hook-action-reset"
              title="重置为待确认，可重新决定是否信任"
              @click="store.resetHookTrust(h.digest)"
            >重置为待确认</button>
            <label class="switch" :title="h.status === 'trusted' ? '启用/禁用该钩子' : '信任后可用'">
              <input
                type="checkbox"
                data-test="hook-toggle-switch"
                :checked="h.disabled !== true"
                :disabled="h.status !== 'trusted'"
                @change="onToggleHookDisabled(h, ($event.target as HTMLInputElement).checked)"
              />
              <span class="switch-track"></span>
            </label>
          </div>
        </div>
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue';
import AppIcon from './AppIcon.vue';
import { useAppStore } from '../stores/appStore';
import type { HookInfo } from '../types/pywebview';

const store = useAppStore();

// C12：Hook 信任状态文案（未知状态回退原文，避免新增状态时静默丢信息）
function hookStatusText(status: HookInfo['status']) {
  switch (status) {
    case 'pending': return '待确认';
    case 'trusted': return '已信任';
    case 'stale': return '配置已变更';
    case 'blocked': return '已拒绝';
    case 'revoked': return '已撤销';
    default: return status;
  }
}

// C13：Hook 手动配置——列表（作用域筛选/关键词搜索）+ 新建/编辑表单双视图
type HookScopeFilterValue = 'all' | 'user' | 'project';
const hookScopeFilter = ref<HookScopeFilterValue>('all');
const hookQuery = ref('');

// 关键词命中 event/command/matcher/status_message/source_path 任一字段即保留
const filteredHooks = computed(() => {
  const kw = hookQuery.value.trim().toLowerCase();
  return store.hooks.filter((h) => {
    if (hookScopeFilter.value !== 'all' && h.scope !== hookScopeFilter.value) return false;
    if (!kw) return true;
    return [h.event, h.command, h.matcher, h.status_message, h.source_path]
      .join(' ')
      .toLowerCase()
      .includes(kw);
  });
});

// 表单三种形态：closed 显示列表；create/edit 时表单替换列表
const hookForm = reactive({
  mode: 'closed' as 'closed' | 'create' | 'edit',
  editing: null as HookInfo | null,
  event: 'PreToolUse',
  scope: 'user' as 'user' | 'project',
  matcher: '',
  command: '',
  timeoutSec: 60,
  statusMessage: '',
  saving: false,
});

function openHookCreate() {
  hookForm.mode = 'create';
  hookForm.editing = null;
  hookForm.event = 'PreToolUse';
  hookForm.scope = 'user';
  hookForm.matcher = '';
  hookForm.command = '';
  hookForm.timeoutSec = 60;
  hookForm.statusMessage = '';
  hookForm.saving = false;
}

function openHookEditor(h: HookInfo) {
  hookForm.mode = 'edit';
  hookForm.editing = h;
  hookForm.event = h.event;
  hookForm.scope = h.scope === 'project' ? 'project' : 'user';
  // matcher 为 "*"（匹配全部）时表单显示为空，保存时归一化回 "*"
  hookForm.matcher = h.matcher === '*' ? '' : h.matcher;
  hookForm.command = h.command;
  hookForm.timeoutSec = Math.max(1, Math.round((h.timeout_ms || 60000) / 1000));
  hookForm.statusMessage = h.status_message || '';
  hookForm.saving = false;
}

function closeHookForm() {
  hookForm.mode = 'closed';
  hookForm.editing = null;
  hookForm.saving = false;
}

// 保存：编辑附 source_path + digest（后端定位口径），新建附 scope（写入用户级/项目级文件）
async function submitHookForm() {
  const command = hookForm.command.trim();
  if (!command) {
    store.notifyModal('表单校验', '请填写要执行的命令');
    return;
  }
  const timeoutSec = Number(hookForm.timeoutSec);
  if (!Number.isFinite(timeoutSec) || timeoutSec < 1 || timeoutSec > 300) {
    store.notifyModal('表单校验', '超时时间需为 1-300 秒之间的数字');
    return;
  }
  const payload: Record<string, any> = {
    event: hookForm.event,
    matcher: hookForm.matcher.trim() || '*',
    command,
    timeout_ms: Math.round(timeoutSec * 1000),
    status_message: hookForm.statusMessage.trim(),
  };
  if (hookForm.mode === 'edit' && hookForm.editing) {
    payload.source_path = hookForm.editing.source_path;
    payload.digest = hookForm.editing.digest;
  } else {
    payload.scope = hookForm.scope;
  }
  hookForm.saving = true;
  try {
    const res = await store.saveHook(payload);
    if (res.ok) {
      closeHookForm();
    } else {
      store.notifyModal('保存钩子', res.msg || '未知错误');
    }
  } finally {
    hookForm.saving = false;
  }
}

// 删除钩子（破坏性）：先复用项目统一的 askConfirm 二次确认，再调 store action
async function onDeleteHook() {
  const h = hookForm.editing;
  if (!h) return;
  const ok = await store.askConfirm(
    '删除钩子',
    h.event,
    `将从配置文件中删除该钩子：${h.command}`,
    '删除',
  );
  if (!ok) return;
  const res = await store.deleteHook(h.source_path, h.digest);
  if (res.ok) {
    closeHookForm();
  } else {
    store.notifyModal('删除钩子', res.msg || '未知错误');
  }
}

// 启停开关：checked = 启用，取反得到后端的 disabled 语义
async function onToggleHookDisabled(h: HookInfo, checked: boolean) {
  const res = await store.setHookDisabled(h.source_path, h.digest, !checked);
  if (!res.ok) store.notifyModal('钩子启停', res.msg || '未知错误');
}

// 进入钩子分区时刷新一次最新状态（组件随分区 v-if 挂载，挂载即代表分区可见）
onMounted(() => {
  store.refreshHooks();
});
</script>

<style scoped>
.hooks-settings {
  width: 100%;
  max-width: 860px;
  margin: 0 auto;
}
.sub-toolbar {
  margin: 0 0 14px;
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 10px;
}
.toolbar-spacer {
  flex: 1;
}
/* 来源分段筛选：全部 / 用户 / 项目 */
.src-filter {
  display: inline-flex;
  align-items: center;
  background: var(--bg2);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 2px;
  gap: 2px;
}
.src-filter .seg-btn {
  border: none;
  background: transparent;
  color: var(--text-dim);
  font-size: 12px;
  line-height: 1;
  padding: 5px 12px;
  border-radius: 6px;
  cursor: pointer;
  transition: 0.15s;
  font-family: inherit;
}
.src-filter .seg-btn:hover {
  color: var(--text);
}
.src-filter .seg-btn.active {
  background: var(--accent-soft);
  color: var(--accent);
  font-weight: 600;
}
.sub-empty {
  color: var(--text-faint);
  font-size: 12.5px;
  padding: 20px 0;
  text-align: center;
}
.pl-src {
  font-size: 10px;
  padding: 0 6px;
  border-radius: 6px;
  margin-left: 4px;
  background: var(--bg3);
  color: var(--text-dim);
  font-weight: 400;
}
.pl-src.user {
  background: var(--accent-soft);
  color: var(--accent);
}
.chip-btn {
  background: var(--bg3);
  border: 1px solid var(--border);
  color: var(--text-dim);
  border-radius: 6px;
  padding: 4px 10px;
  font-size: 12px;
  cursor: pointer;
  transition: 0.15s;
  display: inline-flex;
  align-items: center;
}
.chip-btn:hover {
  border-color: var(--text-faint);
  color: var(--text);
}
.chip-btn:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}
.btn {
  border-radius: 8px;
  padding: 6px 14px;
  font-size: 12.5px;
  border: 1px solid var(--border);
  background: var(--bg3);
  color: var(--text);
  transition: 0.15s;
  display: inline-flex;
  align-items: center;
  cursor: pointer;
}
.btn:hover {
  border-color: var(--text-faint);
}
.btn:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}
.btn.primary {
  background: var(--accent);
  border: none;
  color: #fff;
  font-weight: 600;
}
/* 开关 Pill */
.switch {
  position: relative;
  display: inline-block;
  width: 34px;
  height: 18px;
  margin-left: auto;
  cursor: pointer;
  flex-shrink: 0;
}
.switch input {
  opacity: 0;
  width: 0;
  height: 0;
  position: absolute;
}
.switch-track {
  position: absolute;
  inset: 0;
  background: var(--bg3);
  border: 1px solid var(--border);
  border-radius: 99px;
  transition: 0.15s;
}
.switch-track::before {
  content: '';
  position: absolute;
  width: 12px;
  height: 12px;
  left: 2px;
  top: 2px;
  background: var(--text-faint);
  border-radius: 50%;
  transition: 0.15s;
}
.switch input:checked + .switch-track {
  background: var(--accent);
  border-color: transparent;
}
.switch input:checked + .switch-track::before {
  transform: translateX(16px);
  background: #fff;
}

/* 钩子搜索框 / 单列列表 / 行卡片 */
.hook-search {
  background: var(--bg2);
  border: 1px solid var(--border);
  border-radius: 7px;
  padding: 6px 10px;
  color: var(--text);
  font-size: 12px;
  width: 200px;
  outline: none;
  font-family: inherit;
  transition: 0.15s;
}
.hook-search:focus {
  border-color: var(--accent);
}
.hook-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.hook-row {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 12px 14px;
  background: var(--bg1);
  border: 1px solid var(--border);
  border-radius: 12px;
  cursor: pointer;
  transition: 0.15s;
}
.hook-row:hover {
  border-color: var(--text-faint);
}
.is-disabled .hr-cmd {
  color: var(--text-faint);
  text-decoration: line-through;
}
.hr-ico {
  width: 34px;
  height: 34px;
  border-radius: 10px;
  background: var(--bg2);
  border: 1px solid var(--border);
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--text-dim);
  flex-shrink: 0;
}
.hr-main {
  flex: 1;
  min-width: 0;
}
.hr-line {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
}
.hr-event {
  font-size: 13px;
  font-weight: 600;
  white-space: nowrap;
}
.hr-cmd {
  font-family: var(--mono);
  font-size: 11.5px;
  color: var(--text-dim);
  margin-top: 3px;
  overflow: hidden;
  white-space: nowrap;
  text-overflow: ellipsis;
}
.hr-meta {
  font-size: 10.5px;
  color: var(--text-faint);
  font-family: var(--mono);
  margin-top: 3px;
  overflow: hidden;
  white-space: nowrap;
  text-overflow: ellipsis;
}
.hr-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
}
/* 行内开关紧跟操作按钮，不需要卡片布局的自动右推 */
.hr-actions .switch {
  margin-left: 0;
}
.hook-row .sc-trust-btn {
  padding: 1px 8px;
  font-size: 10px;
  white-space: nowrap;
  vertical-align: middle;
}
.hook-empty {
  border: 1px dashed var(--border);
  border-radius: 12px;
  padding: 44px 24px;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 10px;
  text-align: center;
}
.he-title {
  font-size: 13.5px;
  font-weight: 600;
}
.he-desc {
  font-size: 12px;
  color: var(--text-faint);
  line-height: 1.7;
  max-width: 460px;
}

/* 钩子状态徽标：按 status 分色 */
.hook-status {
  margin-left: auto;
  flex-shrink: 0;
  font-size: 10.5px;
  padding: 1px 8px;
  border-radius: 6px;
  background: var(--bg3);
  color: var(--text-dim);
}
.hook-status.trusted {
  background: var(--accent-soft);
  color: var(--accent);
}
.hook-status.pending,
.hook-status.stale {
  background: rgba(240, 136, 62, 0.15);
  color: var(--orange);
}
.hook-status.blocked {
  background: rgba(248, 81, 73, 0.12);
  color: var(--red);
}
.hook-status.revoked {
  color: var(--text-faint);
}
.hook-status.disabled-badge {
  background: var(--bg3);
  color: var(--text-faint);
}

/* 钩子新建/编辑表单 */
.hook-form-page {
  width: 100%;
}
.hook-back {
  background: transparent;
  border: none;
  color: var(--text-dim);
  font-size: 12px;
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 4px 0;
  cursor: pointer;
  font-family: inherit;
}
.hook-back:hover {
  color: var(--accent);
}
.hook-form-head h3 {
  font-size: 14px;
  margin: 8px 0 4px;
}
.hook-form-head p {
  font-size: 12px;
  color: var(--text-faint);
}
.hook-form-card {
  margin-top: 14px;
  background: var(--bg1);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 16px;
}
.hf-row {
  display: flex;
  gap: 12px;
  margin-bottom: 12px;
}
.hf-field {
  display: flex;
  flex-direction: column;
  gap: 5px;
  flex: 1;
  min-width: 0;
}
.hf-label {
  font-size: 11.5px;
  color: var(--text-dim);
}
.hf-field input,
.hf-field select {
  background: var(--bg2);
  border: 1px solid var(--border);
  border-radius: 7px;
  padding: 7px 10px;
  color: var(--text);
  font-size: 12.5px;
  outline: none;
  font-family: inherit;
  transition: 0.15s;
}
.hf-field input:focus,
.hf-field select:focus {
  border-color: var(--accent);
  box-shadow: 0 0 0 2px var(--accent-soft);
}
.hf-field input.hf-mono {
  font-family: var(--mono);
}
.hf-hint {
  font-size: 10.5px;
  color: var(--text-faint);
  line-height: 1.5;
}
.hf-readonly {
  font-size: 12.5px;
  color: var(--text-dim);
  padding: 7px 0;
}
.hf-advanced {
  border-top: 1px solid var(--border);
  padding-top: 12px;
  margin-bottom: 12px;
}
.hf-advanced summary {
  cursor: pointer;
  font-size: 12px;
  color: var(--text-dim);
  user-select: none;
}
.hf-advanced summary:hover {
  color: var(--text);
}
.hf-advanced[open] summary {
  margin-bottom: 12px;
}
.hf-advanced .hf-row:last-child {
  margin-bottom: 0;
}
.hf-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  border-top: 1px solid var(--border);
  padding-top: 12px;
}
.hf-delete {
  background: transparent;
  border: none;
  color: var(--red);
  font-size: 12.5px;
  cursor: pointer;
  padding: 6px 0;
  font-family: inherit;
}
.hf-delete:hover {
  text-decoration: underline;
}
</style>

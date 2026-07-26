<template>
  <div class="msg" :class="[msg.role === 'user' ? 'msg-user' : 'msg-assistant']">
    <!-- 用户消息 -->
    <div v-if="msg.role === 'user'" class="bubble user-bubble">
      {{ msg.content }}
    </div>

    <!-- 助手消息 -->
    <div v-else-if="msg.role === 'assistant'" class="avatar-row">
      <div class="avatar">✦</div>
      <div class="assistant-body">
        <div class="assistant-name">
          <b>星瑶</b> · <span style="font-size:11px;color:var(--text-faint)">{{ store.activeModelName || '模型' }}</span>
          <span v-if="msg.cancelled" class="cancel-badge">已取消</span>
        </div>

        <!-- 思考过程 Block -->
        <div v-if="msg.reasoning_content && msg.reasoning_content.trim()" class="think-block">
          <div class="tk-head" @click="thinkingOpen = !thinkingOpen">
            <span>💭 思考过程</span>
            <span style="margin-left:auto">{{ thinkingOpen ? '▾' : '▸' }}</span>
          </div>
          <div v-show="thinkingOpen" class="tk-cnt">
            {{ msg.reasoning_content }}
          </div>
        </div>

        <!-- 工具卡片列表 -->
        <div v-if="msg.tool_calls && msg.tool_calls.length > 0">
          <div
            v-for="t in normalizedToolCalls"
            :key="t.id"
            class="tool-card"
            :class="{ open: openTools[t.id], err: t.result && t.result.ok === false }"
          >
            <div class="tool-head" @click="toggleToolCard(t.id)">
              <span class="tico">{{ getToolIcon(t.name) }}</span>
              <span class="tname">{{ (t.result && t.result.summary) || t.label }}</span>

              <span class="badge" :class="t.result ? (t.result.ok === false ? 'err' : 'ok') : 'run'">
                <template v-if="t.result">
                  {{ t.result.ok === false ? '✕ 出错' : '✓ 完成' }}
                </template>
                <template v-else>
                  <span class="spin"></span> 运行中
                </template>
              </span>

              <span v-if="canExpand(t)" class="tool-toggle">{{ openTools[t.id] ? '收起 ▾' : '展开 ▸' }}</span>
            </div>

            <pre v-if="openTools[t.id] && t.result && t.result.result" class="tool-body">{{ t.result.result }}</pre>

            <!-- 运行中的子代理：展开显示每个子代理的实时阶段与耗时 -->
            <div v-if="openTools[t.id] && !t.result && t.name === 'run_subagent'" class="tool-body sub-live">
              <div v-if="store.subagentTasks.length === 0" class="sub-live-empty">子代理正在启动…</div>
              <div v-for="st in store.subagentTasks" :key="st.id" class="sub-live-row">
                <span class="sub-live-stage" :class="subStageClass(st)">{{ subStageLabel(st) }}</span>
                <span class="sub-live-desc" :title="st.description">{{ st.description }}</span>
                <span v-if="st.stage_detail" class="sub-live-detail">{{ st.stage_detail }}</span>
                <span class="sub-live-time">{{ st.elapsed }}s</span>
              </div>
            </div>
          </div>
        </div>

        <!-- 文件变更 Diff 列表 -->
        <div v-if="msg.diffs && msg.diffs.length > 0" class="diff-list">
          <div
            v-for="(d, di) in msg.diffs"
            :key="di"
            class="diff-card"
            :class="{ open: openDiffs[di] }"
          >
            <div class="diff-head" @click="toggleDiff(di)">
              <span class="tico">📝</span>
              <span class="tname">{{ d.file_path }}</span>
              <span class="tool-toggle">{{ openDiffs[di] ? '收起 ▾' : '展开 ▸' }}</span>
            </div>
            <div v-if="openDiffs[di]" class="diff-body">
              <div
                v-for="(line, li) in splitDiffLines(d.diff_text)"
                :key="li"
                class="diff-line"
                :class="diffLineClass(line)"
              >{{ line }}</div>
            </div>
          </div>
        </div>

        <!-- Markdown 文本内容 -->
        <div v-if="msg.content && msg.content.trim()" class="md-body" v-html="renderMarkdown(msg.content)"></div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed } from 'vue';
import { useAppStore, type ChatMessage } from '../stores/appStore';
import { renderMarkdown } from '../utils/markdown';

const props = defineProps<{
  msg: ChatMessage;
}>();

const store = useAppStore();

// 思考块默认跟随全局 store.showThinking；用户在本条消息上手动切换过一次后，
// 该条消息就"钉住"为用户选择的状态，不再随全局按钮变化。
const localThinkOverride = ref<boolean | null>(null);
const thinkingOpen = computed({
  get: () => (localThinkOverride.value !== null ? localThinkOverride.value : store.showThinking),
  set: (v: boolean) => { localThinkOverride.value = v; },
});

const openTools = ref<Record<string, boolean>>({});
const openDiffs = ref<Record<number, boolean>>({});

function toggleToolCard(id: string) {
  openTools.value[id] = !openTools.value[id];
}

// 完成后有结果文本可展开；运行中的 run_subagent 也可展开查看实时进度
function canExpand(t: { name: string; result: any }) {
  if (t.result) return !!t.result.result;
  return t.name === 'run_subagent';
}

const SUB_STAGE_LABELS: Record<string, string> = {
  queued: '排队中',
  model: '调用模型',
  thinking: '思考中',
  tool: '调用工具',
  running: '执行中',
  waiting_approval: '等待授权',
  composing: '生成回复',
  summarizing: '总结中',
  finalizing: '收尾中',
  completed: '已完成',
  failed: '失败',
  timed_out: '超时',
  cancelled: '已取消',
};
const SUB_TERMINAL = new Set(['completed', 'failed', 'timed_out', 'cancelled']);

function subStageLabel(st: { status: string; stage: string }) {
  const key = SUB_TERMINAL.has(st.status) ? st.status : st.stage;
  return SUB_STAGE_LABELS[key] || key || '运行中';
}

function subStageClass(st: { status: string; stage: string }) {
  if (st.status === 'completed') return 'ok';
  if (st.status === 'failed' || st.status === 'timed_out' || st.status === 'cancelled') return 'err';
  if (st.stage === 'waiting_approval') return 'warn';
  return 'run';
}

function toggleDiff(idx: number) {
  openDiffs.value[idx] = !openDiffs.value[idx];
}

const TOOL_ICONS: Record<string, string> = {
  read_file: '📄',
  write_file: '✏️',
  edit_file: '📝',
  search_code: '🔍',
  execute_command: '⚡',
  run_subagent: '🤖',
  plan_write: '📋',
  plan_update: '📋',
  list_directory: '📂',
  glob_files: '📂',
  fetch_url: '🌐',
};

function getToolIcon(name: string) {
  return TOOL_ICONS[name] || '🔧';
}

// 兼容两种 tool_call 形状：
// - 实时事件（新契约）：{ id, name, label }
// - 历史会话消息（switch_session 返回，形状未变）：{ id, function: { name, arguments }, summary }
function normalizeToolCall(call: any): { id: string; name: string; label: string } {
  const name = call.name || (call.function && call.function.name) || call.tool_name || 'tool';
  let label = call.label || call.summary || '';
  if (!label) {
    const args = call.function && call.function.arguments;
    if (args && typeof args === 'object') {
      const p = args.file_path || args.path || args.query || args.command || '';
      label = p ? `${name} ${p}` : name;
    } else {
      label = name;
    }
  }
  return { id: call.id, name, label };
}

function getToolResult(id: string): any {
  if (id && store.toolResults[id]) return store.toolResults[id];
  if (id) {
    const found = store.messages.find(m => m.role === 'tool' && m.tool_call_id === id);
    if (found) {
      return { result: found.content, summary: found.content, ok: true, name: found.name };
    }
  }
  return null;
}

const normalizedToolCalls = computed(() => {
  const calls = props.msg.tool_calls;
  if (!calls) return [];
  return calls.map((call: any) => {
    const norm = normalizeToolCall(call);
    return { ...norm, result: getToolResult(norm.id) };
  });
});

function splitDiffLines(text: string): string[] {
  if (!text) return [];
  return text.split('\n');
}

function diffLineClass(line: string) {
  if (line.startsWith('+++') || line.startsWith('---') || line.startsWith('@@') || line.startsWith('diff ') || line.startsWith('index ')) return 'hdr';
  if (line.startsWith('+')) return 'add';
  if (line.startsWith('-')) return 'del';
  return '';
}
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
  background: linear-gradient(135deg, #274060, #233a63);
  border: 1px solid #33507e;
  border-radius: 12px 12px 4px 12px;
  padding: 10px 14px;
  max-width: 70%;
  line-height: 1.65;
}
.avatar-row {
  display: flex;
  gap: 10px;
}
.avatar {
  width: 28px;
  height: 28px;
  border-radius: 8px;
  background: linear-gradient(135deg, var(--accent), var(--accent2));
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 14px;
  flex-shrink: 0;
  color: #fff;
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
  color: #b8a5d6;
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

.tool-card {
  background: var(--bg1);
  border: 1px solid var(--border);
  border-radius: 9px;
  margin: 7px 0;
  overflow: hidden;
}
.tool-card.err {
  border-left: 3px solid var(--red);
}
.tool-head {
  padding: 8px 12px;
  display: flex;
  align-items: center;
  gap: 8px;
  cursor: pointer;
  font-size: 12.5px;
}
.tool-head:hover {
  background: var(--bg2);
}
.tico {
  font-size: 14px;
}
.tname {
  font-weight: 600;
  color: var(--text);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.tool-toggle {
  color: var(--text-faint);
  font-size: 11px;
  flex-shrink: 0;
}
.badge {
  margin-left: auto;
  font-size: 11px;
  border-radius: 99px;
  padding: 2px 8px;
  display: flex;
  align-items: center;
  gap: 5px;
  flex-shrink: 0;
}
.badge.run {
  background: rgba(240, 136, 62, 0.15);
  color: var(--orange);
}
.badge.ok {
  background: rgba(63, 185, 80, 0.15);
  color: var(--green);
}
.badge.err {
  background: rgba(248, 81, 73, 0.15);
  color: var(--red);
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

.tool-body {
  display: block;
  margin: 0;
  padding: 8px 12px;
  background: #0a0e14;
  border-top: 1px solid var(--border);
  font-family: var(--mono);
  font-size: 11.5px;
  white-space: pre-wrap;
  color: var(--text-dim);
  max-height: 240px;
  overflow-y: auto;
}

.sub-live {
  font-family: var(--mono);
}
.sub-live-empty {
  color: var(--text-faint);
  font-size: 11.5px;
}
.sub-live-row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 3px 0;
  font-size: 11.5px;
}
.sub-live-stage {
  flex-shrink: 0;
  border-radius: 99px;
  padding: 1px 8px;
  font-size: 10.5px;
}
.sub-live-stage.run {
  background: rgba(240, 136, 62, 0.15);
  color: var(--orange);
}
.sub-live-stage.warn {
  background: rgba(210, 153, 34, 0.18);
  color: var(--orange);
}
.sub-live-stage.ok {
  background: rgba(63, 185, 80, 0.15);
  color: var(--green);
}
.sub-live-stage.err {
  background: rgba(248, 81, 73, 0.15);
  color: var(--red);
}
.sub-live-desc {
  color: var(--text);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.sub-live-detail {
  color: var(--text-faint);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  max-width: 200px;
}
.sub-live-time {
  margin-left: auto;
  flex-shrink: 0;
  color: var(--text-faint);
}

.diff-list {
  margin: 7px 0;
}
.diff-card {
  background: var(--bg1);
  border: 1px solid var(--border);
  border-radius: 9px;
  margin: 7px 0;
  overflow: hidden;
}
.diff-head {
  padding: 8px 12px;
  display: flex;
  align-items: center;
  gap: 8px;
  cursor: pointer;
  font-size: 12.5px;
}
.diff-head:hover {
  background: var(--bg2);
}
.diff-body {
  font-family: var(--mono);
  font-size: 11.5px;
  background: #0a0e14;
  border-top: 1px solid var(--border);
  max-height: 280px;
  overflow-y: auto;
  line-height: 1.55;
  padding: 4px 0;
}
.diff-line {
  padding: 1px 12px;
  white-space: pre;
  color: var(--text-dim);
}
.diff-line.add {
  background: rgba(63, 185, 80, 0.12);
  color: var(--green);
}
.diff-line.del {
  background: rgba(248, 81, 73, 0.12);
  color: var(--red);
}
.diff-line.hdr {
  color: var(--text-faint);
}

.md-body {
  margin-top: 8px;
  line-height: 1.7;
}
</style>

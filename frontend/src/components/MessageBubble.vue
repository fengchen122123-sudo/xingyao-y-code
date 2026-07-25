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
          <b>星瑶</b> · <span style="font-size:11px;color:var(--text-faint)">{{ store.currentModel }}</span>
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
            v-for="(call, idx) in msg.tool_calls" 
            :key="call.id || idx" 
            class="tool-card"
            :class="{ open: openTools[call.id || idx] }"
          >
            <div class="tool-head" @click="toggleToolCard(call.id || idx)">
              <span class="tico">{{ getToolMeta(call).icon }}</span>
              <span class="tname">{{ getToolMeta(call).label }}</span>
              <span class="targ">{{ getToolMeta(call).detail || getArgsDisplay(extractToolInfo(call).args) }}</span>
              
              <span 
                class="badge" 
                :class="[getToolResult(call.id) ? (getToolResult(call.id)?.content?.startsWith('🚨') ? 'err' : 'ok') : 'run']"
              >
                <template v-if="getToolResult(call.id)">
                  {{ getToolResult(call.id)?.content?.startsWith('🚨') ? '✕ 出错' : '✓ 完成' }}
                </template>
                <template v-else>
                  <span class="spin"></span> 运行中
                </template>
              </span>
            </div>

            <div class="tool-body">
              {{ getToolResult(call.id) ? getToolResult(call.id)?.content : getExecutingDescription(call) }}
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
import { ref } from 'vue';
import { useAppStore, type ChatMessage } from '../stores/appStore';
import { renderMarkdown } from '../utils/markdown';

const props = defineProps<{
  msg: ChatMessage;
}>();

const store = useAppStore();
const thinkingOpen = ref(true);
const openTools = ref<Record<string, boolean>>({});

function toggleToolCard(key: string | number) {
  openTools.value[key] = !openTools.value[key];
}

function extractToolInfo(call: any) {
  let name = '';
  let args: any = {};

  if (call.function) {
    if (typeof call.function === 'string') {
      name = call.function;
    } else {
      name = call.function.name || '';
      args = call.function.arguments || {};
    }
  }

  if (!name) {
    name = call.name || call.tool_name || call.function_name || '';
  }
  if (!args || (typeof args === 'object' && Object.keys(args).length === 0)) {
    args = call.args || call.arguments || {};
  }

  if (typeof args === 'string') {
    try {
      args = JSON.parse(args);
    } catch (e) {
      // 保留原始字符串
    }
  }

  return { name: name || 'tool', args };
}

function getArgsDisplay(args: any) {
  if (!args) return '';
  if (typeof args === 'string') return args;
  try {
    const s = JSON.stringify(args);
    return s.length > 50 ? s.slice(0, 47) + '...' : s;
  } catch (e) {
    return String(args);
  }
}

function getToolMeta(call: any) {
  const { name, args } = extractToolInfo(call);
  const summary = call.summary || '';

  if (summary && summary.trim()) {
    const match = summary.match(/^([\uD800-\uDBFF][\uDC00-\uDFFF]|[\u2600-\u27BF]|\S+)\s+(.*)$/);
    if (match) {
      return { icon: match[1], label: match[2], detail: '' };
    }
  }

  const p = typeof args === 'object' && args ? args : {};
  switch (name) {
    case 'read_file':
      return { icon: '📄', label: '读取文件', detail: p.file_path || p.path || '' };
    case 'write_file':
      return { icon: '✏️', label: '写入文件', detail: p.file_path || p.path || '' };
    case 'edit_file':
      return { icon: '📝', label: '编辑文件', detail: p.file_path || p.path || '' };
    case 'search_code':
      return { icon: '🔍', label: '代码检索', detail: p.query || '' };
    case 'execute_command':
      return { icon: '⚡', label: '执行 Shell 命令', detail: p.command || p.cmd || '' };
    case 'run_subagent':
      return { icon: '🤖', label: '启动子 Agent', detail: p.task_description || p.prompt || p.task || '' };
    case 'plan_write':
    case 'plan_update':
      return { icon: '📋', label: '编制任务计划', detail: '' };
    case 'list_directory':
    case 'glob_files':
      return { icon: '📂', label: '浏览文件树', detail: p.dir_path || p.pattern || p.path || '' };
    case 'fetch_url':
      return { icon: '🌐', label: '请求 Web 页面', detail: p.url || '' };
    default:
      return { icon: '🔧', label: (name && name !== 'tool') ? name : '执行工具指令', detail: '' };
  }
}

function getExecutingDescription(call: any) {
  const { name, args } = extractToolInfo(call);
  const p = typeof args === 'object' && args ? args : {};
  switch (name) {
    case 'read_file':
      return `📄 正在读取文件: ${p.file_path || p.path || '项目文件'}`;
    case 'write_file':
      return `✏️ 正在写入文件: ${p.file_path || p.path || '项目文件'}`;
    case 'edit_file':
      return `📝 正在修改文件: ${p.file_path || p.path || '项目文件'}`;
    case 'search_code':
      return `🔍 正在检索代码: ${p.query || '关键字'}`;
    case 'execute_command':
      return `⚡ 正在执行 Shell 命令: ${p.command || p.cmd || ''}`;
    case 'run_subagent':
      return `🤖 正在启动子 Agent 代理分析: ${p.task_description || p.prompt || p.task || '任务'}`;
    case 'plan_write':
    case 'plan_update':
      return `📋 正在更新与编制任务计划...`;
    case 'list_directory':
    case 'glob_files':
      return `📂 正在扫描目录结构: ${p.dir_path || p.pattern || '.'}`;
    case 'fetch_url':
      return `🌐 正在请求网页内容: ${p.url || ''}`;
    default:
      return `🔧 正在执行工具指令 (${name})...`;
  }
}

function getToolResult(id: string) {
  if (id && store.toolResults[id]) return store.toolResults[id];
  if (id) {
    const found = store.messages.find(m => m.role === 'tool' && m.tool_call_id === id);
    if (found) {
      return { content: found.content || '✓ 完成', name: found.name };
    }
  }
  return null;
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
}
.targ {
  color: var(--text-faint);
  font-family: var(--mono);
  font-size: 11.5px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  max-width: 320px;
}
.badge {
  margin-left: auto;
  font-size: 11px;
  border-radius: 99px;
  padding: 2px 8px;
  display: flex;
  align-items: center;
  gap: 5px;
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
  padding: 8px 12px;
  background: #0a0e14;
  border-top: 1px solid var(--border);
  font-family: var(--mono);
  font-size: 11.5px;
  white-space: pre-wrap;
  color: var(--text-dim);
  max-height: 200px;
  overflow-y: auto;
}

.md-body {
  margin-top: 8px;
  line-height: 1.7;
}
</style>

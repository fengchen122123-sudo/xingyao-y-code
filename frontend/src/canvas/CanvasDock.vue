<template>
  <!-- 画布内嵌对话坞：收起态是一条输入条，聚焦/点击展开成带最近消息的面板。
       为什么用 absolute：它挂在右面板分区（data-theme-region）内部，分区开了
       isolation，fixed 会落到分区层叠上下文之外，被相邻分区的伪元素盖住 -->
  <div class="cd" :class="{ 'is-open': open }" data-test="canvas-dock">
    <!-- 展开态头部：标题 + 流式速度 + 收起按钮 -->
    <div v-if="open" class="cd-head">
      <span class="cd-title">画布对话</span>
      <span v-if="busy" class="cd-chip">{{ speedText }}</span>
      <span class="cd-spacer"></span>
      <button class="cd-icon" data-test="canvas-dock-close" title="收起" @click="collapse">×</button>
    </div>

    <!-- 消息区：只渲染最近 20 条。画布坞是旁路视图，没有翻历史诉求，
         全量渲染长会话会拖慢画布拖拽（每次流式更新都要 diff 上千节点） -->
    <div v-if="open" ref="bodyRef" class="cd-body" data-test="canvas-dock-body">
      <div v-for="row in rows" :key="row.key" class="cd-msg" :class="`is-${row.role}`">
        <span class="cd-role">{{ row.roleLabel }}</span>
        <div class="cd-text">{{ row.content }}</div>
      </div>

      <!-- 流式内容只 watch 全局 store 字段；绝不在本组件里注册 window.onBridgeEvent，
           appStore 已有唯一消费者，抢注册会让主对话区丢流 -->
      <div v-if="thinking || streaming" class="cd-msg is-assistant">
        <span class="cd-role">助手</span>
        <div>
          <div v-if="thinking" class="cd-think">{{ thinking }}</div>
          <div class="cd-text">{{ streaming }}<i class="cd-caret"></i></div>
        </div>
      </div>

      <div v-if="!rows.length && !streaming && !thinking" class="cd-empty">
        在画布上直接问一句，回答与主对话区共用同一个会话
      </div>
    </div>

    <!-- 输入行：收起态就是这 40px 的一条；Enter 发送、Shift+Enter 换行 -->
    <div class="cd-input">
      <textarea
        ref="inputRef"
        v-model="draft"
        class="cd-textarea"
        :rows="draftRows"
        data-test="canvas-dock-input"
        :placeholder="open ? '继续追问… (Enter 发送 / Shift+Enter 换行)' : '问问画布上的内容…'"
        @focus="expand"
        @keydown="onKeydown"
      ></textarea>
      <button
        class="cd-send"
        :class="{ 'is-stop': busy }"
        :disabled="!busy && !canSend"
        :data-test="busy ? 'canvas-dock-stop' : 'canvas-dock-send'"
        :title="busy ? '停止当前回合' : '发送'"
        @click="onAction"
      >{{ busy ? '停止' : '发送' }}</button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue';
import { useAppStore } from '../stores/appStore';

// 复用全局会话 store：消息、流式文本、running 都由它单一维护。
// 例外：不在这里注册桥事件消费者（见模板注释），组件只做只读视图 + 发消息
const store = useAppStore();

/** 收起/展开：收起态只留输入条，展开态才渲染消息区（省掉未打开时的渲染开销） */
const open = ref(false);
const draft = ref('');
const bodyRef = ref<HTMLElement | null>(null);
const inputRef = ref<HTMLTextAreaElement | null>(null);

/** 只渲染最近 20 条：旁路视图不做历史浏览 */
const MAX_ROWS = 20;

const ROLE_LABEL: Record<string, string> = {
  user: '我',
  assistant: '助手',
  tool: '工具',
  error: '错误',
  notice: '提示',
};

interface DockRow {
  key: string;
  role: string;
  roleLabel: string;
  content: string;
}

const rows = computed<DockRow[]>(() => {
  const all = store.messages;
  const start = Math.max(0, all.length - MAX_ROWS);
  const out: DockRow[] = [];
  for (let i = start; i < all.length; i++) {
    const m = all[i];
    const text = (m.content || '').trim();
    // 工具消息常常只有 name：退化成一行标签保住上下文；完全空的消息整条跳过
    const content = text || (m.role === 'tool' ? m.name || '工具调用' : '');
    if (!content) continue;
    out.push({
      // id 可能缺省（本地乐观消息），索引兜底保证 key 稳定且唯一
      key: m.id || `i-${i}`,
      role: m.role,
      roleLabel: ROLE_LABEL[m.role] || m.role,
      content,
    });
  }
  return out;
});

const streaming = computed(() => store.currentStreamingText);
const thinking = computed(() => store.currentThinkingText);
const busy = computed(() => store.running);
const speedText = computed(() => {
  const s = store.streamingSpeed;
  return s > 0 ? `${s.toFixed(1)} t/s` : '生成中';
});

const canSend = computed(() => draft.value.trim().length > 0);
/** 展开后最多 4 行，收起态锁 1 行——收起时高度必须稳在 40px 那条输入条 */
const draftRows = computed(() =>
  open.value ? Math.min(4, Math.max(1, draft.value.split('\n').length)) : 1,
);

function expand() {
  open.value = true;
}

function collapse() {
  open.value = false;
  inputRef.value?.blur();
}

function submit() {
  const text = draft.value.trim();
  if (!text) return;
  draft.value = '';
  // 统一走 store.sendMessage：后端的排队/会话绑定/乐观渲染都由它负责，
  // 画布坞不直连 pywebview，避免第二套发送链路
  void store.sendMessage(text);
}

async function onAction() {
  if (busy.value) {
    await store.stopCurrentTurn();
    return;
  }
  submit();
}

function onKeydown(e: KeyboardEvent) {
  // isComposing：中文输入法选词时的 Enter 不能当发送，否则会把未上屏的拼音发出去
  if (e.key !== 'Enter' || e.shiftKey || e.isComposing) return;
  e.preventDefault();
  // 运行中不本地拦截：sendMessage 会转成排队消息，本轮结束后自动补发
  submit();
}

// 新消息/流式片段到达时贴底：只在展开态滚动，收起态不动用户视野
watch([() => store.messages.length, streaming, thinking], async () => {
  if (!open.value) return;
  await nextTick();
  const el = bodyRef.value;
  if (el) el.scrollTop = el.scrollHeight;
});
</script>

<style scoped>
/* 收起态 ~40px 一条；展开态 380px 面板。宽 520px 是有意比画布窄很多：
   坞是浮层，越窄挡住的节点越少。颜色全部走全局主题变量，跟随主题切换 */
.cd {
  position: absolute;
  left: 50%;
  bottom: 16px;
  transform: translateX(-50%);
  /* 高于右下角自绘缩放控件（z-index: 2），重叠时坞在上层 */
  z-index: 3;
  display: flex;
  flex-direction: column;
  width: 520px;
  max-width: calc(100% - 28px);
  border: 1px solid var(--border);
  border-radius: 10px;
  /* color-mix 让面板略透出画布点阵，视觉上像"浮在画布上"而不是切掉一块 */
  background: color-mix(in srgb, var(--bg1) 94%, transparent);
  box-shadow: 0 12px 28px color-mix(in srgb, var(--border) 60%, transparent);
  backdrop-filter: blur(6px);
  overflow: hidden;
}
/* 展开高度用固定值而非 max-height：面板高度稳定，输入行下面的画布视线不跳 */
.cd.is-open {
  height: 380px;
}

.cd-head {
  display: flex;
  align-items: center;
  gap: 7px;
  flex: 0 0 auto;
  padding: 7px 8px 7px 12px;
  border-bottom: 1px solid var(--border);
}
.cd-title {
  font-family: var(--mono);
  font-size: 10px;
  letter-spacing: 0.7px;
  text-transform: uppercase;
  color: var(--text-faint);
}
/* 速度胶囊：等宽字体保持数字跳动时不横向抖动 */
.cd-chip {
  font-family: var(--mono);
  font-size: 10px;
  color: var(--accent);
  border: 1px solid color-mix(in srgb, var(--accent) 34%, transparent);
  background: var(--accent-soft);
  border-radius: 99px;
  padding: 1px 7px;
}
.cd-spacer {
  flex: 1;
}
.cd-icon {
  width: 20px;
  height: 20px;
  line-height: 1;
  border: 1px solid transparent;
  border-radius: 5px;
  background: transparent;
  color: var(--text-faint);
  font-family: inherit;
  font-size: 15px;
  cursor: pointer;
  transition: 0.15s;
}
.cd-icon:hover {
  border-color: var(--border);
  background: var(--bg3);
  color: var(--text-bright);
}

/* 消息区：flex:1 + min-height:0 才能让内部滚动而不是把输入行顶出面板 */
.cd-body {
  flex: 1 1 auto;
  min-height: 0;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 9px;
  padding: 10px 12px 12px;
}
.cd-body::-webkit-scrollbar {
  width: 8px;
}
.cd-body::-webkit-scrollbar-thumb {
  background: var(--bg3);
  border-radius: 4px;
}

.cd-msg {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  font-size: 12.5px;
  line-height: 1.62;
  color: var(--text-dim);
}
/* 角色标签用等宽小字 + 描边，与视觉稿里节点头部 "kind" 标签同一套观感 */
.cd-role {
  flex: 0 0 auto;
  margin-top: 2px;
  padding: 1px 5px;
  border: 1px solid var(--border);
  border-radius: 4px;
  color: var(--text-faint);
  font-family: var(--mono);
  font-size: 9.5px;
  letter-spacing: 0.3px;
}
.cd-text {
  word-break: break-word;
  white-space: pre-wrap;
}
/* 用户自己的话提亮，一眼区分谁说的 */
.cd-msg.is-user .cd-text {
  color: var(--text-bright);
}
.cd-msg.is-error .cd-role,
.cd-msg.is-error .cd-text {
  color: var(--red);
}
.cd-msg.is-notice .cd-text {
  color: var(--text-faint);
}
/* 思考过程压低对比度、左侧一条竖线，避免和正式回答混在一起 */
.cd-think {
  margin-bottom: 4px;
  padding-left: 8px;
  border-left: 2px solid var(--border);
  color: var(--text-faint);
  font-size: 11.5px;
  white-space: pre-wrap;
  max-height: 96px;
  overflow-y: auto;
}
/* 流式光标：跟着文本末尾闪，表示还在生成 */
.cd-caret {
  display: inline-block;
  width: 2px;
  height: 12px;
  margin-left: 2px;
  vertical-align: -1px;
  background: var(--accent);
  animation: cd-blink 1s steps(2) infinite;
}
@keyframes cd-blink {
  50% {
    opacity: 0;
  }
}
.cd-empty {
  margin: auto;
  color: var(--text-faint);
  font-size: 12px;
  text-align: center;
}

/* 输入行：收起态高度就靠这里撑到约 40px（28px 按钮 + 上下 6px 内边距） */
.cd-input {
  flex: 0 0 auto;
  display: flex;
  align-items: flex-end;
  gap: 6px;
  padding: 6px 8px;
}
.cd.is-open .cd-input {
  border-top: 1px solid var(--border);
}
.cd-textarea {
  flex: 1;
  /* 手动 rows 控制行数，禁掉原生拖拽柄：拖柄会破坏面板固定高度 */
  resize: none;
  border: none;
  outline: none;
  background: transparent;
  color: var(--text);
  font-family: inherit;
  font-size: 12.5px;
  line-height: 1.5;
  padding: 5px 4px 1px;
  max-height: 84px;
  overflow-y: auto;
}
.cd-textarea::placeholder {
  color: var(--text-faint);
}
.cd-send {
  flex: 0 0 auto;
  height: 28px;
  padding: 0 12px;
  border: 1px solid color-mix(in srgb, var(--accent) 48%, transparent);
  border-radius: 6px;
  background: var(--accent-soft);
  color: var(--accent);
  font-family: inherit;
  font-size: 12px;
  cursor: pointer;
  transition: 0.15s;
}
.cd-send:hover:not(:disabled) {
  background: color-mix(in srgb, var(--accent) 26%, transparent);
}
.cd-send:disabled {
  border-color: var(--border);
  background: transparent;
  color: var(--text-faint);
  cursor: default;
}
/* 运行态换成「停止」：用暖色调区分于发送，点击即中断当前回合 */
.cd-send.is-stop {
  border-color: color-mix(in srgb, var(--red) 48%, transparent);
  background: color-mix(in srgb, var(--red) 14%, transparent);
  color: var(--red);
}
</style>
<template>
  <div class="page">
    <van-nav-bar fixed placeholder safe-area-inset-top @click-left="goBack">
      <template #left>
        <span class="nav-back"><svg class="i xxl"><use href="#i-chev-r" /></svg></span>
        <img class="nvlogo" :src="logoUrl" alt="星瑶" />
      </template>
      <template #title>
        <span class="nav-title">{{ sessionTitle }}</span>
      </template>
      <template #right>
        <span class="mx" @click="opsDlg = true"><svg class="i lg"><use href="#i-more" /></svg></span>
      </template>
    </van-nav-bar>
    <div class="projline">
      <span class="mono wd">{{ store.currentWorkdir || '自由对话' }}</span>
      <div class="meter"><i :style="{ width: pctClamped + '%' }" /></div>
      <b class="mono meter-txt">{{ pctClamped }}% · {{ fmtNum(store.tokenUsage.used) }}/{{ store.tokenUsage.window ? fmtNum(store.tokenUsage.window) : '—' }} · 缓存 {{ store.tokenUsage.cached_pct }}%</b>
    </div>

    <div ref="streamEl" class="stream" @scroll="onStreamScroll">
      <div v-if="store.historyHasMore" class="load-older" @click="loadOlder">
        {{ loadingOlder ? '加载中…' : '查看更早消息' }}
      </div>

      <template v-for="(turn, ti) in turns" :key="ti">
        <div v-if="turn.user && ti > 0" class="day-sep"></div>

        <div v-if="turn.user" class="u-row">
          <div class="u-bub">
            <div class="plain">{{ turn.user.content }}</div>
            <div v-if="turn.user.media?.length" class="imgrow">
              <img v-for="(img, i) in turn.user.media" :key="i" class="ph" :src="img.url" @click="previewImg(img.url)" />
            </div>
          </div>
        </div>

        <div v-if="turn.items.length || isTurnLive(turn)" class="a">
          <div class="a-head">
            <span v-if="isTurnLive(turn)" class="live" />
            <img class="a-logo" :src="logoUrl" alt="" />
            <span class="nm">{{ store.agentName }}</span>
            <span class="md mono">{{ turnModel(turn) }}</span>
            <span class="tm">{{ isTurnLive(turn) ? '运行中' : '' }}</span>
          </div>

          <!-- 工具执行行（默认折叠为一行摘要，点开看明细） -->
          <div v-if="turnToolRows(turn).length" class="fw" :class="{ open: logsOpen.has(ti) }">
            <div class="foldhead" @click="toggleLogs(ti)">
              <svg class="i sm chev"><use href="#i-chev-r" /></svg>
              <span>工具 {{ turnCalls(turn).length }} 步 · {{ toolStatus(turn) }}</span>
            </div>
            <div class="foldbody">
              <div class="logs">
                <div v-for="r in turnToolRows(turn)" :key="r.id" class="lg" :class="{ err: r.status === 'failed', live: r.status === 'running' }">
                  <span class="st" :class="'k-' + r.kind" />
                  <span class="tg">{{ r.action }}<template v-if="r.count > 1"> ×{{ r.count }}</template></span>
                  <span class="ar">{{ r.command || r.detail || r.errorSummary }}</span>
                  <span class="rs" :class="{ good: r.status === 'completed' }">{{ rowStatusLabel(r) }}</span>
                </div>
              </div>
            </div>
          </div>

          <!-- 思考段（已完成回放，默认 2 行截断，点标题展开） -->
          <div v-if="turnThinking(turn) && !isTurnLive(turn)" class="tw" :class="{ open: thinkOpen.has(ti) }">
            <div class="think">
              <div class="lb" @click="toggleThink(ti)">思考 <svg class="i sm chev"><use href="#i-chev-r" /></svg></div>{{ turnThinking(turn) }}
            </div>
          </div>

          <!-- 结论 -->
          <div v-for="m in answersOf(turn)" :key="'c' + (m._msg_id ?? m.content?.slice(0, 20))" class="ans">
            <div v-if="m.content"><MdView :source="m.content" /></div>
            <div v-if="m.media?.length" class="imgrow">
              <img v-for="(img, i) in m.media" :key="i" class="ph" :src="img.url" @click="previewImg(img.url)" />
            </div>
          </div>

          <!-- 错误备注 + 重发 -->
          <div v-for="m in errorsOf(turn)" :key="'e' + (m._msg_id ?? m.error_note)" class="inline danger">
            {{ m.error_note }}
            <span class="retry" @click="resendLast"><svg class="i sm"><use href="#i-redo" /></svg>重发上一条消息</span>
          </div>

          <!-- 回合 metrics footer（默认只留操作，指标收进“详情”） -->
          <div v-if="turnMetrics(turn) || turnUndoable(turn)" class="mw" :class="{ open: metricsOpen.has(ti) }">
            <div class="mrow">
              <span class="op" @click="copyMsg(lastAnswerText(turn))"><svg class="i sm"><use href="#i-copy" /></svg>复制</span>
              <span v-if="turnUndoable(turn)" class="undo op" @click="startUndo(turn)"><svg class="i sm"><use href="#i-undo" /></svg>撤销此回合</span>
              <span class="sp" />
              <span v-if="turnMetrics(turn)" class="op" @click="toggleMetrics(ti)"><svg class="i sm chev"><use href="#i-chev-r" /></svg>详情</span>
            </div>
            <div v-if="turnMetrics(turn)" class="metricline">
              <span class="mono">↓ {{ fmtNum(turnMetrics(turn)!.completion_tokens) }}</span>
              <span v-if="turnMetrics(turn)!.token_speed" class="mono">{{ Math.round(turnMetrics(turn)!.token_speed) }} tok/s</span>
              <span v-if="turnMetrics(turn)!.ttft_ms" class="mono">首字 {{ (turnMetrics(turn)!.ttft_ms! / 1000).toFixed(1) }}s</span>
              <span class="mono">回合 {{ (turnMetrics(turn)!.duration_s || 0).toFixed(1) }}s</span>
            </div>
          </div>
          <div v-else-if="answersOf(turn).length" class="mrow">
            <span class="op" @click="copyMsg(lastAnswerText(turn))"><svg class="i sm"><use href="#i-copy" /></svg>复制</span>
          </div>

          <!-- 运行中 live -->
          <template v-if="isTurnLive(turn)">
            <div v-if="reasonBuf" class="thinking">
              <div class="lab">思考</div>{{ reasonBuf.slice(-700) }}
            </div>
            <div v-if="streamingText" class="ans"><div class="md"><MdView :source="streamingText" /><span class="cursor" /></div></div>
            <div v-if="!reasonBuf && !streamingText" class="notice">{{ store.agentName }}正在思考…</div>
          </template>
        </div>
      </template>

      <!-- 内联审批卡：直接在对话内允许/拒绝（与「审批」Tab 同源） -->
      <div v-if="activePerm" class="perm-card" :class="{ ask: isAskPerm }">
        <div class="pc-head">
          <svg class="i"><use :href="isAskPerm ? '#i-chat' : '#i-shield'" /></svg>
          {{ isAskPerm ? `${store.agentName} 提问` : `${store.agentName}请求执行以下操作` }}
          <span class="ss mono">{{ activePerm.sessionName || '当前会话' }} ›</span>
        </div>
        <div class="pc-body">{{ activePerm.summary || activePerm.functionName }}</div>
        <div v-if="activePerm.undoImpact" class="pc-imp">{{ permImpactText(activePerm.undoImpact) }}</div>
        <template v-if="isAskPerm">
          <div v-if="activePerm.options.length" class="pc-choices">
            <button v-for="(o, oi) in activePerm.options" :key="oi" :disabled="permBusy" @click="permChoice(oi)">{{ o }}</button>
          </div>
          <div class="pc-acts">
            <input v-model="permAnswer" placeholder="输入回答…" @keydown.enter.prevent="permSubmit" />
            <button class="p" :disabled="permBusy || !permAnswer.trim()" @click="permSubmit">回答</button>
          </div>
        </template>
        <div v-else-if="activePerm.permissionOptions.length" class="pc-acts">
          <button v-for="o in activePerm.permissionOptions" :key="o.key" class="p" :disabled="permBusy" @click="permOption(o.key)">{{ o.label }}</button>
          <button class="rej" :disabled="permBusy" @click="permDecide(false)">拒绝</button>
        </div>
        <div v-else class="pc-acts">
          <button class="p" :disabled="permBusy" @click="permDecide(true)">允许</button>
          <button v-if="activePerm.allowAlways" :disabled="permBusy" @click="permDecide(true, true)">总是允许</button>
          <button class="rej" :disabled="permBusy" @click="permDecide(false)">拒绝</button>
        </div>
      </div>

      <div v-for="(n, ni) in recentNotices" :key="'n' + ni" class="notice" :class="{ warn: n.kind === 'error' }">{{ n.text }}</div>

      <van-empty v-if="!store.messages.length && store.connected" description="发第一条消息开始对话" />
      <van-empty v-if="!store.connected" description="连接中断，等待重连…" />
    </div>

    <!-- 一键到顶 / 到底（长会话时按需显示，浮在输入栏上方） -->
    <div v-if="showToTop || showToBottom" class="scroll-nav" :style="{ bottom: composerH + 12 + 'px' }">
      <div v-if="showToTop" class="snav-btn" title="回到顶部" @click="toTop">
        <svg class="i lg"><use href="#i-up2" /></svg>
      </div>
      <div v-if="showToBottom" class="snav-btn" title="回到底部" @click="toBottom">
        <svg class="i lg down"><use href="#i-up2" /></svg>
      </div>
    </div>

    <!-- 输入栏 -->
    <div ref="composerEl" class="composer">
      <!-- 排队条 -->
      <div v-if="store.currentQueue.length" class="qbar">
        <span class="q"><svg class="i sm"><use href="#i-clock" /></svg>排队 {{ store.currentQueue.length }} 条：{{ store.currentQueue[0].text }}</span>
        <span class="x" @click="cancelQueued(store.currentQueue[0].id)"><svg class="i sm"><use href="#i-x" /></svg>取消</span>
        <span class="x" style="color:var(--accent)" @click="jumpQueued(store.currentQueue[0].id)"><svg class="i sm"><use href="#i-up2" /></svg>插队</span>
      </div>

      <!-- 附件 chips -->
      <div v-if="attachChips.length" class="chips">
        <div v-for="(a, ai) in attachChips" :key="ai" class="chip">
          <svg class="i sm"><use href="#i-clip" /></svg>{{ a.name }}
          <span class="x" @click="removeAttach(ai)"><svg class="i sm"><use href="#i-x" /></svg></span>
        </div>
      </div>

      <div class="irow">
        <div class="input-box">
          <textarea v-model="draft" rows="1" :placeholder="`向${store.agentName}提问… 更多功能见右上角会话操作`" @keydown.enter.prevent="onEnter" @input="onInput" />
          <!-- 提示/状态 + 停止：与输入框同一行（trailing） -->
          <span v-if="store.isRunning" class="inline-meta running"><span class="dot" />运行中<span class="stop-ic" title="中止会话" @click="stop"><svg class="i sm"><use href="#i-stop" /></svg></span></span>
          <span v-else class="inline-meta">Enter 发送</span>
        </div>
        <button class="send" :class="{ empty: !draft.trim() }" :disabled="sending" @click="onPrimary">
          <svg class="i xxl"><use :href="draft.trim() ? '#i-arrow-up' : '#i-plus'" /></svg>
        </button>
        <input ref="camInput" type="file" accept="image/*" capture="environment" class="hidden-file" @change="onFiles" />
        <input ref="galInput" type="file" accept="image/*" class="hidden-file" @change="onFiles" />
        <input ref="fileInput" type="file" multiple class="hidden-file" @change="onFiles" />
      </div>
    </div>

    <!-- 添加附件：拍照 / 相册 / 文件（移动端底部菜单，不越 tabbar） -->
    <van-popup v-model:show="attachDlg" position="bottom" round>
      <div class="sheet att-sheet">
        <div class="sh-h"><span>添加附件</span><span class="x" @click="attachDlg = false">✕</span></div>
        <div class="ai" @click="pickAttach('cam')"><span class="ic"><svg class="i xxl"><use href="#i-camera" /></svg></span>拍照<span class="sub">调用相机</span></div>
        <div class="ai" @click="pickAttach('gal')"><span class="ic"><svg class="i xxl"><use href="#i-image" /></svg></span>相册<span class="sub">选择图片</span></div>
        <div class="ai" @click="pickAttach('file')"><span class="ic"><svg class="i xxl"><use href="#i-file" /></svg></span>文件<span class="sub">任意文件</span></div>
      </div>
    </van-popup>

    <!-- 撤销确认（preview → execute，与桌面 ChangedFilesPanel 同链路） -->
    <van-popup v-model:show="undoDlg" position="bottom" round>
      <div class="sheet">
        <div class="sh-h"><span>撤销此回合</span><span class="x" @click="undoDlg = false">✕</span></div>
        <div class="undo-impact">{{ undoSummary || '正在读取撤销影响…' }}</div>
        <div class="btnrow">
          <button class="btn-g" @click="undoDlg = false">取消</button>
          <button class="btn-p" :disabled="!undoToken || undoing" @click="confirmUndo">确认撤销</button>
        </div>
      </div>
    </van-popup>

    <!-- 会话操作（⋯：模型/权限/重命名/清空/导出/删除，与列表长按共用组件） -->
    <SessionOps :show="opsDlg" :target="currentTarget" :can-insert="true" @close="opsDlg = false" @reopen="opsDlg = true" @deleted="goBack" @insert="insertDraft" />
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onActivated, onBeforeUnmount, onMounted, ref, type Ref } from 'vue';
import { useRouter } from 'vue-router';
import { showConfirmDialog, showFailToast, showImagePreview, showToast } from 'vant';
import { useAppStore, type ChatMessage, type PermRequest, type ToolCallUi } from '../stores/app';
import MdView from '../components/MdView.vue';
import SessionOps from '../components/SessionOps.vue';
import { copyText } from '../utils/clipboard';
import { buildToolTraceRows, rowStatusLabel, type ToolTraceRow } from '../utils/toolTrace';

const props = defineProps<{ id: string }>();
const store = useAppStore();
const router = useRouter();

// 星瑶品牌 logo（public 静态资源，运行时引用；/m/ 前缀与 lan_server 托管路径一致）
const logoUrl = '/m/icon.png';

const draft = ref('');
const sending = ref(false);
const loadingOlder = ref(false);
const streamEl = ref<HTMLElement>();
interface AttachChip { name: string; path: string }
const attachChips = ref<AttachChip[]>([]);
const attachDlg = ref(false);
const camInput = ref<HTMLInputElement>();
const galInput = ref<HTMLInputElement>();
const fileInput = ref<HTMLInputElement>();

// ---- 次要信息折叠（工具日志 / 思考 / 指标，默认收起）----
const thinkOpen = ref(new Set<number>());
const logsOpen = ref(new Set<number>());
const metricsOpen = ref(new Set<number>());
function makeToggle(s: Ref<Set<number>>) {
  return (i: number) => {
    const next = new Set(s.value);
    if (next.has(i)) next.delete(i);
    else next.add(i);
    s.value = next;
  };
}
const toggleLogs = makeToggle(logsOpen);
const toggleThink = makeToggle(thinkOpen);
const toggleMetrics = makeToggle(metricsOpen);

// ---- 输入：多行自适应 + / @ 引导到会话操作 ----
let lastHintTs = 0;
function onInput(e: Event) {
  autoGrow(e);
  const v = (e.target as HTMLTextAreaElement).value || '';
  if ((/(^|\s)\//.test(v) || v.includes('@')) && Date.now() - lastHintTs > 4000) {
    lastHintTs = Date.now();
    showToast('更多功能请点右上角 ⋯ → 会话操作');
  }
}

/** 会话操作「更多操作」选中后插入输入框（技能 / MCP / 引用文件） */
function insertDraft(text: string) {
  const cur = draft.value.replace(/\s+$/, '');
  draft.value = (cur ? cur + ' ' : '') + text;
  nextTick(() => {
    const el = document.querySelector('.input-box textarea') as HTMLTextAreaElement | null;
    if (!el) return;
    el.focus();
    el.setSelectionRange(el.value.length, el.value.length);
    el.style.height = BASE_TA_H + 'px';
    el.style.height = Math.min(124, el.scrollHeight) + 'px';
  });
}

// ---- 附件 ----
async function onFiles(e: Event) {
  const input = e.target as HTMLInputElement;
  const files = Array.from(input.files || []);
  input.value = '';
  for (const f of files.slice(0, 5)) {
    try {
      const buf = await f.arrayBuffer();
      let binary = '';
      const bytes = new Uint8Array(buf);
      const chunk = 0x8000;
      for (let i = 0; i < bytes.length; i += chunk) {
        binary += String.fromCharCode(...bytes.subarray(i, i + chunk));
      }
      const b64 = btoa(binary);
      const path = await store.uploadAttachment(f.name, b64);
      attachChips.value.push({ name: f.name, path });
    } catch (err: any) {
      showFailToast(err?.message || `上传 ${f.name} 失败`);
    }
  }
}
function removeAttach(i: number) {
  attachChips.value.splice(i, 1);
}

const sessionTitle = computed(() => {
  const s = store.flatSessions.find((x) => x.name === store.currentSession);
  return s?.display_name || store.currentSession || '会话';
});

interface TurnGroup { user?: ChatMessage; items: ChatMessage[] }
const turns = computed<TurnGroup[]>(() => {
  const out: TurnGroup[] = [];
  for (const m of store.messages) {
    if (m.role === 'user') out.push({ user: m, items: [] });
    else if (m.role === 'error') { store.pushNotice(m.content, 'error'); continue; }
    else { if (!out.length) out.push({ items: [] }); out[out.length - 1].items.push(m); }
  }
  return out;
});

function isTurnLive(turn: TurnGroup): boolean {
  const last = turn.items[turn.items.length - 1];
  return store.isRunning && !!last && last.role === 'assistant' && last.execution_state === 'running'
    && !turn.items.some((m) => m.role === 'assistant' && (m.content || '').trim() && m !== last);
}
function turnModel(turn: TurnGroup): string {
  return turn.items.find((m) => m.model_name)?.model_name || store.activeModelName || '';
}
function turnThinking(turn: TurnGroup): string {
  return turn.items.map((m) => m.reasoning_content || '').filter(Boolean).join('\n\n');
}
function turnMetrics(turn: TurnGroup): ChatMessage['metrics'] | null {
  const withMetrics = turn.items.filter((m) => m.metrics);
  if (!withMetrics.length) return null;
  const agg = { duration_s: 0, completion_tokens: 0, token_speed: 0, ttft_ms: undefined as number | undefined };
  for (const m of withMetrics) {
    agg.duration_s += m.metrics!.duration_s || 0;
    agg.completion_tokens += m.metrics!.completion_tokens || 0;
    agg.token_speed = m.metrics!.token_speed || agg.token_speed;
    if (m.metrics!.ttft_ms != null && agg.ttft_ms == null) agg.ttft_ms = m.metrics!.ttft_ms;
  }
  return agg;
}
/** 本回合末条用户消息的 turnId 可撤销（后端有检查点状态时显示按钮） */
function turnUndoable(turn: TurnGroup): boolean {
  return !!(turn.user?.turnId && store.undoStateOf(turn.user.turnId));
}
function lastAnswerText(turn: TurnGroup): string {
  const answers = answersOf(turn);
  return answers.length ? answers[answers.length - 1].content : '';
}
/** 本回合全部工具调用（含历史配对结果） */
function turnCalls(turn: TurnGroup): ToolCallUi[] {
  const calls: ToolCallUi[] = [];
  for (const m of turn.items) for (const c of m.tool_calls || []) calls.push(c);
  return calls;
}
/** 工具执行行：按 display 分类/分组，动作+目标+状态（对齐桌面） */
function turnToolRows(turn: TurnGroup): ToolTraceRow[] {
  const state = isTurnLive(turn) ? 'running' : (turn.items.some((m) => m.error_note) ? 'error' : 'completed');
  return buildToolTraceRows(turnCalls(turn), state);
}
/** 折叠摘要：工具执行状态（执行中 / 有失败 / 已完成） */
function toolStatus(turn: TurnGroup): string {
  const rows = turnToolRows(turn);
  if (rows.some((r) => r.status === 'running')) return '执行中…';
  if (rows.some((r) => r.status === 'failed')) return '有失败';
  return '已完成';
}
const answersOf = (turn: TurnGroup) => turn.items.filter((m) => m.role === 'assistant' && (m.content || '').trim() && m.execution_state !== 'running');
const errorsOf = (turn: TurnGroup) => turn.items.filter((m) => m.error_note);

// ---- 内联审批（与「审批」Tab 同源，直接在对话内完成）----
const activePerm = computed<PermRequest | null>(() =>
  store.permRequests.find((p) => p.sessionName === store.currentSession) || store.permRequests[0] || null,
);
const isAskPerm = computed(() => activePerm.value?.functionName === 'ask_user');
const permBusy = ref(false);
const permAnswer = ref('');

function permImpactText(imp: any): string {
  if (!imp || typeof imp !== 'object') return '';
  if (typeof imp.summary === 'string' && imp.summary) return imp.summary;
  const files = Number(imp.file_count ?? (Array.isArray(imp.files) ? imp.files.length : 0) ?? 0);
  const msgs = Number(imp.message_count ?? 0);
  const parts: string[] = [];
  if (files) parts.push(`${files} 个文件改动`);
  if (msgs) parts.push(`${msgs} 条消息`);
  return parts.length ? `将撤销 ${parts.join('与')}` : '';
}

async function permDecide(approved: boolean, always = false) {
  const p = activePerm.value;
  if (!p) return;
  if (!approved) {
    try {
      await showConfirmDialog({ title: '拒绝请求', message: '确定拒绝该权限请求？模型将收到拒绝结果。' });
    } catch { return; }
  }
  permBusy.value = true;
  try {
    await store.respondPermission(p.requestId, approved, always);
    showToast(approved ? '已允许' : '已拒绝');
  } catch (e: any) {
    showFailToast(e?.message || '操作失败');
  } finally {
    permBusy.value = false;
  }
}

async function permOption(key: string) {
  const p = activePerm.value;
  if (!p) return;
  permBusy.value = true;
  try {
    await store.respondPermissionOption(p.requestId, key);
    showToast('已响应');
  } catch (e: any) {
    showFailToast(e?.message || '操作失败');
  } finally {
    permBusy.value = false;
  }
}

async function permChoice(index: number) {
  const p = activePerm.value;
  if (!p) return;
  permBusy.value = true;
  try {
    await store.respondPermissionChoice(p.requestId, index);
    showToast('已响应');
  } catch (e: any) {
    showFailToast(e?.message || '操作失败');
  } finally {
    permBusy.value = false;
  }
}

async function permSubmit() {
  const p = activePerm.value;
  const text = permAnswer.value.trim();
  if (!p || !text) return;
  permBusy.value = true;
  try {
    await store.respondPermissionChoice(p.requestId, -1, text);
    permAnswer.value = '';
    showToast('已回答');
  } catch (e: any) {
    showFailToast(e?.message || '操作失败');
  } finally {
    permBusy.value = false;
  }
}

const reasonBuf = computed(() => store.currentThinkingText);
const streamingText = computed(() => store.currentStreamingText);
const recentNotices = computed(() => store.notices.slice(-3));

// token 水位
const pctClamped = computed(() => {
  const { used, window } = store.tokenUsage;
  const p = window ? Math.round((used / window) * 100) : (used ? Math.min(100, Math.round(used / 128000 * 100)) : 0);
  return Math.max(0, Math.min(100, p));
});
function fmtNum(n: number): string {
  if (!Number.isFinite(n)) return '0';
  return n >= 1000 ? (n / 1000).toFixed(1).replace(/\.0$/, '') + 'K' : String(n);
}

function ensureLiveVisible() {
  nextTick(() => { if (isNearEnd()) scrollEnd(); });
}

async function copyMsg(t: string) {
  const ok = await copyText(t);
  showToast(ok ? '已复制' : '复制失败');
}

function previewImg(url: string) {
  showImagePreview([url]);
}

function goBack() {
  router.push('/convo');
}

function isNearEnd(): boolean {
  const el = streamEl.value;
  return !el || el.scrollHeight - el.scrollTop - el.clientHeight < 140;
}
function scrollEnd() {
  const el = streamEl.value;
  if (el) el.scrollTop = el.scrollHeight;
}

async function loadOlder() {
  loadingOlder.value = true;
  try {
    const el = streamEl.value;
    const prevHeight = el?.scrollHeight || 0;
    await store.loadOlder();
    nextTick(() => {
      if (el) el.scrollTop = el.scrollHeight - prevHeight;
    });
  } catch (e: any) {
    showFailToast(e?.message || '加载历史失败');
  } finally {
    loadingOlder.value = false;
  }
}

async function send() {
  const text = draft.value.trim();
  if (!text) return;
  sending.value = true;
  try {
    const paths = attachChips.value.map((a) => a.path);
    await store.send(text, paths);
    draft.value = '';
    attachChips.value = [];
    const ta = document.querySelector('.input-box textarea') as HTMLTextAreaElement | null;
    if (ta) ta.style.height = BASE_TA_H + 'px';
    nextTick(scrollEnd);
  } catch (e: any) {
    showFailToast(e?.message || '发送失败');
  } finally {
    sending.value = false;
  }
}

function onEnter() {
  void send();
}

/** 错误重发：重发本回合末条用户消息（对齐客户端草稿回退重试语义） */
async function resendLast() {
  const lastUser = [...store.messages].reverse().find((m) => m.role === 'user');
  if (!lastUser) return;
  try {
    await store.send(lastUser.content);
    showToast('已重发');
    nextTick(scrollEnd);
  } catch (e: any) {
    showFailToast(e?.message || '重发失败');
  }
}

async function stop() {
  try {
    await store.stopTurn();
    showToast('已请求停止');
  } catch (e: any) {
    showFailToast(e?.message || '停止失败');
  }
}

// ---- 排队 ----
async function cancelQueued(id: string) {
  try {
    await store.cancelQueued(id);
  } catch (e: any) {
    showFailToast(e?.message || '取消排队失败');
  }
}
async function jumpQueued(id: string) {
  try {
    await store.jumpQueue(id);
    showToast('已插队');
  } catch (e: any) {
    showFailToast(e?.message || '插队失败');
  }
}

// ---- 撤销回合 ----
const undoDlg = ref(false);
const undoSummary = ref('');
const undoToken = ref('');
const undoTurnId = ref('');
const undoing = ref(false);

async function startUndo(turn: TurnGroup) {
  const turnId = turn.user?.turnId;
  if (!turnId) return;
  undoTurnId.value = turnId;
  undoSummary.value = '';
  undoToken.value = '';
  undoDlg.value = true;
  try {
    const r = await store.previewUndo(turnId);
    if (!r) { undoSummary.value = '无法撤销：服务端预检失败'; return; }
    const p = r.preview as any;
    const fileCount = Number(p?.file_count ?? (p?.files || []).length ?? 0);
    const msgCount = Number(p?.message_count ?? 0);
    undoSummary.value = `将撤销 ${fileCount} 个文件改动与 ${msgCount} 条消息${p?.checkpoint_at ? `，检查点 ${new Date(p.checkpoint_at).toLocaleString()}` : ''}。聊天历史保留。`;
    undoToken.value = r.token;
  } catch (e: any) {
    undoSummary.value = e?.message || '读取撤销影响失败';
  }
}

async function confirmUndo() {
  if (!undoToken.value) return;
  undoing.value = true;
  try {
    await store.executeUndo(undoTurnId.value, undoToken.value);
    undoDlg.value = false;
    showToast('已撤销');
  } catch (e: any) {
    showFailToast(e?.message || '撤销失败');
  } finally {
    undoing.value = false;
  }
}

const BASE_TA_H = 24;
function autoGrow(e: Event) {
  const ta = e.target as HTMLTextAreaElement;
  ta.style.height = BASE_TA_H + 'px';
  ta.style.height = Math.min(124, ta.scrollHeight) + 'px';
}

/** 发送键复用：无文字→打开附件菜单；有文字→发送 */
function onPrimary() {
  if (!draft.value.trim()) { attachDlg.value = true; return; }
  void send();
}

/** 附件来源：拍照 / 相册 / 文件（各自唤起对应 input） */
function pickAttach(kind: 'cam' | 'gal' | 'file') {
  attachDlg.value = false;
  const el = kind === 'cam' ? camInput.value : kind === 'gal' ? galInput.value : fileInput.value;
  el?.click();
}

// ---- 一键到顶 / 到底 ----
const showToTop = ref(false);
const showToBottom = ref(false);
const composerH = ref(72);
const composerEl = ref<HTMLElement>();

function updateScrollNav() {
  const el = streamEl.value;
  if (!el) return;
  showToTop.value = el.scrollTop > 200;
  showToBottom.value = el.scrollHeight - el.scrollTop - el.clientHeight > 300;
}
function onStreamScroll() { updateScrollNav(); }
function toTop() {
  const el = streamEl.value;
  if (el) el.scrollTo({ top: 0, behavior: 'smooth' });
}
function toBottom() {
  const el = streamEl.value;
  if (el) el.scrollTo({ top: el.scrollHeight, behavior: 'smooth' });
}

let composerRO: ResizeObserver | null = null;

const opsDlg = ref(false);
// ⋯ 菜单目标=当前会话（与列表长按共用 SessionOps 组件）
const currentTarget = computed(() => store.flatSessions.find((s) => s.name === store.currentSession) || null);

let unwatch: (() => void) | null = null;
onMounted(() => {
  // 深链直开：确保当前会话与路由一致（tab 内切换时 store 已就绪则跳过）
  const want = decodeURIComponent(props.id || '');
  if (want && want !== store.currentSession && store.flatSessions.some((s) => s.name === want)) {
    void store.switchSession(want).then(() => nextTick(scrollEnd)).catch(() => {});
  }
  // 流式更新跟随滚动
  let lastLen = 0;
  unwatch = store.$subscribe(() => {
    const len = store.currentStreamingText.length + store.currentThinkingText.length;
    if (len !== lastLen) {
      lastLen = len;
      ensureLiveVisible();
    }
  });
  // 输入栏高度随排队条/附件 chips/多行输入变化，实时同步浮动按钮位置
  if (composerEl.value && typeof ResizeObserver !== 'undefined') {
    composerRO = new ResizeObserver(() => { composerH.value = composerEl.value?.offsetHeight || 72; });
    composerRO.observe(composerEl.value);
  }
  nextTick(() => { scrollEnd(); updateScrollNav(); });
});
onActivated(() => nextTick(() => { scrollEnd(); updateScrollNav(); }));
onBeforeUnmount(() => { unwatch?.(); composerRO?.disconnect(); composerRO = null; });
</script>

<style scoped>
.page { height: 100vh; height: 100dvh; display: flex; flex-direction: column; overflow: hidden; position: relative; }
/* 一键到顶 / 到底：浮在输入栏上方，按需显示 */
.scroll-nav { position: absolute; right: 14px; z-index: 5; display: flex; flex-direction: column; gap: 8px; }
.snav-btn { width: 34px; height: 34px; border-radius: 50%; background: var(--bg-raised); border: 1px solid var(--line); color: var(--text-2); display: flex; align-items: center; justify-content: center; box-shadow: 0 2px 8px rgba(0,0,0,.3); cursor: pointer; }
.snav-btn:active { background: var(--bg-overlay); transform: scale(.94); }
.snav-btn .down { transform: rotate(180deg); }
.nav-title { max-width: 150px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; display: inline-block; vertical-align: middle; }
.nvlogo { width: 22px; height: 22px; border-radius: 6px; object-fit: cover; box-shadow: 0 0 8px var(--accent-soft); }
/* 返回键最左且醒目（logo 保留在标题旁） */
.nav-back { width: 36px; height: 36px; border-radius: 10px; background: var(--bg-raised); border: 1px solid var(--line-strong); color: var(--text-bright); display: flex; align-items: center; justify-content: center; flex: none; }
.nav-back .i { transform: rotate(180deg); }
.nav-back:active { background: var(--bg-overlay); }
:deep(.van-nav-bar__left) { gap: 8px; }
:deep(.van-nav-bar__title) { left: 92px !important; }
.mx { color: var(--text-2); display: flex; }
.projline { padding: 5px 16px 7px; font-size: 10px; color: var(--text-3); background: var(--bg-panel); border-bottom: 1px solid var(--line); display: flex; gap: 8px; align-items: center; }
.projline .wd { max-width: 120px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.meter { flex: 1; height: 3px; background: var(--bg-inset); border-radius: 2px; overflow: hidden; }
.meter i { display: block; height: 100%; background: var(--accent); transition: width .3s ease; }
.meter-txt { font-weight: 400; flex: none; }

.stream { flex: 1; overflow-y: auto; padding: 12px 16px; display: flex; flex-direction: column; gap: 10px; }
.load-older { text-align: center; font-size: 11px; color: var(--text-3); padding: 4px; }
.load-older:active { color: var(--accent); }
.day-sep { display: flex; align-items: center; gap: 10px; margin: 2px 0; }
.day-sep::before, .day-sep::after { content: ''; flex: 1; height: 1px; background: var(--line); }

.u-row { display: flex; justify-content: flex-end; margin: 10px 0 6px; }
.u-bub { max-width: 82%; background: linear-gradient(135deg, var(--accent), color-mix(in srgb, var(--accent) 70%, #000)); border-radius: 20px 20px 6px 20px; padding: 10px 14px; font-size: 13.5px; line-height: 1.72; }
html.light .u-bub { background: var(--accent); }
.u-bub .plain { white-space: pre-wrap; overflow-wrap: anywhere; }
.imgrow { display: flex; gap: 5px; margin-top: 6px; flex-wrap: wrap; }
.ph { width: 74px; height: 55px; border-radius: 5px; border: 1px solid var(--line-strong); object-fit: cover; }

.a { margin: 14px 0 18px; }
.a-head { display: flex; align-items: baseline; gap: 7px; margin-bottom: 8px; }
.a-logo { width: 19px; height: 19px; border-radius: 5px; object-fit: cover; align-self: center; flex: none; box-shadow: 0 0 8px var(--accent-soft); }
.a-head .nm { font-size: 12.5px; font-weight: 700; }
.a-head .md { font-size: 10px; color: var(--text-3); }
.a-head .tm { margin-left: auto; font-family: var(--font-mono); font-size: 10px; color: var(--text-3); }
.a-head .live { width: 7px; height: 7px; border-radius: 50%; background: var(--ok); align-self: center; animation: livepulse 1.2s infinite; }

/* 折叠：工具日志 / 思考 / 指标（默认收起，点标题展开） */
.foldhead { display: flex; align-items: center; gap: 6px; font-family: var(--font-mono); font-size: 10.5px; color: var(--text-3); cursor: pointer; padding: 4px 0; user-select: none; }
.chev { transition: transform .15s; }
.fw.open > .foldhead .chev, .tw.open .lb .chev { transform: rotate(90deg); }
.fw .foldbody { display: none; }
.fw.open .foldbody { display: block; }
.tw .think { max-height: 3.4em; overflow: hidden; }
.tw.open .think { max-height: none; }
.mw .metricline { display: none; }
.mw.open .metricline { display: flex; }

.logs { margin: 6px 0 2px; border-radius: 12px; background: var(--bg-raised); border: 1px solid var(--line); padding: 9px 12px; display: flex; flex-direction: column; gap: 3px; }
.lg { display: flex; align-items: center; gap: 8px; font-family: var(--font-mono); font-size: 11px; color: var(--text-2); line-height: 1.5; }
.lg .st { width: 5px; height: 5px; border-radius: 50%; background: var(--ok); flex: none; }
.lg.err .st { background: var(--danger); }
.lg.live .st { background: var(--accent); animation: livepulse 1s infinite; }
/* 分类色：读/搜=accent、命令=绿、写=mutation 橙 */
.lg .st.k-read-group, .lg .st.k-search-group { background: var(--accent); }
.lg .st.k-command { background: var(--ok); }
.lg .st.k-mutation { background: var(--warn); }
.lg .st.k-skill { background: var(--accent); }
.lg .tg { color: var(--text-1); flex: none; max-width: 45%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.lg .ar { color: var(--text-3); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; min-width: 0; }
.lg .rs { margin-left: auto; font-size: 10px; flex: none; color: var(--text-3); }
.lg .rs.good { color: var(--ok); }
.lg.err .rs { color: var(--danger); }
.lg.live .rs { color: var(--accent); }

/* 内联审批卡（与审批 Tab 同源） */
.perm-card { border: 1px solid color-mix(in srgb, var(--danger) 30%, transparent); background: var(--bg-panel); border-radius: 14px; padding: 12px 14px; margin: 10px 0 4px; }
.perm-card.ask { border-color: color-mix(in srgb, var(--accent) 32%, transparent); }
.pc-head { font-size: 11.5px; font-weight: 600; color: var(--danger); display: flex; gap: 7px; align-items: center; }
.perm-card.ask .pc-head { color: var(--accent); }
.pc-head .i { width: 13px; height: 13px; }
.pc-head .ss { margin-left: auto; font-size: 10px; color: var(--text-3); font-weight: 400; }
.pc-body { font-size: 12.5px; color: var(--text-1); margin: 8px 0 4px; line-height: 1.65; word-break: break-all; font-family: var(--font-mono); }
.pc-imp { font-size: 11px; color: var(--text-3); margin: 4px 0 2px; line-height: 1.6; }
.pc-choices { display: flex; flex-direction: column; gap: 7px; margin: 8px 0 4px; }
.pc-choices button { padding: 9px 12px; border-radius: 9px; border: 1px solid var(--line-strong); background: none; color: var(--text-1); font-size: 12.5px; text-align: left; }
.pc-choices button:active { border-color: var(--accent); }
.pc-acts { display: flex; gap: 8px; margin-top: 9px; flex-wrap: wrap; align-items: center; }
.pc-acts button { padding: 8px 14px; border-radius: 9px; border: 1px solid var(--line-strong); background: none; color: var(--text-2); font-size: 12.5px; }
.pc-acts button.p { border-color: var(--accent); background: var(--accent); color: var(--accent-text); font-weight: 600; }
.pc-acts button.rej { color: var(--danger); border-color: color-mix(in srgb, var(--danger) 30%, transparent); background: color-mix(in srgb, var(--danger) 10%, transparent); }
.pc-acts button:disabled { opacity: .5; }
.pc-acts input { flex: 1; min-width: 130px; padding: 8px 11px; border-radius: 9px; border: 1px solid var(--line-strong); background: var(--bg-inset); color: var(--text-1); font-size: 12.5px; outline: none; }

.thinking { font-size: 12px; line-height: 1.78; color: var(--text-3); border-left: 2px solid var(--line); padding-left: 11px; margin: 6px 0; max-height: 130px; overflow-y: auto; white-space: pre-wrap; overflow-wrap: anywhere; }
.thinking .lab { font-size: 9.5px; letter-spacing: .16em; margin-bottom: 3px; opacity: .7; }
/* 已完成回放的思考段（受 .tw 折叠控制） */
.think { font-size: 12px; line-height: 1.78; color: var(--text-3); border-left: 2px solid var(--line); padding-left: 11px; margin: 4px 0 8px; white-space: pre-wrap; overflow-wrap: anywhere; }
.think .lb { font-size: 9.5px; letter-spacing: .16em; opacity: .7; margin-bottom: 3px; font-family: var(--font-mono); display: flex; align-items: center; gap: 5px; cursor: pointer; user-select: none; }
.ans { margin-top: 3px; font-size: 14.5px; line-height: 1.78; overflow-wrap: anywhere; }
.ans .md :deep(p) { margin: 6px 0; }
.ans .md :deep(li) { margin: 5px 0 5px 18px; }

.mrow { display: flex; align-items: center; gap: 5px; margin-top: 11px; font-family: var(--font-mono); font-size: 10.5px; color: var(--text-3); flex-wrap: wrap; }
.mrow .op { padding: 4px 10px; border-radius: 7px; cursor: pointer; display: inline-flex; gap: 5px; align-items: center; }
.mrow .op:active { background: var(--bg-raised); }
.mrow .undo { color: var(--warn); border: 1px solid color-mix(in srgb, var(--warn) 35%, transparent); border-radius: 99px; }
.mrow .sp { flex: 1; }
.metricline { gap: 9px; margin-top: 7px; font-family: var(--font-mono); font-size: 10.5px; color: var(--text-3); }

.inline { margin: 8px 0 2px; border-radius: 9px; padding: 7px 11px; font-size: 11.5px; }
.inline.danger { border: 1px solid color-mix(in srgb, var(--danger) 30%, transparent); background: color-mix(in srgb, var(--danger) 7%, var(--bg-panel)); color: var(--danger); white-space: pre-wrap; overflow-wrap: anywhere; }
.inline .retry { display: inline-flex; gap: 5px; align-items: center; margin-top: 6px; margin-left: 8px; color: var(--accent); cursor: pointer; font-size: 12px; }

.cursor { display: inline-block; width: 7px; height: 13px; background: var(--accent); vertical-align: -2px; animation: bk 1s steps(2) infinite; }
@keyframes bk { 50% { opacity: 0; } }

.notice { font-size: 10.5px; color: var(--text-3); }
.notice.warn { color: var(--warn); border: 1px solid color-mix(in srgb, var(--warn) 30%, transparent); background: color-mix(in srgb, var(--warn) 8%, var(--bg-panel)); border-radius: 8px; padding: 6px 10px; white-space: pre-wrap; overflow-wrap: anywhere; }

/* ===== 输入栏 ===== */
.composer { flex: none; position: sticky; bottom: 0; z-index: 5; background: var(--bg); border-top: 1px solid var(--line); padding: 9px 16px calc(14px + env(safe-area-inset-bottom)); }

.qbar { display: flex; align-items: center; gap: 8px; font-size: 11px; color: var(--warn); background: color-mix(in srgb, var(--warn) 10%, var(--bg-panel)); border: 1px solid color-mix(in srgb, var(--warn) 30%, transparent); border-radius: 10px; padding: 7px 12px; margin-bottom: 7px; }
.qbar .q { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; display: inline-flex; gap: 6px; align-items: center; }
.qbar .x { cursor: pointer; flex: none; display: inline-flex; gap: 4px; align-items: center; }
.chips { display: flex; gap: 6px; margin-bottom: 6px; flex-wrap: wrap; }
.chip { font-size: 11px; font-family: var(--font-mono); color: var(--text-2); background: var(--bg-raised); border: 1px solid var(--line); border-radius: 99px; padding: 3px 10px; display: flex; gap: 6px; align-items: center; }
.chip .x { color: var(--text-3); cursor: pointer; display: flex; }
/* 提示/状态 + 停止：并入输入框同一行 */
.inline-meta { display: inline-flex; align-items: center; gap: 6px; font-size: 10px; color: var(--text-3); flex: none; align-self: flex-end; white-space: nowrap; }
.inline-meta.running { color: var(--ok); }
.inline-meta .dot { width: 6px; height: 6px; border-radius: 50%; background: var(--ok); flex: none; }
.inline-meta .stop-ic { width: 24px; height: 24px; border-radius: 50%; border: 1px solid var(--line-strong); display: flex; align-items: center; justify-content: center; color: var(--text-2); }
.inline-meta .stop-ic:active { background: var(--bg-raised); }
/* 输入行只有 [输入框][发送键]：附件入口复用发送键，编辑区最大化 */
.irow { display: flex; align-items: flex-end; gap: 8px; }
.hidden-file { display: none; }
.input-box { flex: 1; min-width: 0; background: var(--bg-overlay); border-radius: 14px; padding: 8px 13px; display: flex; align-items: flex-end; gap: 8px; min-height: 40px; }
html.light .input-box { background: var(--bg-inset); }
.input-box textarea { flex: 1; min-width: 0; border: none; background: none; resize: none; color: var(--text-1); font-size: 16px; font-family: var(--font-ui); outline: none; height: 24px; max-height: 124px; line-height: 1.5; }
.input-box textarea::placeholder { color: var(--text-3); }
.send { width: 40px; height: 40px; border-radius: 12px; background: var(--accent); color: var(--accent-text); display: flex; align-items: center; justify-content: center; border: 1px solid var(--accent); flex: none; cursor: pointer; transition: background .15s, color .15s; }
.send svg { stroke: currentColor; }
.send.empty { background: var(--bg-raised); border-color: var(--line-strong); color: var(--text-2); }
.send:disabled { opacity: .5; }

/* 添加附件底部菜单 */
.att-sheet .ai { display: flex; align-items: center; gap: 13px; padding: 15px 16px; font-size: 14px; cursor: pointer; }
.att-sheet .ai:active { background: var(--bg-raised); }
.att-sheet .ai .ic { width: 38px; height: 38px; border-radius: 10px; background: var(--bg-raised); border: 1px solid var(--line); display: flex; align-items: center; justify-content: center; color: var(--accent); flex: none; }
.att-sheet .ai .sub { margin-left: auto; font-size: 11px; color: var(--text-3); }

/* 撤销弹层 */
.sheet { padding: 14px 16px 20px; }
.sh-h { display: flex; align-items: center; justify-content: space-between; padding: 0; margin-bottom: 10px; font-size: 13px; font-weight: 600; }
.sh-h .x { font-size: 12px; color: var(--text-3); font-weight: 400; padding: 4px 8px; }
.undo-impact { margin: 0; padding: 10px 12px; border-radius: 9px; border: 1px solid color-mix(in srgb, var(--warn) 30%, transparent); background: color-mix(in srgb, var(--warn) 8%, var(--bg-panel)); color: var(--text-2); font-size: 12px; line-height: 1.7; }
.btnrow { display: flex; gap: 8px; margin: 16px 0 0; }
.btnrow button { flex: 1; padding: 10px; border-radius: 8px; font-size: 13px; }
.btn-g { border: 1px solid var(--line-strong); background: none; color: var(--text-2); }
.btn-p { border: none; background: var(--accent); color: var(--accent-text); font-weight: 600; }
.btn-p:disabled { opacity: .4; }
</style>

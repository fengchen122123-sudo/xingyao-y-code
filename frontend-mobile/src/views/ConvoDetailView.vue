<template>
  <div class="page">
    <van-nav-bar fixed placeholder safe-area-inset-top @click-left="goBack">
      <template #left>
        <img class="nvlogo" :src="logoUrl" alt="星瑶" />
        <svg class="i lg back"><use href="#i-chev-r" /></svg>
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
            <span class="nm">星瑶</span>
            <span class="md mono">{{ turnModel(turn) }}</span>
            <span class="tm">{{ isTurnLive(turn) ? '运行中' : '' }}</span>
          </div>

          <!-- 工具执行行 -->
          <div v-if="turnToolLines(turn).length" class="logs">
            <div v-for="(l, li) in turnToolLines(turn)" :key="li" class="lg" :class="{ err: l.ok === false, live: l.live }">
              <span class="st" />
              <span class="tg">{{ l.label }}</span>
              <span class="ar">{{ l.summary }}</span>
              <span class="rs" :class="{ good: l.ok && !l.live }">{{ l.live ? '执行中…' : (l.ok ? '完成' : '失败') }}</span>
            </div>
          </div>

          <!-- 思考段（已完成回放） -->
          <div v-if="turnThinking(turn) && !isTurnLive(turn)" class="thinking">
            <div class="lab">思考</div>{{ turnThinking(turn) }}
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

          <!-- 回合 metrics footer（对齐客户端 step_metrics/历史 _display.metrics） -->
          <div v-if="turnMetrics(turn) || turnUndoable(turn)" class="mrow">
            <span class="op" @click="copyMsg(lastAnswerText(turn))"><svg class="i sm"><use href="#i-copy" /></svg>复制</span>
            <span v-if="turnUndoable(turn)" class="undo op" @click="startUndo(turn)"><svg class="i sm"><use href="#i-undo" /></svg>撤销此回合</span>
            <span class="sp" />
            <template v-if="turnMetrics(turn)">
              <span class="mono">↓ {{ fmtNum(turnMetrics(turn)!.completion_tokens) }}</span>
              <span v-if="turnMetrics(turn)!.token_speed" class="mono">{{ Math.round(turnMetrics(turn)!.token_speed) }} tok/s</span>
              <span v-if="turnMetrics(turn)!.ttft_ms" class="mono">首字 {{ (turnMetrics(turn)!.ttft_ms! / 1000).toFixed(1) }}s</span>
              <span class="mono">回合 {{ (turnMetrics(turn)!.duration_s || 0).toFixed(1) }}s</span>
            </template>
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
            <div v-if="!reasonBuf && !streamingText" class="notice">正在思考…</div>
          </template>
        </div>
      </template>

      <div v-for="(n, ni) in recentNotices" :key="'n' + ni" class="notice" :class="{ warn: n.kind === 'error' }">{{ n.text }}</div>

      <van-empty v-if="!store.messages.length && store.connected" description="发第一条消息开始对话" />
      <van-empty v-if="!store.connected" description="连接中断，等待重连…" />
    </div>

    <!-- 回到底部（离底 >300px 显示） -->
    <div v-if="showToBottom" class="to-bottom" @click="toBottom"><svg class="i" style="transform:rotate(180deg)"><use href="#i-chev-r" /></svg></div>

    <!-- 输入栏 -->
    <div class="composer">
      <!-- @ / 斜杠 补全面板 -->
      <div v-if="suggests.length" class="sugg">
        <div
          v-for="(s, si) in suggests"
          :key="si"
          class="sg"
          :class="{ on: si === sugIndex }"
          @click="applySuggest(s)"
        >
          <span class="k mono">{{ s.kind }}</span>{{ s.label }}<span class="d">{{ s.desc }}</span>
        </div>
      </div>

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

      <div class="comp-meta">
        <span v-if="store.isRunning" class="run">● 运行中 · 发送自动排队</span>
        <span v-else class="hint">Enter 发送</span>
        <span class="ic stop-ic" :class="{ disabled: !store.isRunning }" @click="stop"><svg class="i sm"><use href="#i-stop" /></svg></span>
      </div>
      <div class="input-flat">
        <span class="att">
          <input type="file" multiple class="file-input" @change="onFiles" />
          <svg class="i lg"><use href="#i-clip" /></svg>
        </span>
        <div class="input-box">
          <textarea v-model="draft" rows="1" placeholder="输入指令… / 技能 · @ 引用文件" @keydown.enter.prevent="onEnter" @input="autoGrow" @keyup="onKeyup" @click="onKeyup" />
        </div>
        <button class="send" :disabled="sending" @click="send"><svg class="i lg" style="stroke:var(--accent-text)"><use href="#i-arrow-up" /></svg></button>
      </div>
    </div>

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
    <SessionOps :show="opsDlg" :target="currentTarget" @close="opsDlg = false" @deleted="goBack" />
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onActivated, onBeforeUnmount, onMounted, ref } from 'vue';
import { useRouter } from 'vue-router';
import { showFailToast, showImagePreview, showToast } from 'vant';
import { useAppStore, type ChatMessage } from '../stores/app';
import MdView from '../components/MdView.vue';
import SessionOps from '../components/SessionOps.vue';
import { copyText } from '../utils/clipboard';

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

// ---- @ / 斜杠补全 ----
interface Suggest { kind: string; label: string; desc: string; insert: string }
const sugIndex = ref(0);
const suggests = ref<Suggest[]>([]);
let sugTrigger = '';

/** 扁平化文件树路径（限深，避免超大工作区卡顿） */
function flattenTree(nodes: any[], prefix = '', depth = 0): string[] {
  if (depth > 3) return [];
  const out: string[] = [];
  for (const n of nodes || []) {
    const p = prefix ? `${prefix}/${n.name}` : String(n.name || '');
    out.push(p);
    if (n.children?.length) out.push(...flattenTree(n.children, p, depth + 1));
  }
  return out;
}
const flatFiles = computed(() => flattenTree(store.fileTree).slice(0, 200));

function detectSuggest() {
  const el = document.querySelector('.input-box textarea') as HTMLTextAreaElement | null;
  if (!el) return;
  const upto = draft.value.slice(0, el.selectionStart ?? draft.value.length);
  const at = upto.match(/@([\w./\\\-\u4e00-\u9fa5]*)$/);
  const slash = upto.match(/(^|\s)\/([\w\u4e00-\u9fa5-]*)$/);
  if (at) {
    sugTrigger = at[0];
    const q = (at[1] || '').toLowerCase();
    suggests.value = flatFiles.value
      .filter((f) => f.toLowerCase().includes(q))
      .slice(0, 6)
      .map((f) => ({ kind: '@', label: f, desc: '文件', insert: `@${f} ` }));
  } else if (slash) {
    sugTrigger = slash[0];
    const q = (slash[2] || '').toLowerCase();
    const cmds: Suggest[] = [
      { kind: '/', label: 'clear', desc: '清空当前会话历史', insert: '/clear ' },
    ];
    const skillItems: Suggest[] = (store.skills || [])
      .filter((s: any) => String(s.name || '').toLowerCase().includes(q) && s.enabled !== false)
      .slice(0, 5)
      .map((s: any) => ({ kind: '/', label: String(s.name || ''), desc: String(s.description || '技能').slice(0, 24), insert: `/${s.name} ` }));
    suggests.value = [...skillItems, ...cmds.filter((c) => c.label.toLowerCase().includes(q))].slice(0, 6);
  } else {
    suggests.value = [];
  }
  sugIndex.value = 0;
}
function onKeyup() { detectSuggest(); }
function applySuggest(s: Suggest) {
  const el = document.querySelector('.input-box textarea') as HTMLTextAreaElement | null;
  if (el && sugTrigger) {
    const pos = el.selectionStart ?? draft.value.length;
    draft.value = draft.value.slice(0, pos - sugTrigger.length) + s.insert + draft.value.slice(pos);
  } else {
    draft.value += s.insert;
  }
  suggests.value = [];
  nextTick(() => (document.querySelector('.input-box textarea') as HTMLTextAreaElement | null)?.focus());
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
interface ToolLine { label: string; summary: string; ok?: boolean; live?: boolean }
function turnToolLines(turn: TurnGroup): ToolLine[] {
  const lines: ToolLine[] = [];
  for (const m of turn.items) {
    for (const c of m.tool_calls || []) {
      lines.push({
        label: c.label || c.name,
        summary: c.result?.summary || '',
        ok: c.result ? c.result.ok : undefined,
        live: !c.result,
      });
    }
  }
  return lines;
}
const answersOf = (turn: TurnGroup) => turn.items.filter((m) => m.role === 'assistant' && (m.content || '').trim() && m.execution_state !== 'running');
const errorsOf = (turn: TurnGroup) => turn.items.filter((m) => m.error_note);

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
    suggests.value = [];
    const ta = document.querySelector('.input-box textarea') as HTMLTextAreaElement | null;
    if (ta) ta.style.height = '38px';
    nextTick(scrollEnd);
  } catch (e: any) {
    showFailToast(e?.message || '发送失败');
  } finally {
    sending.value = false;
  }
}

function onEnter() {
  if (suggests.value.length) {
    applySuggest(suggests.value[sugIndex.value]);
    return;
  }
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

function autoGrow(e: Event) {
  const ta = e.target as HTMLTextAreaElement;
  ta.style.height = '38px';
  ta.style.height = Math.min(110, ta.scrollHeight) + 'px';
}

const showToBottom = ref(false);
function onStreamScroll() {
  const el = streamEl.value;
  if (!el) return;
  showToBottom.value = el.scrollHeight - el.scrollTop - el.clientHeight > 300;
}
function toBottom() { const el = streamEl.value; if (el) el.scrollTop = el.scrollHeight; }

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
});
onActivated(() => nextTick(scrollEnd));
onBeforeUnmount(() => unwatch?.());
</script>

<style scoped>
.page { height: 100vh; height: 100dvh; display: flex; flex-direction: column; overflow: hidden; position: relative; }
.to-bottom { position: absolute; right: 14px; bottom: 96px; width: 30px; height: 30px; border-radius: 50%; background: var(--bg-raised); border: 1px solid var(--line); color: var(--text-2); display: flex; align-items: center; justify-content: center; font-size: 13px; z-index: 5; box-shadow: 0 2px 8px rgba(0,0,0,.3); }
.nav-title { max-width: 150px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; display: inline-block; vertical-align: middle; }
.nvlogo { width: 22px; height: 22px; border-radius: 6px; object-fit: cover; box-shadow: 0 0 8px var(--accent-soft); }
.back { transform: rotate(180deg); color: var(--text-2); cursor: pointer; }
.mx { color: var(--text-2); display: flex; }
.projline { padding: 5px 16px 7px; font-size: 10px; color: var(--text-3); background: var(--bg-panel); border-bottom: 1px solid var(--line); display: flex; gap: 8px; align-items: center; }
.projline .wd { max-width: 120px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.meter { flex: 1; height: 3px; background: var(--bg-inset); border-radius: 2px; overflow: hidden; }
.meter i { display: block; height: 100%; background: var(--accent); transition: width .3s ease; }
.meter-txt { font-weight: 400; flex: none; }

.stream { flex: 1; overflow-y: auto; padding: 10px 12px; display: flex; flex-direction: column; gap: 8px; }
.load-older { text-align: center; font-size: 11px; color: var(--text-3); padding: 4px; }
.load-older:active { color: var(--accent); }
.day-sep { display: flex; align-items: center; gap: 10px; margin: 2px 0; }
.day-sep::before, .day-sep::after { content: ''; flex: 1; height: 1px; background: var(--line); }

.u-row { display: flex; justify-content: flex-end; margin: 6px 0 3px; }
.u-bub { max-width: 84%; background: linear-gradient(135deg, var(--accent), color-mix(in srgb, var(--accent) 70%, #000)); border-radius: 20px 20px 6px 20px; padding: 10px 15px; font-size: 14px; line-height: 1.6; }
html.light .u-bub { background: var(--accent); }
.u-bub .plain { white-space: pre-wrap; overflow-wrap: anywhere; }
.imgrow { display: flex; gap: 5px; margin-top: 6px; flex-wrap: wrap; }
.ph { width: 74px; height: 55px; border-radius: 5px; border: 1px solid var(--line-strong); object-fit: cover; }

.a { margin: 10px 0 12px; }
.a-head { display: flex; align-items: baseline; gap: 7px; margin-bottom: 6px; }
.a-logo { width: 19px; height: 19px; border-radius: 5px; object-fit: cover; align-self: center; flex: none; box-shadow: 0 0 8px var(--accent-soft); }
.a-head .nm { font-size: 13px; font-weight: 700; }
.a-head .md { font-size: 10px; color: var(--text-3); }
.a-head .tm { margin-left: auto; font-family: var(--font-mono); font-size: 10px; color: var(--text-3); }
.a-head .live { width: 7px; height: 7px; border-radius: 50%; background: var(--ok); align-self: center; animation: livepulse 1.2s infinite; }

.logs { margin: 6px 0 2px; border-radius: 12px; background: var(--bg-raised); border: 1px solid var(--line); padding: 8px 12px; display: flex; flex-direction: column; gap: 1px; }
.lg { display: flex; align-items: center; gap: 8px; font-family: var(--font-mono); font-size: 11px; color: var(--text-2); line-height: 1.5; }
.lg .st { width: 5px; height: 5px; border-radius: 50%; background: var(--ok); flex: none; }
.lg.err .st { background: var(--danger); }
.lg.live .st { background: var(--accent); animation: livepulse 1s infinite; }
.lg .tg { color: var(--text-1); flex: none; }
.lg .ar { color: var(--text-3); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.lg .rs { margin-left: auto; font-size: 10px; flex: none; }
.lg .rs.good { color: var(--ok); }
.lg .rs:not(.good) { color: var(--danger); }

.thinking { font-size: 12.5px; line-height: 1.7; color: var(--text-3); border-left: 2px solid var(--line); padding-left: 11px; margin: 6px 0; max-height: 130px; overflow-y: auto; white-space: pre-wrap; overflow-wrap: anywhere; }
.thinking .lab { font-size: 9.5px; letter-spacing: .16em; margin-bottom: 3px; opacity: .7; }
.ans { margin-top: 3px; font-size: 15px; line-height: 1.7; overflow-wrap: anywhere; }
.ans .md :deep(p) { margin: 5px 0; }
.ans .md :deep(li) { margin: 5px 0 5px 18px; }

.mrow { display: flex; align-items: center; gap: 5px; margin-top: 9px; font-family: var(--font-mono); font-size: 10.5px; color: var(--text-3); flex-wrap: wrap; }
.mrow .op { padding: 4px 10px; border-radius: 7px; cursor: pointer; display: inline-flex; gap: 5px; align-items: center; }
.mrow .op:active { background: var(--bg-raised); }
.mrow .undo { color: var(--warn); border: 1px solid color-mix(in srgb, var(--warn) 35%, transparent); border-radius: 99px; }
.mrow .sp { flex: 1; }

.inline { margin: 8px 0 2px; border-radius: 9px; padding: 7px 11px; font-size: 11.5px; }
.inline.danger { border: 1px solid color-mix(in srgb, var(--danger) 30%, transparent); background: color-mix(in srgb, var(--danger) 7%, var(--bg-panel)); color: var(--danger); white-space: pre-wrap; overflow-wrap: anywhere; }
.inline .retry { display: inline-flex; gap: 5px; align-items: center; margin-top: 6px; margin-left: 8px; color: var(--accent); cursor: pointer; font-size: 12px; }

.cursor { display: inline-block; width: 7px; height: 13px; background: var(--accent); vertical-align: -2px; animation: bk 1s steps(2) infinite; }
@keyframes bk { 50% { opacity: 0; } }

.notice { font-size: 10.5px; color: var(--text-3); }
.notice.warn { color: var(--warn); border: 1px solid color-mix(in srgb, var(--warn) 30%, transparent); background: color-mix(in srgb, var(--warn) 8%, var(--bg-panel)); border-radius: 8px; padding: 6px 10px; white-space: pre-wrap; overflow-wrap: anywhere; }

/* ===== 输入栏 ===== */
.composer { flex: none; position: sticky; bottom: 0; z-index: 5; background: var(--bg); border-top: 1px solid var(--line); padding: 8px 14px calc(14px + env(safe-area-inset-bottom)); }
.sugg { position: absolute; left: 12px; right: 12px; bottom: 100%; margin-bottom: 6px; background: var(--bg-panel); border: 1px solid var(--line-strong); border-radius: 12px; overflow: hidden; box-shadow: var(--shadow-2); }
.sugg .sg { padding: 9px 14px; font-size: 12.5px; display: flex; gap: 8px; align-items: center; cursor: pointer; }
.sugg .sg.on, .sugg .sg:active { background: var(--bg-raised); }
.sugg .sg .k { font-size: 10px; color: var(--accent); border: 1px solid var(--accent-line); border-radius: 4px; padding: 0 6px; flex: none; }
.sugg .sg .d { margin-left: auto; font-size: 10px; color: var(--text-3); }
.qbar { display: flex; align-items: center; gap: 8px; font-size: 11px; color: var(--warn); background: color-mix(in srgb, var(--warn) 10%, var(--bg-panel)); border: 1px solid color-mix(in srgb, var(--warn) 30%, transparent); border-radius: 10px; padding: 7px 12px; margin-bottom: 7px; }
.qbar .q { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; display: inline-flex; gap: 6px; align-items: center; }
.qbar .x { cursor: pointer; flex: none; display: inline-flex; gap: 4px; align-items: center; }
.chips { display: flex; gap: 6px; margin-bottom: 6px; flex-wrap: wrap; }
.chip { font-size: 11px; font-family: var(--font-mono); color: var(--text-2); background: var(--bg-raised); border: 1px solid var(--line); border-radius: 99px; padding: 3px 10px; display: flex; gap: 6px; align-items: center; }
.chip .x { color: var(--text-3); cursor: pointer; display: flex; }
.comp-meta { display: flex; align-items: center; gap: 7px; font-size: 10px; color: var(--text-3); margin-bottom: 5px; }
.comp-meta .run { color: var(--ok); }
.comp-meta .hint { flex: 1; }
.comp-meta .stop-ic { width: 22px; height: 22px; border-radius: 50%; border: 1px solid var(--line-strong); display: flex; align-items: center; justify-content: center; color: var(--text-2); }
.comp-meta .stop-ic.disabled { opacity: .35; }
.input-flat { display: flex; align-items: flex-end; gap: 6px; }
.att { color: var(--text-2); padding: 9px 4px; cursor: pointer; display: flex; position: relative; }
.file-input { position: absolute; inset: 0; opacity: 0; width: 100%; height: 100%; cursor: pointer; }
.input-box { flex: 1; min-width: 0; background: var(--bg-overlay); border-radius: 13px; padding: 9px 12px; display: flex; align-items: flex-end; gap: 7px; }
html.light .input-box { background: var(--bg-inset); }
.input-box textarea { flex: 1; min-width: 0; border: none; background: none; resize: none; color: var(--text-1); font-size: 16px; font-family: var(--font-ui); outline: none; height: 38px; line-height: 1.5; }
.input-box textarea::placeholder { color: var(--text-3); }
.send { width: 38px; height: 38px; border-radius: 11px; background: var(--accent); color: var(--accent-text); display: flex; align-items: center; justify-content: center; border: none; flex: none; cursor: pointer; }
.send:disabled { opacity: .5; }

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

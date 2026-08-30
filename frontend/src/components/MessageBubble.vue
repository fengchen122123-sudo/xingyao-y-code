<template>
  <div class="msg" :class="msg.role === 'user' ? 'msg-user' : msg.role === 'error' ? 'msg-error' : msg.role === 'notice' ? 'msg-notice' : 'msg-assistant'">
    <!-- 用户消息（文本 + 可选多张图片缩略图，点击放大）；气泡下方悬停浮现操作栏 -->
    <template v-if="msg.role === 'user'">
      <div class="bubble user-bubble">
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
      <!-- 用户消息操作栏：气泡下方，悬停气泡时浮现，点击复制本条消息文本 -->
      <div v-if="msg.content" class="user-action-bar">
        <button
          class="msg-copy-btn"
          :class="{ copied: userCopied }"
          :title="userCopied ? '已复制' : '复制消息'"
          data-test="user-copy-btn"
          @click="copyUserMessage"
        >
          <AppIcon name="copy" :size="13" :stroke-width="2" />
          <span>{{ userCopied ? '已复制' : '复制' }}</span>
        </button>
      </div>
    </template>

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

        <!-- Markdown 文本内容（代码块复制按钮通过事件委托处理点击）；
             文件链接先渲染、后经后端批量校验，目录/不存在的路径降级为纯文本；
             媒体清单（media_v1）存在时 ![..](..)/[video:..](..) 渲染为图片/视频 -->
        <div
          v-if="msg.content && msg.content.trim()"
          ref="mdBodyRef"
          class="md-body"
          v-html="renderMarkdown(msg.content, mediaCtx)"
          @click="onMdBodyClick"
        ></div>

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
import { ref, computed, watch, nextTick, onUnmounted } from 'vue';
import { useAppStore, type ChatMessage } from '../stores/appStore';
import { renderMarkdown } from '../utils/markdown';
import { renderMermaidBlocks } from '../utils/mermaidRenderer';
import { toAbsoluteLocalPath } from '../utils/localPath';
import AppIcon from './AppIcon.vue';
import FoldCard from './FoldCard.vue';
import ToolExecutionTrace from './ToolExecutionTrace.vue';

const props = defineProps<{
  msg: ChatMessage;
}>();

// 当前气泡是“本轮正在等待响应”的空助手气泡时，显示动态干活反馈。
// 任何实际内容（思考流 / 工具调用 / 正文 token）到达后自动隐藏。
const store = useAppStore();
// 用户消息复制按钮的「已复制」反馈状态与定时器
const userCopied = ref(false);
let userCopiedTimer: number | undefined;
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
  if (userCopiedTimer) window.clearTimeout(userCopiedTimer);
});

// 思考块默认跟随全局 store.showThinking；用户在本条消息上手动切换过一次后，
// 该条消息就"钉住"为用户选择的状态，不再随全局按钮变化。
const localThinkOverride = ref<boolean | null>(null);
const thinkingOpen = computed({
  get: () => (localThinkOverride.value !== null ? localThinkOverride.value : store.showThinking),
  set: (v: boolean) => { localThinkOverride.value = v; },
});

// 媒体渲染上下文：media_v1 清单按 ordinal 与正文中的媒体语法对齐
const mediaCtx = computed(() =>
  props.msg.media && props.msg.media.length ? { items: props.msg.media } : undefined
);

// 复制文本到剪贴板：优先 Clipboard API，失败回退 execCommand（pywebview/旧内核兼容）
async function copyToClipboard(text: string): Promise<void> {
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    document.execCommand('copy');
    document.body.removeChild(ta);
  }
}

// Markdown 内容点击事件委托：本地文件链接走内嵌预览，代码块复制按钮走复制；
// 媒体图片点击进灯箱（与本条消息内其他图片共用导航序列）
function onMdBodyClick(e: MouseEvent) {
  const mdImg = (e.target as HTMLElement).closest('img.md-img');
  if (mdImg) {
    const urls = (props.msg.media || [])
      .filter(m => m.kind === 'image' && m.url)
      .map(m => m.url as string);
    if (urls.length) {
      const src = mdImg.getAttribute('src') || '';
      store.openLightbox(urls, Math.max(0, urls.indexOf(src)));
    }
    return;
  }

  const fileLink = (e.target as HTMLElement).closest('a[data-file-link]');
  if (fileLink) {
    e.preventDefault();
    const raw = fileLink.getAttribute('data-file-link') || '';
    if (!raw) return;
    const target = toAbsoluteLocalPath(raw, store.workdir);
    if (!target) {
      store.notifyModal('文件预览', '当前会话未绑定工作目录，无法解析相对路径');
      return;
    }
    store.openFilePreview(target);
    return;
  }

  const mermaidCopy = (e.target as HTMLElement).closest('.mermaid-copy');
  if (mermaidCopy) {
    onMermaidCopyClick(mermaidCopy as HTMLElement);
    return;
  }

  const btn = (e.target as HTMLElement).closest('.code-copy');
  if (!btn) return;
  const code = btn.closest('.code-block')?.querySelector('code');
  if (!code) return;
  const text = code.textContent || '';
  copyToClipboard(text)
    .then(() => {
      const original = btn.textContent;
      btn.textContent = '已复制';
      btn.classList.add('copied');
      window.setTimeout(() => {
        btn.textContent = original;
        btn.classList.remove('copied');
      }, 1500);
    })
    .catch(() => {
      // 复制失败保持原样，不额外提示
    });
}

// mermaid 块复制源码：渲染成功与否都可从隐藏/展示的 pre.mermaid-src 取原文
function onMermaidCopyClick(btn: HTMLElement) {
  const source = btn.closest('.mermaid-block')?.querySelector('pre.mermaid-src');
  if (!source) return;
  copyToClipboard(source.textContent || '')
    .then(() => {
      const original = btn.textContent;
      btn.textContent = '已复制';
      window.setTimeout(() => {
        btn.textContent = original;
      }, 1500);
    })
    .catch(() => {
      // 复制失败保持原样，不额外提示
    });
}

// ---- 文件链接后置校验：目录 / 不存在的路径降级为纯文本 ----
// 渲染层（renderMarkdown）是纯字符串函数无法查磁盘，先按路径形态渲染成链接，
// 挂载/内容变化后再由后端批量确认真实类型；目录与不存在的路径还原为纯文本，
// 避免用户点击后才发现「无法预览」。校验失败（如 API 未就绪）时保留链接原样。
const mdBodyRef = ref<HTMLElement | null>(null);
// 跨渲染的路径类型缓存：v-html 在 content 每次变化时整体重建 DOM，
// 节点级标记会随 innerHTML 替换丢失，因此按路径文本缓存校验结果；
// 流式输出期间同一路径只请求一次后端。
const pathKindCache = new Map<string, string>();
let pathCheckSeq = 0;

function downgradeInvalidFileLinks(root: HTMLElement | null): void {
  if (!root) return;
  const anchors = Array.from(root.querySelectorAll('a[data-file-link]'));
  if (!anchors.length) return;
  // 先应用缓存：已确认目录/不存在路径直接降级，仅未知路径送后端
  const pending: Array<{ raw: string; abs: string }> = [];
  const seen = new Set<string>();
  for (const a of anchors) {
    const raw = a.getAttribute('data-file-link') || '';
    const cached = pathKindCache.get(raw);
    if (cached && cached !== 'file') {
      const span = document.createElement('span');
      span.textContent = a.textContent || '';
      a.replaceWith(span);
      continue;
    }
    if (!cached && !seen.has(raw)) {
      seen.add(raw);
      pending.push({ raw, abs: toAbsoluteLocalPath(raw, store.workdir) || raw });
    }
  }
  if (!pending.length) return;
  const seq = ++pathCheckSeq;
  store.checkPathsKind(pending.map(p => p.abs)).then((results) => {
    if (seq !== pathCheckSeq) return; // 期间内容又变了，丢弃过期结果
    // 按返回的 path 字段对应（而非索引），对重复触发/乱序更稳健
    for (const r of results || []) {
      if (r && r.kind) {
        const hit = pending.find(p => p.abs === r.path);
        if (hit) pathKindCache.set(hit.raw, r.kind);
      }
    }
    for (const a of anchors) {
      if (!a.isConnected) continue;
      const raw = a.getAttribute('data-file-link') || '';
      const kind = pathKindCache.get(raw);
      // 校验不到的条目（理论不发生）保持链接原样，宁可多留可点链接也不误伤
      if (kind && kind !== 'file') {
        const span = document.createElement('span');
        span.textContent = a.textContent || '';
        a.replaceWith(span);
      }
    }
  }).catch(() => { /* 校验失败保留链接，点击时后端仍有兜底提示 */ });
}

watch(
  () => [props.msg.content, mdBodyRef.value],
  async () => {
    await nextTick();
    downgradeInvalidFileLinks(mdBodyRef.value);
    // mermaid 块异步渲染：渲染失败/加载失败时块内降级显示源码，不阻塞正文
    void renderMermaidBlocks(mdBodyRef.value);
  },
  { immediate: true },
);

// 用户消息复制：点击复制按钮把本条消息文本写入剪贴板，按钮短暂显示「已复制」
async function copyUserMessage() {
  const text = props.msg.content || '';
  if (!text) return;
  await copyToClipboard(text);
  userCopied.value = true;
  if (userCopiedTimer) window.clearTimeout(userCopiedTimer);
  userCopiedTimer = window.setTimeout(() => { userCopied.value = false; }, 1500);
}
</script>

<style scoped>
.msg {
  max-width: var(--chat-col-width);
  margin: 0 auto 18px;
}
.msg-user {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
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
/* 用户消息操作栏：气泡下方一行，悬停气泡时浮现（时间戳后续补充） */
.user-action-bar {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-top: 4px;
  opacity: 0;
  transition: opacity 0.15s;
}
.msg-user:hover .user-action-bar {
  opacity: 1;
}
.msg-copy-btn {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 2px 8px;
  font-size: 11px;
  line-height: 1.5;
  color: var(--text-faint);
  background: var(--bg2);
  border: 1px solid var(--border);
  border-radius: 6px;
  cursor: pointer;
}
.msg-copy-btn:hover {
  color: var(--text-bright);
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

/* 本地文件链接：虚线强调，与普通 http 链接区分，点击走内嵌预览 */
.md-body :deep(a[data-file-link]) {
  color: var(--accent);
  text-decoration: underline dotted;
  cursor: pointer;
}

/* ---- 媒体语法渲染：图片网格 / 视频播放器 / 明确失败态 ---- */
.md-body :deep(img.md-img) {
  display: block;
  max-width: min(420px, 100%);
  max-height: 320px;
  border-radius: 8px;
  border: 1px solid var(--border);
  margin: 6px 0;
  cursor: zoom-in;
  object-fit: contain;
}
.md-body :deep(video.md-video) {
  display: block;
  max-width: min(480px, 100%);
  border-radius: 8px;
  border: 1px solid var(--border);
  margin: 6px 0;
  background: #000;
}
.md-body :deep(.md-media-error) {
  display: inline-block;
  margin: 4px 0;
  padding: 3px 10px;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--bg2);
  color: var(--text-faint);
  font-size: 12px;
}

/* ---- mermaid 图表块：渲染中/成功/失败三态 ----
   markdown.ts 只产出占位容器（源码已转义在 pre.mermaid-src 内），
   mermaidRenderer 异步注入净化后的 SVG；流式重建 DOM 后凭缓存 key 秒回。 */
.md-body :deep(.mermaid-block) {
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--bg2);
  padding: 8px 10px;
  margin: 8px 0;
  overflow-x: auto;
}
.md-body :deep(.mermaid-head) {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 4px;
}
.md-body :deep(.mermaid-tag) {
  font-size: 11px;
  color: var(--text-faint);
}
.md-body :deep(.mermaid-copy) {
  margin-left: auto;
  font-size: 11px;
  padding: 2px 8px;
  color: var(--text-faint);
  background: var(--bg3);
  border: 1px solid var(--border);
  border-radius: 6px;
  cursor: pointer;
}
.md-body :deep(.mermaid-copy:hover) {
  color: var(--text-bright);
}
.md-body :deep(.mermaid-block svg) {
  max-width: 100%;
  height: auto;
}
/* 渲染中占位 */
.md-body :deep(.mermaid-block[data-state='pending'])::after,
.md-body :deep(.mermaid-block[data-state='rendering'])::after {
  content: '图表渲染中…';
  display: block;
  text-align: center;
  font-size: 12px;
  color: var(--text-faint);
  padding: 8px 0;
}
/* 渲染失败：显示源码 + 明确提示（渲染器加载失败同此态） */
.md-body :deep(.mermaid-block[data-state='error']) .mermaid-src {
  display: block;
  white-space: pre-wrap;
  word-break: break-word;
  font-family: var(--mono);
  font-size: 12px;
  line-height: 1.6;
  margin: 0;
  color: var(--text);
}
.md-body :deep(.mermaid-block[data-state='error'])::after {
  content: '⚠️ 图表渲染失败，已显示 Mermaid 源码。可点击「复制源码」到其他渲染工具查看。';
  display: block;
  font-size: 12px;
  color: var(--orange);
  margin-top: 6px;
}
/* 成功态与渲染中隐藏源码（error 态由上面规则放开） */
.md-body :deep(.mermaid-block[data-state='done'] .mermaid-src),
.md-body :deep(.mermaid-block[data-state='pending'] .mermaid-src),
.md-body :deep(.mermaid-block[data-state='rendering'] .mermaid-src) {
  display: none;
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

<template>
  <div class="msg" :class="msg.role === 'user' ? 'msg-user' : msg.role === 'error' ? 'msg-error' : msg.role === 'notice' ? 'msg-notice' : 'msg-assistant'">
    <template v-if="msg.role === 'user'">
      <div v-if="store.guiTheme?.id === 'builtin.qq2009'" class="qq09-msg-sender">
        <b class="qq09-sender-name">木偶</b>
      </div>
      <div v-if="editing" class="user-edit-card">
        <textarea
          ref="editInputRef"
          v-model="editText"
          class="user-edit-input"
          data-test="user-edit-input"
          rows="2"
          placeholder="修改消息内容…"
          @input="autoResizeEditInput"
          @keydown="onEditKeydown"
          @paste="onEditPaste"
        ></textarea>
        <div
          v-if="editImages.length"
          class="edit-img-grid"
          data-test="user-edit-images"
        >
          <div v-for="(img, i) in editImages" :key="i" class="edit-img-cell">
            <img :src="img" class="edit-img" alt="待发送的图片">
            <button
              type="button"
              class="edit-img-remove"
              title="移除这张图片"
              data-test="user-edit-image-remove"
              :disabled="editSubmitting"
              @click="removeEditImage(i)"
            >×</button>
          </div>
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
        <div class="user-edit-footer">
          <input
            ref="editImageInput"
            type="file"
            accept="image/png,image/jpeg,image/gif,image/webp"
            multiple
            class="edit-file-input"
            data-test="user-edit-image-input"
            @change="onEditImagesPicked"
          >
          <div class="user-edit-footer-left">
            <button
              type="button"
              class="edit-icon-btn"
              data-test="user-edit-image-add"
              title="添加图片（也支持粘贴）"
              aria-label="添加图片"
              :disabled="editSubmitting"
              @click="pickEditImages"
            >
              <AppIcon name="paperclip" :size="14" :stroke-width="1.85" />
            </button>
            <span class="user-edit-hint" :class="{ 'is-warning': store.running }">
              <AppIcon :name="store.running ? 'triangle-alert' : 'info'" :size="12" />
              <span>{{ editHint }}</span>
            </span>
          </div>
          <div class="user-edit-footer-actions">
            <button
              type="button"
              class="edit-btn edit-btn-cancel"
              data-test="user-edit-cancel"
              :disabled="editSubmitting"
              @click="cancelEdit"
            >
              <span>取消</span>
            </button>
            <button
              type="button"
              class="edit-btn edit-btn-submit"
              data-test="user-edit-submit"
              :disabled="editSubmitting || (!editText.trim() && !editImages.length)"
              @click="submitEdit"
            >
              <AppIcon v-if="editSubmitting" name="loader" :size="12.5" :stroke-width="2.2" class="edit-spin" />
              <AppIcon v-else name="arrow-up" :size="13" :stroke-width="2.2" />
              <span>{{ editSubmitting ? '处理中…' : '保存并重新生成' }}</span>
            </button>
          </div>
        </div>
      </div>
      <template v-else>
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
        <div v-if="msg.content || canEdit" class="user-action-bar">
          <button
            v-if="canEdit"
            type="button"
            class="user-action-btn"
            title="编辑并重新生成"
            aria-label="编辑并重新生成"
            data-test="user-edit-btn"
            @click="startEdit"
          >
            <AppIcon name="pencil" :size="13.5" :stroke-width="1.85" />
          </button>
          <button
            v-if="msg.content"
            type="button"
            class="user-action-btn"
            :class="{ copied: userCopied }"
            :title="userCopied ? '已复制' : '复制'"
            :aria-label="userCopied ? '已复制' : '复制'"
            data-test="user-copy-btn"
            @click="copyUserMessage"
          >
            <AppIcon :name="userCopied ? 'check' : 'copy'" :size="13.5" :stroke-width="userCopied ? 2.2 : 1.85" />
          </button>
        </div>
      </template>
    </template>

    <div v-else-if="msg.role === 'assistant'" class="avatar-row">
      <div class="assistant-body">
        <div v-if="showName || msg.cancelled" class="assistant-name">
          <template v-if="showName">
            <b class="an-agent">{{ store.agentName }}</b>
            <span v-if="msg.model_name" class="an-model">{{ msg.model_name }}</span>
          </template>
          <span v-if="msg.cancelled" class="cancel-badge">已取消</span>
        </div>

        <div v-if="showProcess && msg.reasoning_content && msg.reasoning_content.trim()" class="think-block">
          <FoldCard v-model:open="thinkingOpen" caret="end">
            <template #title>
              <span class="tk-head">
                <AppIcon class="tk-brain" name="lightbulb" :size="13" :stroke-width="1.8" />
                <span class="tk-label" :class="{ 'is-thinking': !!thinkLiveLine }">{{ thinkLabel }}</span>
                <template v-if="thinkLiveLine">
                  <span class="tk-sep">·</span>
                  <span class="tk-live" data-test="think-live-line">
                    <span class="tk-live-text">{{ thinkLiveLine }}</span>
                  </span>
                </template>
                <template v-else-if="!thinkingOpen && thinkSummaryLine">
                  <span class="tk-sep">·</span>
                  <span class="tk-summary">{{ thinkSummaryLine }}</span>
                </template>
              </span>
            </template>
            <div
              ref="tkScrollRef"
              class="tk-cnt"
              :class="{ 'is-scrollable': tkScrollable, 'at-top': tkAtTop, 'at-bottom': tkAtBottom }"
              @scroll="syncTkScroll"
            >{{ msg.reasoning_content }}</div>
          </FoldCard>
        </div>

        <!-- Markdown 文本内容（代码块复制按钮通过事件委托处理点击）；
             文件链接先渲染、后经后端批量校验，目录/不存在的路径降级为纯文本；
             媒体清单（media_v1）存在时 ![..](..)/[video:..](..) 渲染为图片/视频 -->
        <div
          v-if="showAnswer && msg.content && msg.content.trim()"
          ref="mdBodyRef"
          class="md-body"
          @click="onMdBodyClick"
        >
          <div v-for="b in mdBlocks" :key="b.key" class="md-block" v-html="b.html"></div>
        </div>

        <ToolExecutionTrace
          v-if="showProcess && ((msg.tool_calls && msg.tool_calls.length > 0) || (msg.legacy_diffs && msg.legacy_diffs.length > 0))"
          :calls="msg.tool_calls || []"
          :legacy-diffs="msg.legacy_diffs || []"
          :state="msg.execution_state || 'completed'"
        />

        <div v-if="showAnswer && msg.error_note" class="error-note">
          <AppIcon name="triangle-alert" :size="13" /> {{ msg.error_note }}
        </div>

        <div v-if="showAnswer && msg.error_note && isLoginExpiredError(msg.error_note)" class="login-again-row">
          <button
            type="button"
            class="login-again-btn"
            data-test="login-again-btn"
            @click="onLoginAgain"
          >
            <AppIcon name="refresh" :size="12" /> 重新登录
          </button>
        </div>

        <div v-if="showAnswer && msg.error_note && store.resumeAvailable" class="resume-continue" data-test="resume-continue">
          <button
            type="button"
            class="resume-btn"
            data-test="resume-continue-btn"
            :disabled="store.resumeBusy"
            @click="store.runResume()"
          >
            <AppIcon name="play" :size="12" /> 从断点继续
          </button>
          <span class="resume-hint">仅回灌已完成的调用；含写操作的调用需手动重做</span>
        </div>
        <ul v-if="showAnswer && msg.error_note && store.resumeBlockers.length" class="resume-blockers" data-test="resume-blockers">
          <li
            v-for="item in store.resumeBlockers"
            :key="item.call_ordinal"
            data-test="resume-blocker-item"
          >
            第 {{ item.call_ordinal }} 个调用 {{ item.tool_name }} 需手动重做
          </li>
        </ul>

        <div v-if="showAnswer && showWorking" class="working-line">
          <span class="spin"></span>
          <span class="wl-text">{{ store.agentName }}正在干活<span class="wl-dots"><i>.</i><i>.</i><i>.</i></span></span>
          <span v-if="elapsed >= 3" class="wl-elapsed">已等待 {{ elapsed }}s</span>
        </div>

        <div v-else-if="showAnswer && showStreamingSpeed && isStreamingSpeedVisible" class="streaming-speed-pill" data-test="streaming-speed">
          <AppIcon name="orbit" :size="12" class="zap-pulse" />
          <span>{{ liveSpeedText }}</span>
        </div>

        <div
          v-else-if="showAnswer && showMetrics && msg.metrics && (msg.metrics.token_speed || msg.metrics.duration_s)"
          class="msg-metrics-bar"
          :title="metricsTooltip"
          data-test="msg-metrics"
        >
          <span v-if="msg.metrics.token_speed" class="metric-item">
            <AppIcon name="orbit" :size="12" class="metric-icon" />
            <span class="metric-val">{{ msg.metrics.token_speed }}</span> t/s
          </span>
          <span v-if="msg.metrics.token_speed && msg.metrics.duration_s" class="metric-sep">·</span>
          <span v-if="msg.metrics.duration_s" class="metric-item">{{ formatDuration(msg.metrics.duration_s) }}</span>
          <template v-if="msg.metrics.completion_tokens">
            <span class="metric-sep">·</span>
            <span class="metric-item">{{ msg.metrics.completion_tokens }} tokens</span>
          </template>
        </div>
      </div>
    </div>

    <div v-else-if="msg.role === 'error'" class="error-card">
      <span class="error-icon"><AppIcon name="triangle-alert" :size="16" /></span>
      <div class="error-body">
        <div class="error-title">运行错误</div>
        <div class="error-text">{{ msg.content }}</div>
        <!-- 登录过期（账号 cookie / 专属 Key 失效）：错误旁直接给登录入口，
             不要求用户自己去找左下角账号卡片 -->
        <div v-if="isLoginExpiredError(msg.content)" class="login-again-row">
          <button
            type="button"
            class="login-again-btn"
            data-test="login-again-btn"
            @click="onLoginAgain"
          >
            <AppIcon name="refresh" :size="12" /> 重新登录
          </button>
        </div>
      </div>
    </div>

    <div v-else-if="msg.role === 'notice'" class="notice-card">
      <span class="notice-icon"><AppIcon name="info" :size="15" /></span>
      <div class="notice-body">
        <span class="notice-title">提示</span>
        <span class="notice-text">{{ msg.content }}</span>
      </div>
      <button
        v-if="exportNoticePath"
        type="button"
        class="notice-action-btn"
        data-test="notice-open-export-dir"
        title="打开导出目录并定位文件"
        :disabled="openingExportDir"
        @click="onOpenExportDir"
      >
        <AppIcon name="folder-open" :size="12" />
        <span>打开目录</span>
      </button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, watch, nextTick, onMounted, onUnmounted } from 'vue';
import { isLoopbackPageHost, useAppStore, type ChatMessage } from '../stores/appStore';
import { renderMarkdownDetailed, COPY_ICON_SVG, CHECK_ICON_SVG } from '../utils/markdown';
import { splitStableBlocks } from '../utils/markdownBlocks';
import { renderMermaidBlocks } from '../utils/mermaidRenderer';
import { canOpenInRightPanel, toAbsoluteLocalPath } from '../utils/localPath';
import { useIsMobile } from '../composables/useViewport';
import { formatTokenSpeed, formatDuration } from '../utils/tokenSpeed';
import { isLoginExpiredError } from '../utils/loginExpired';
import { extractExportSuccessPath } from '../utils/exportNotice';
import AppIcon from './AppIcon.vue';
import FoldCard from './FoldCard.vue';
import ToolExecutionTrace from './ToolExecutionTrace.vue';

const props = withDefaults(defineProps<{
  msg: ChatMessage;
  showName?: boolean;
  showMetrics?: boolean;
  /** 是否显示流式速度胶囊（TurnGroup 方案A 关闭避免 t/s 刷屏） */
  showStreamingSpeed?: boolean;
  showProcess?: boolean;
  showAnswer?: boolean;
}>(), {
  showName: true,
  showMetrics: true,
  showStreamingSpeed: true,
  showProcess: true,
  showAnswer: true,
});

const store = useAppStore();

function onLoginAgain() {
  store.openTrLoginModal();
  void store.refreshTrAuth();
}

const openingExportDir = ref(false);
const exportNoticePath = computed(() => {
  if (props.msg.role !== 'notice' || !isLoopbackPageHost()) return '';
  return extractExportSuccessPath(props.msg.content);
});

async function onOpenExportDir() {
  const target = exportNoticePath.value;
  if (!target || openingExportDir.value) return;
  openingExportDir.value = true;
  try {
    await store.revealInExplorer(target);
  } finally {
    openingExportDir.value = false;
  }
}
// 移动端（<=768px）右面板整体隐藏：HTML 文件点击只能回退源码预览弹窗
const isMobile = useIsMobile();
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

const isLatestAssistant = computed(() => {
  const msgs = store.messages;
  return msgs.length > 0 && msgs[msgs.length - 1] === props.msg;
});

const isStreamingSpeedVisible = computed(() => {
  if (props.msg.role !== 'assistant' || !store.running) return false;
  if (!isLatestAssistant.value) return false;
  if (showWorking.value) return false;
  const m = props.msg;
  const hasContent = !!(m.content && m.content.trim()) || !!(m.reasoning_content && m.reasoning_content.trim());
  return hasContent;
});

const liveSpeedText = computed(() => {
  const speed = store.streamingSpeed || 0;
  return speed > 0 ? formatTokenSpeed(speed) : '正在输出...';
});

const metricsTooltip = computed(() => {
  const m = props.msg.metrics;
  if (!m) return '';
  const parts: string[] = [];
  if (m.ttft_ms != null && m.ttft_ms > 0) {
    parts.push(`首字延迟: ${m.ttft_ms}ms`);
  }
  if (m.output_duration_s != null && m.output_duration_s > 0) {
    parts.push(`生成耗时: ${m.output_duration_s}s`);
  } else if (m.duration_s != null && m.duration_s > 0) {
    parts.push(`总耗时: ${m.duration_s}s`);
  }
  if (m.completion_tokens) {
    parts.push(`输出: ${m.completion_tokens} tokens`);
  }
  if (m.token_speed) {
    parts.push(`平均速度: ${m.token_speed} t/s`);
  }
  return parts.join(' · ');
});

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

const localThinkOverride = ref<boolean | null>(null);
const thinkingOpen = computed({
  get: () => (localThinkOverride.value !== null ? localThinkOverride.value : store.showThinking),
  set: (v: boolean) => { localThinkOverride.value = v; },
});

// 折叠态标题行预览的最大字符数：超长行只留结尾（"正在输出"的尾部才是最新推进内容），
// 避免一整段没有换行的思考把标题塞满开头几个字、看上去像不动
const THINK_LIVE_MAX = 90;

function toPlainHeadline(line: string): string {
  const plain = line
    .replace(/^\s*```[A-Za-z0-9_+-]*/, '')
    .replace(/^[\s>#*+\-–—\d.、:：)）]+/, '')
    .replace(/\*\*|__|`/g, '')
    .replace(/\s+/g, ' ')
    .trim();
  return plain.length > THINK_LIVE_MAX ? `…${plain.slice(-THINK_LIVE_MAX)}` : plain;
}

const thinkSummaryLine = computed(() => {
  const raw = props.msg.reasoning_content || '';
  if (!raw.trim()) return '';
  const lines = raw.split('\n');
  for (let i = 0; i < lines.length; i += 1) {
    const plain = toPlainHeadline(lines[i]);
    if (plain) return plain;
  }
  return '';
});

const thinkLiveLine = computed(() => {
  if (thinkingOpen.value) return '';
  if (!store.running || !isLatestAssistant.value) return '';
  const raw = props.msg.reasoning_content || '';
  if (!raw.trim()) return '';
  const lines = raw.split('\n');
  for (let i = lines.length - 1; i >= 0; i -= 1) {
    const plain = toPlainHeadline(lines[i]);
    if (plain) return plain;
  }
  return '';
});

const thinkStartAt = ref<number | null>(null);
const thinkEndedAt = ref<number | null>(null);
const thinkTick = ref(Date.now());
let thinkTicker: number | null = null;

const thinkStreaming = computed(() => {
  const raw = (props.msg.reasoning_content || '').trim();
  return !!raw && store.running && isLatestAssistant.value;
});

watch(
  thinkStreaming,
  (streaming) => {
    if (streaming) {
      if (thinkStartAt.value === null) {
        thinkStartAt.value = Date.now();
        thinkTick.value = thinkStartAt.value;
      }
      return;
    }
    if (thinkStartAt.value !== null && thinkEndedAt.value === null) {
      thinkEndedAt.value = Date.now();
      thinkTick.value = thinkEndedAt.value;
    }
  },
  { immediate: true },
);

const thinkDurationSec = computed(() => {
  const start = thinkStartAt.value;
  if (start === null) return null;
  const end = thinkEndedAt.value ?? thinkTick.value;
  return Math.max(1, Math.round((end - start) / 1000));
});

const thinkLabel = computed(() => {
  if (thinkLiveLine.value) return '正在思考';
  const sec = thinkDurationSec.value;
  return sec === null ? '思考' : `思考 · ${sec} 秒`;
});

watch(
  () => thinkingOpen.value && thinkStreaming.value,
  (ticking) => {
    if (thinkTicker !== null) {
      window.clearInterval(thinkTicker);
      thinkTicker = null;
    }
    if (ticking) {
      thinkTicker = window.setInterval(() => {
        thinkTick.value = Date.now();
      }, 1000);
    }
  },
  { immediate: true },
);

const tkScrollRef = ref<HTMLElement | null>(null);
const tkAtTop = ref(true);
const tkAtBottom = ref(true);
const tkScrollable = ref(false);

function syncTkScroll() {
  const el = tkScrollRef.value;
  if (!el) return;
  tkScrollable.value = el.scrollHeight > el.clientHeight + 1;
  tkAtTop.value = el.scrollTop <= 1;
  tkAtBottom.value = el.scrollTop + el.clientHeight >= el.scrollHeight - 1;
}

watch(
  () => props.msg.reasoning_content,
  async () => {
    await nextTick();
    const el = tkScrollRef.value;
    if (!el) return;
    if (tkAtBottom.value) el.scrollTop = el.scrollHeight;
    syncTkScroll();
  },
);

watch(thinkingOpen, async () => {
  await nextTick();
  syncTkScroll();
});

onMounted(async () => {
  await nextTick();
  syncTkScroll();
});

onUnmounted(() => {
  if (thinkTicker !== null) {
    window.clearInterval(thinkTicker);
    thinkTicker = null;
  }
});

const mediaCtx = computed(() =>
  props.msg.media && props.msg.media.length ? { items: props.msg.media } : undefined
);

// 媒体清单指纹。清单是回合收尾（message_final）才补挂到消息上的，而流式期间图片
// 语法已经先按「无清单」退化渲染成文件链接并写进 blockCache；缓存 key 只含「起始
// 媒体序号 + 块源码」，清单后补时 key 不变，旧 HTML 会被直接命中，图片永远不显示
// （切走再回来才恢复）。因此指纹一变必须整体失效已完成块缓存。
const mediaSignature = computed(() =>
  (props.msg.media || [])
    .map((m) => `${m.kind}:${m.url || m.error_code || m.digest || ''}`)
    .join('|')
);
let lastMediaSig: string | null = null;

const blockCache = new Map<string, { html: string; mediaCount: number }>();

interface MdBlock {
  key: string;
  html: string;
}

const mdBlocks = computed<MdBlock[]>(() => {
  // media 清单指纹变化即整体失效已完成块缓存（原因见 mediaSignature 注释）
  if (mediaSignature.value !== lastMediaSig) {
    lastMediaSig = mediaSignature.value;
    blockCache.clear();
  }
  const src = props.msg.content || '';
  if (!src.trim()) return [];
  const { blocks, tail } = splitStableBlocks(src);
  const out: MdBlock[] = [];
  let ordinal = 0;
  for (const block of blocks) {
    const key = `${ordinal}|${block}`;
    let cached = blockCache.get(key);
    if (!cached) {
      const rendered = renderMarkdownDetailed(block, mediaCtx.value, ordinal);
      cached = { html: rendered.html, mediaCount: rendered.mediaCount };
      blockCache.set(key, cached);
    }
    ordinal += cached.mediaCount;
    out.push({ key, html: cached.html });
  }
  out.push({
    key: 'tail',
    html: renderMarkdownDetailed(tail, mediaCtx.value, ordinal).html,
  });
  return out;
});

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

function onMdBodyClick(e: MouseEvent) {
  const copyBtn = (e.target as HTMLElement).closest('button.md-img-copy');
  if (copyBtn) {
    e.preventDefault();
    const src = copyBtn.getAttribute('data-copy-src') || '';
    if (src) copyMdImageToClipboard(src, copyBtn as HTMLButtonElement);
    return;
  }

  const saveBtn = (e.target as HTMLElement).closest('button.md-img-save, button.md-video-save');
  if (saveBtn) {
    e.preventDefault();
    const src = saveBtn.getAttribute('data-save-src') || '';
    if (src) saveMdImageAs(src, saveBtn.getAttribute('data-save-name') || '', saveBtn as HTMLButtonElement);
    return;
  }

  const mermaidSvg = (e.target as HTMLElement).closest('.mermaid-svg');
  if (mermaidSvg) {
    const svg = mermaidSvg.innerHTML;
    if (svg.trim()) store.openLightbox([svg], 0);
    return;
  }

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
    // html / pdf / md 交给右栏（网页直接渲染，md 先渲染成预览页）；
    // 移动端没有右面板，一律回退源码预览弹窗
    if (canOpenInRightPanel(target) && !isMobile.value) {
      void store.openInRightPanel(target);
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
    });
}

// ---- 媒体图片"复制原图" ----
// 剪贴板写入用 image/png（ClipboardItem 的通用兼容格式）：PNG 原样写入，
// 其余格式经 canvas 转码；GIF 会复制为静态首帧。媒体服务已带 CORS 头，
// fetch 跨端口取图可行；页面是 127.0.0.1 安全上下文，clipboard.write 可用。
async function blobToPngBlob(blob: Blob): Promise<Blob> {
  const bitmap = await createImageBitmap(blob);
  try {
    const canvas = document.createElement('canvas');
    canvas.width = bitmap.width;
    canvas.height = bitmap.height;
    canvas.getContext('2d')!.drawImage(bitmap, 0, 0);
    return await new Promise<Blob>((resolve, reject) => {
      canvas.toBlob(
        b => (b ? resolve(b) : reject(new Error('canvas toBlob 失败'))),
        'image/png',
      );
    });
  } finally {
    bitmap.close();
  }
}

async function copyMdImageToClipboard(src: string, btn: HTMLButtonElement) {
  const setFeedback = (ok: boolean) => {
    const label = ok ? '已复制' : '复制失败';
    btn.classList.toggle('copied', ok);
    btn.classList.toggle('copy-failed', !ok);
    btn.innerHTML = (ok ? CHECK_ICON_SVG : COPY_ICON_SVG) + `<span class="md-img-copy-text">${label}</span>`;
    window.setTimeout(() => {
      btn.classList.remove('copied', 'copy-failed');
      btn.innerHTML = COPY_ICON_SVG + '<span class="md-img-copy-text">复制</span>';
    }, 1500);
  };
  try {
    const resp = await fetch(src);
    if (!resp.ok) throw new Error(`媒体服务返回 ${resp.status}`);
    const blob = await resp.blob();
    const png = blob.type === 'image/png' ? blob : await blobToPngBlob(blob);
    await navigator.clipboard.write([new ClipboardItem({ 'image/png': png })]);
    setFeedback(true);
  } catch {
    setFeedback(false);
  }
}

async function saveMdImageAs(src: string, suggestedName: string, btn: HTMLButtonElement) {
  const label = btn.querySelector('.md-img-save-text, .md-video-save-text');
  const restore = () => {
    btn.classList.remove('saved', 'save-failed');
    if (label) label.textContent = '另存为';
  };
  const feedback = (text: string, cls: string) => {
    btn.classList.add(cls);
    if (label) label.textContent = text;
    window.setTimeout(restore, 1500);
  };
  btn.disabled = true;
  try {
    const res = await store.saveMediaImage(src, suggestedName);
    if (res && res.ok) {
      feedback('已保存', 'saved');
    } else if (!res || !res.cancelled) {
      feedback('保存失败', 'save-failed');
      if (res && res.msg) store.notifyChat(`另存失败：${res.msg}`);
    }
  } finally {
    btn.disabled = false;
  }
}

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
    });
}

// ---- 文件链接后置校验：目录 / 不存在的路径降级为纯文本 ----
// 渲染层（renderMarkdown）是纯字符串函数无法查磁盘，先按路径形态渲染成链接，
// 挂载/内容变化后再由后端批量确认真实类型；目录与不存在的路径还原为纯文本，
// 避免用户点击后才发现「无法预览」。校验失败（如 API 未就绪）时保留链接原样。
const mdBodyRef = ref<HTMLElement | null>(null);
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
    if (seq !== pathCheckSeq) return;
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

async function copyUserMessage() {
  const text = props.msg.content || '';
  if (!text) return;
  await copyToClipboard(text);
  userCopied.value = true;
  if (userCopiedTimer) window.clearTimeout(userCopiedTimer);
  userCopiedTimer = window.setTimeout(() => { userCopied.value = false; }, 1500);
}

const editing = ref(false);
const editText = ref('');
const editSubmitting = ref(false);
const editInputRef = ref<HTMLTextAreaElement | null>(null);
const editImages = ref<string[]>([]);
const editImageInput = ref<HTMLInputElement | null>(null);

const canEdit = computed(() => !!props.msg._msg_id);

const editHint = computed(() => (store.running
  ? '保存将中断当前回合，并删除该回合产生的记录'
  : '该回合的文件改动不会随编辑回滚'));

function autoResizeEditInput() {
  const el = editInputRef.value;
  if (!el) return;
  el.style.height = 'auto';
  el.style.height = `${Math.min(Math.max(el.scrollHeight, 44), 260)}px`;
}

async function startEdit() {
  if (!canEdit.value) return;
  editText.value = props.msg.content || '';
  editImages.value = [...(props.msg.images || [])];
  editing.value = true;
  await nextTick();
  autoResizeEditInput();
  const el = editInputRef.value;
  if (el) {
    el.focus();
    el.setSelectionRange(el.value.length, el.value.length);
  }
}

function cancelEdit() {
  if (editSubmitting.value) return;
  editing.value = false;
  editText.value = '';
  editImages.value = [];
}

function removeEditImage(index: number) {
  if (editSubmitting.value) return;
  editImages.value.splice(index, 1);
}

function pickEditImages() {
  if (editSubmitting.value) return;
  editImageInput.value?.click();
}

function readFileAsDataUrl(file: File): Promise<string> {
  return new Promise((resolve) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || ''));
    reader.onerror = () => resolve('');
    reader.readAsDataURL(file);
  });
}

async function addEditImageFiles(files: File[]) {
  for (const file of files) {
    if (!/^image\/(png|jpeg|gif|webp)$/.test(file.type)) continue;
    const url = await readFileAsDataUrl(file);
    if (url) editImages.value.push(url);
  }
}

async function onEditImagesPicked(e: Event) {
  const input = e.target as HTMLInputElement;
  const files = Array.from(input.files || []);
  input.value = '';
  await addEditImageFiles(files);
}

async function onEditPaste(e: ClipboardEvent) {
  const items = Array.from(e.clipboardData?.items || []);
  const files: File[] = [];
  for (const item of items) {
    if (item.kind === 'file' && item.type.startsWith('image/')) {
      const file = item.getAsFile();
      if (file) files.push(file);
    }
  }
  if (!files.length) return;
  // 有图片就拦下默认粘贴，避免把图片当成路径文本塞进输入框
  e.preventDefault();
  await addEditImageFiles(files);
}

async function submitEdit() {
  if (editSubmitting.value) return;
  const text = editText.value.trim();
  if (!text && !editImages.value.length) return;
  editSubmitting.value = true;
  try {
    const paths: string[] = [];
    for (const url of editImages.value) {
      if (!url.startsWith('data:')) {
        paths.push(url);
        continue;
      }
      const saved = await store.saveClipboardImage(url);
      // 落盘失败（超 8MB / 格式不支持）已由 store 提示，中止以免图片静默丢失
      if (!saved) return;
      paths.push(saved);
    }
    const ok = await store.editUserMessage(props.msg, text, paths);
    if (ok) editing.value = false;
  } finally {
    editSubmitting.value = false;
  }
}

function onEditKeydown(e: KeyboardEvent) {
  // 输入法组合中的 Enter 是在确认候选词，不能当成提交
  if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
    e.preventDefault();
    void submitEdit();
  } else if (e.key === 'Escape') {
    e.preventDefault();
    cancelEdit();
  }
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
  background: var(--xy-user-bubble, var(--bg2));
  border: none;
  border-radius: 16px;
  padding: 8px 16px;
  max-width: 82%;
  font-size: 14px;
  line-height: 1.625;
  color: var(--xy-text, var(--text));
}
.user-text {
  white-space: pre-wrap;
  word-break: break-word;
}
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
.user-action-bar {
  display: flex;
  align-items: center;
  gap: 2px;
  margin-top: 4px;
  padding-right: 2px;
  opacity: 0;
  transition: opacity 0.14s ease;
}
.msg-user:hover .user-action-bar,
.msg-user:focus-within .user-action-bar,
.user-action-bar:has(.copied) {
  opacity: 1;
}
@media (hover: none) {
  .user-action-bar {
    opacity: 0.72;
  }
}
.user-action-btn {
  width: 26px;
  height: 26px;
  padding: 0;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: none;
  border-radius: 7px;
  background: transparent;
  color: var(--xy-text-3, var(--text-faint));
  cursor: pointer;
  transition: background 0.14s ease, color 0.14s ease, transform 0.1s ease;
}
.user-action-btn:hover {
  background: var(--xy-hover, var(--bg2));
  color: var(--xy-text, var(--text-bright));
}
.user-action-btn:active {
  transform: scale(0.94);
}
.user-action-btn:focus-visible {
  outline: 2px solid var(--xy-accent, var(--accent));
  outline-offset: 1px;
}
.user-action-btn.copied {
  color: var(--xy-green, var(--green));
  background: var(--xy-green-soft, rgba(22, 163, 74, 0.1));
}
.user-edit-card {
  width: min(100%, 660px);
  background: var(--xy-card, var(--bg2));
  border: 1px solid var(--xy-border-strong, var(--border));
  border-radius: 16px;
  padding: 12px 14px 10px;
  box-shadow: var(--xy-shadow-composer, var(--xy-shadow-sm));
  transition: border-color 0.16s ease, box-shadow 0.16s ease;
}
.user-edit-card:focus-within {
  border-color: var(--xy-accent, var(--accent));
  box-shadow: 0 0 0 3px var(--xy-accent-soft, var(--accent-soft)), var(--xy-shadow-composer, none);
}
.user-edit-input {
  display: block;
  width: 100%;
  min-height: 44px;
  max-height: 260px;
  padding: 0;
  border: 0;
  outline: none;
  background: transparent;
  color: var(--xy-text, var(--text));
  font-family: var(--sans);
  font-size: 14px;
  line-height: 1.625;
  resize: none;
  overflow-y: auto;
}
.user-edit-input::placeholder {
  color: var(--xy-text-3, var(--text-faint));
}
/* 编辑态的图片网格：紧凑圆角缩略图，右上角带悬浮移除按钮 */
.edit-img-grid {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 10px;
}
.edit-img-cell {
  position: relative;
  width: 64px;
  height: 64px;
  flex-shrink: 0;
}
.edit-img {
  width: 100%;
  height: 100%;
  object-fit: cover;
  border-radius: 10px;
  border: 1px solid var(--xy-border, var(--border));
  background: var(--xy-surface, var(--bg3));
  display: block;
}
.edit-img-remove {
  position: absolute;
  top: -5px;
  right: -5px;
  width: 18px;
  height: 18px;
  padding: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  border: 1px solid rgba(255, 255, 255, 0.2);
  border-radius: 50%;
  background: rgba(15, 15, 15, 0.72);
  color: #fff;
  font-size: 13px;
  line-height: 1;
  cursor: pointer;
  backdrop-filter: blur(4px);
}
.edit-img-remove:hover:not(:disabled) {
  background: rgba(15, 15, 15, 0.9);
}
.edit-img-remove:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.edit-file-input {
  display: none;
}
.user-edit-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 8px 12px;
  margin-top: 10px;
  padding-top: 8px;
  border-top: 1px solid var(--xy-border, var(--border));
}
.user-edit-footer-left {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
  flex: 1 1 auto;
}
.edit-icon-btn {
  width: 28px;
  height: 28px;
  padding: 0;
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: none;
  border-radius: 8px;
  background: transparent;
  color: var(--xy-text-2, var(--text-dim));
  cursor: pointer;
  transition: background 0.14s ease, color 0.14s ease;
}
.edit-icon-btn:hover:not(:disabled) {
  background: var(--xy-hover, var(--bg3));
  color: var(--xy-text, var(--text-bright));
}
.user-edit-hint {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: 11.5px;
  line-height: 1.4;
  color: var(--xy-text-3, var(--text-faint));
}
.user-edit-hint.is-warning {
  color: var(--xy-amber, var(--orange));
}
.user-edit-footer-actions {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-left: auto;
  flex-shrink: 0;
}
.edit-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 5px;
  height: 28px;
  padding: 0 12px;
  border-radius: 8px;
  font-family: inherit;
  font-size: 12.5px;
  font-weight: 500;
  line-height: 1;
  cursor: pointer;
  white-space: nowrap;
  transition: background 0.14s ease, border-color 0.14s ease, color 0.14s ease, opacity 0.14s ease, filter 0.14s ease;
}
.edit-btn-cancel {
  color: var(--xy-text-2, var(--text-dim));
  background: transparent;
  border: 1px solid var(--xy-border-strong, var(--border));
}
.edit-btn-cancel:hover:not(:disabled) {
  background: var(--xy-hover, var(--bg3));
  color: var(--xy-text, var(--text-bright));
}
.edit-btn-submit {
  padding: 0 13px;
  color: var(--xy-accent-fg, #ffffff);
  background: var(--xy-accent, var(--accent));
  border: 1px solid transparent;
}
.edit-btn-submit:hover:not(:disabled) {
  filter: brightness(1.08);
}
.edit-btn-submit:active:not(:disabled) {
  filter: brightness(0.96);
}
.edit-btn:disabled,
.edit-icon-btn:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}
.edit-spin {
  animation: spin 0.9s linear infinite;
}

.avatar-row {
  display: flex;
}
.assistant-body {
  flex: 1;
  min-width: 0;
}
.assistant-name {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  color: var(--xy-text-3, var(--text-faint));
  margin-bottom: 8px;
}
.assistant-name .an-agent {
  font-size: 13.5px;
  font-weight: 600;
  color: var(--xy-text, var(--text));
}
.assistant-name .an-model {
  font-family: var(--mono);
  font-variant-numeric: tabular-nums;
  font-size: 12px;
  color: var(--xy-text-3, var(--text-faint));
}
.cancel-badge {
  margin-left: 4px;
  font-size: 10.5px;
  color: var(--xy-amber, var(--orange));
  background: var(--xy-amber-soft, rgba(240, 136, 62, 0.12));
  border-radius: 6px;
  padding: 1px 7px;
}

.think-block {
  margin: 4px 0 6px;
  font-size: 12px;
  color: var(--xy-text-3, var(--text-faint));
}
.think-block :deep(.fold-head) {
  width: fit-content;
  max-width: 100%;
  gap: 6px;
  padding: 1px 0;
  color: inherit;
}
.think-block :deep(.fold-head:hover) {
  color: var(--xy-text-2, var(--text-dim));
}
.tk-head {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
}
.tk-brain {
  flex: none;
  opacity: 0.85;
}
.tk-label {
  flex: none;
  font-weight: 400;
  white-space: nowrap;
}
.tk-label.is-thinking {
  background: linear-gradient(
    90deg,
    var(--text-faint) 0%,
    var(--text-bright) 45%,
    var(--text-faint) 90%
  );
  background-size: 220% 100%;
  -webkit-background-clip: text;
  background-clip: text;
  color: transparent;
  animation: tk-shimmer 2.4s linear infinite;
}
@keyframes tk-shimmer {
  from { background-position: 120% 0; }
  to { background-position: -120% 0; }
}
.tk-sep {
  flex: none;
  opacity: 0.65;
}
.tk-live,
.tk-summary {
  display: inline-flex;
  align-items: center;
  flex: 1 1 auto;
  min-width: 0;
  font-weight: 400;
  letter-spacing: 0;
  color: inherit;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.tk-live-text {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.think-block :deep(.fold-caret) {
  opacity: 0.75;
  margin-left: 2px;
  transition: opacity 0.12s ease, transform 0.15s ease;
}
.think-block :deep(.fold-head:hover .fold-caret),
.think-block :deep(.fold-head:focus-visible .fold-caret),
.think-block :deep(.fold-caret.is-open) {
  opacity: 1;
}
.tk-cnt {
  margin: 4px 0 2px;
  padding-left: 20px;
  border-left: none;
  max-height: calc(1.6em * 10 + 2px);
  overflow-y: auto;
  overscroll-behavior: contain;
  scrollbar-gutter: stable;
  white-space: pre-wrap;
  font-family: inherit;
  font-size: 12px;
  line-height: 1.6;
  color: var(--xy-text-3, var(--text-faint));
}
.tk-cnt.is-scrollable.at-bottom:not(.at-top) {
  -webkit-mask-image: linear-gradient(to bottom, transparent 0, #000 14px);
  mask-image: linear-gradient(to bottom, transparent 0, #000 14px);
}
.tk-cnt.is-scrollable.at-top:not(.at-bottom) {
  -webkit-mask-image: linear-gradient(to bottom, #000 calc(100% - 14px), transparent 100%);
  mask-image: linear-gradient(to bottom, #000 calc(100% - 14px), transparent 100%);
}
.tk-cnt.is-scrollable:not(.at-top):not(.at-bottom) {
  -webkit-mask-image: linear-gradient(
    to bottom,
    transparent 0,
    #000 14px,
    #000 calc(100% - 14px),
    transparent 100%
  );
  mask-image: linear-gradient(
    to bottom,
    transparent 0,
    #000 14px,
    #000 calc(100% - 14px),
    transparent 100%
  );
}
@media (prefers-reduced-motion: reduce) {
  .tk-label.is-thinking {
    animation: none;
    background: none;
    color: var(--text-faint);
  }
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

.streaming-speed-pill {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  margin-top: 6px;
  padding: 2px 8px;
  font-size: 11px;
  font-family: var(--mono);
  color: var(--accent);
  background: var(--bg-surface-2);
  border: 1px solid var(--border-subtle);
  border-radius: 10px;
  user-select: none;
}
.streaming-speed-pill .zap-pulse {
  animation: zap-pulse-anim 1.2s ease-in-out infinite;
}
@keyframes zap-pulse-anim {
  0%, 100% { opacity: 0.5; transform: scale(0.9); }
  50% { opacity: 1; transform: scale(1.15); }
}

.msg-metrics-bar {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  margin-top: 6px;
  padding: 2px 6px;
  font-size: 11px;
  color: var(--text-faint);
  user-select: none;
  border-radius: 4px;
  transition: color 0.15s, background 0.15s;
}
.msg-metrics-bar:hover {
  color: var(--text-dim);
  background: var(--bg-hover, rgba(255, 255, 255, 0.04));
}
.msg-metrics-bar .metric-item {
  display: inline-flex;
  align-items: center;
  gap: 3px;
}
.msg-metrics-bar .metric-icon {
  color: var(--accent);
}
.msg-metrics-bar .metric-sep {
  opacity: 0.4;
}
.msg-metrics-bar .metric-val {
  font-weight: 500;
  font-family: var(--mono);
}

.md-body {
  margin-top: 0;
  font-size: 13.5px;
  line-height: 1.68;
  color: var(--xy-text, var(--text));
}

.md-body :deep(a[data-file-link]) {
  color: var(--accent);
  text-decoration: underline dotted;
  cursor: pointer;
}

.md-body :deep(.md-media-wrap) {
  position: relative;
  display: block;
  width: fit-content;
}
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
.md-body :deep(.md-img-actions),
.md-body :deep(.md-video-actions) {
  position: absolute;
  top: 14px;
  right: 14px;
  display: flex;
  gap: 6px;
  opacity: 0;
  transition: opacity 0.15s ease;
}
.md-body :deep(.md-media-wrap:hover .md-img-actions),
.md-body :deep(.md-media-wrap:hover .md-video-actions),
.md-body :deep(.md-img-actions:focus-within),
.md-body :deep(.md-video-actions:focus-within),
.md-body :deep(.md-img-actions:has(.copied)),
.md-body :deep(.md-img-actions:has(.saved)),
.md-body :deep(.md-img-actions:has(.copy-failed)),
.md-body :deep(.md-img-actions:has(.save-failed)),
.md-body :deep(.md-video-actions:has(.saved)),
.md-body :deep(.md-video-actions:has(.save-failed)) {
  opacity: 1;
}
.md-body :deep(.md-img-copy),
.md-body :deep(.md-img-save),
.md-body :deep(.md-video-save) {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 4px 8px;
  border: 1px solid rgba(255, 255, 255, 0.28);
  border-radius: 6px;
  background: rgba(15, 15, 15, 0.72);
  color: #fff;
  font-size: 12px;
  line-height: 1;
  cursor: pointer;
  transition: background 0.15s ease, border-color 0.15s ease;
  backdrop-filter: blur(4px);
}
.md-body :deep(.md-img-copy svg),
.md-body :deep(.md-img-save svg),
.md-body :deep(.md-video-save svg) {
  width: 13px;
  height: 13px;
}
.md-body :deep(.md-img-copy:disabled),
.md-body :deep(.md-img-save:disabled),
.md-body :deep(.md-video-save:disabled) {
  cursor: default;
  opacity: 0.75;
}
.md-body :deep(.md-img-copy:hover),
.md-body :deep(.md-img-save:hover),
.md-body :deep(.md-video-save:hover) {
  background: rgba(15, 15, 15, 0.9);
  border-color: rgba(255, 255, 255, 0.55);
  color: #fff;
}
.md-body :deep(.md-img-copy.copied),
.md-body :deep(.md-img-save.saved),
.md-body :deep(.md-video-save.saved) {
  color: #4ade80;
}
.md-body :deep(.md-img-copy.copy-failed),
.md-body :deep(.md-img-save.save-failed),
.md-body :deep(.md-video-save.save-failed) {
  color: #f87171;
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
.md-body :deep(.mermaid-svg) {
  cursor: zoom-in;
}
.md-body :deep(.mermaid-block[data-state='pending'])::after,
.md-body :deep(.mermaid-block[data-state='rendering'])::after {
  content: '图表渲染中…';
  display: block;
  text-align: center;
  font-size: 12px;
  color: var(--text-faint);
  padding: 8px 0;
}
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
  content: '⚠️ 图表渲染失败，已显示 Mermaid 源码。' attr(data-error-reason) '可点击「复制源码」到其他渲染工具查看。';
  display: block;
  font-size: 12px;
  color: var(--orange);
  margin-top: 6px;
}
.md-body :deep(.mermaid-block[data-state='done'] .mermaid-src),
.md-body :deep(.mermaid-block[data-state='pending'] .mermaid-src),
.md-body :deep(.mermaid-block[data-state='rendering'] .mermaid-src) {
  display: none;
}

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

.login-again-row {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 6px;
}
.login-again-btn {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 4px 12px;
  font-size: 12px;
  font-weight: 500;
  color: var(--accent);
  background: color-mix(in srgb, var(--accent) 10%, var(--bg3));
  border: 1px solid color-mix(in srgb, var(--accent) 45%, var(--border));
  border-radius: 6px;
  cursor: pointer;
}
.login-again-btn:hover {
  border-color: var(--accent);
  background: color-mix(in srgb, var(--accent) 16%, var(--bg3));
}

.resume-continue {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 6px;
}
.resume-btn {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 4px 10px;
  font-size: 12px;
  color: var(--text);
  background: var(--bg3);
  border: 1px solid var(--border);
  border-radius: 6px;
  cursor: pointer;
}
.resume-btn:hover:not(:disabled) {
  border-color: var(--accent);
}
.resume-btn:disabled {
  opacity: 0.6;
  cursor: default;
}
.resume-hint {
  font-size: 11.5px;
  color: var(--text-dim);
}
.resume-blockers {
  margin: 6px 0 0;
  padding-left: 18px;
  font-size: 12px;
  color: var(--red);
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

.msg-notice {
  width: 100%;
  display: flex;
  justify-content: flex-start;
}
.notice-card {
  width: 100%;
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding: 8px 12px;
  border: 1px solid var(--xy-border, var(--border));
  border-radius: 10px;
  background: var(--xy-surface, var(--bg2));
  font-size: 12.5px;
  line-height: 1.55;
}
.notice-icon {
  flex-shrink: 0;
  color: var(--xy-text-3, var(--text-faint));
  display: inline-flex;
  align-items: center;
  height: 19px;
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
  font-weight: 500;
  color: var(--xy-text-2, var(--text-dim));
}
.notice-text {
  flex: 1;
  min-width: 0;
  color: var(--xy-text-2, var(--text-dim));
  white-space: pre-wrap;
  word-break: break-word;
}
.notice-action-btn {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  flex-shrink: 0;
  align-self: center;
  height: 24px;
  margin: -2px 0;
  padding: 0 9px;
  border-radius: 6px;
  border: 1px solid var(--xy-border-strong, var(--border));
  background: var(--xy-card, var(--bg1));
  font-family: inherit;
  font-size: 12px;
  font-weight: 500;
  color: var(--xy-text-2, var(--text));
  cursor: pointer;
  white-space: nowrap;
  transition: color 0.14s, border-color 0.14s, background 0.14s;
}
.notice-action-btn:hover:not(:disabled) {
  border-color: var(--xy-accent, var(--accent));
  color: var(--xy-accent, var(--accent));
  background: var(--xy-hover, var(--bg3));
}
.notice-action-btn:disabled {
  opacity: 0.6;
  cursor: default;
}
.notice-action-btn:focus-visible {
  outline: 2px solid var(--xy-accent, var(--accent));
  outline-offset: 1px;
}
</style>

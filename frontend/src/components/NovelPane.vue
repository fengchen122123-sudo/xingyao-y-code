<template>
  <div class="novel-wrap" data-test="novel-pane">
    <!-- 加载中：只在首次拉取、且还没有任何快照时占位，重拉时保留旧内容避免闪白 -->
    <div v-if="phase === 'loading'" class="novel-hint" data-test="novel-loading">
      <AppIcon name="loader" class="icon-spin" :size="14" />
      <span>正在读取小说项目状态…</span>
    </div>

    <!-- 出错（含目录不存在 / 追踪文件损坏等结构化 error）：只显示错误文本，不崩-->
    <div v-else-if="phase === 'error'" class="novel-block" data-test="novel-error">
      <div class="novel-block-title">
        <AppIcon name="triangle-alert" :size="13" :stroke-width="2" /> 读取失败
      </div>
      <p class="novel-error-text">{{ errorText }}</p>
      <button class="novel-btn" data-test="novel-retry" @click="load()">重试</button>
    </div>

    <!-- 非小说项目：给出「初始化小说项目」引导，不直接写文件（设计 §7.3-4） -->
    <div v-else-if="phase === 'empty'" class="novel-empty" data-test="novel-empty">
      <div class="novel-empty-icon"><AppIcon name="file" :size="22" :stroke-width="1.6" /></div>
      <div class="novel-empty-title">{{ emptyTitle }}</div>
      <p class="novel-empty-desc">{{ emptyDesc }}</p>
      <button
        class="novel-btn primary"
        data-test="novel-init"
        :disabled="!canInit"
        :title="canInit ? '把初始化指令填进输入框，由你在对话里确认后发送' : '请先引入项目文件夹'"
        @click="initNovel()"
      >初始化小说项目</button>
    </div>

    <!-- 正常态：头部字数 / 章节列表 / 状态卡 -->
    <template v-else>
      <!-- 工具区：多书切换（同一工作区内多本小说时才出现）+ 导出全书 -->
      <div class="novel-toolbar">
        <select
          v-if="books.length > 1"
          class="novel-select"
          data-test="novel-book-select"
          title="切换同一工作区内的其他小说项目"
          :value="bookSelectValue"
          @change="onBookChange"
        >
          <option v-for="b in books" :key="b.path" :value="joinBookPath(b.path)">
            {{ b.title }}（已写 {{ b.chapters_written }} 章）
          </option>
        </select>
        <span class="novel-toolbar-spacer"></span>
        <button
          class="novel-btn small"
          data-test="novel-export-md"
          :disabled="exporting"
          title="导出全书为单个 Markdown 文件到「导出」目录"
          @click="exportNovel('md')"
        >导出 MD</button>
        <button
          class="novel-btn small"
          data-test="novel-export-txt"
          :disabled="exporting"
          title="导出全书为单个纯文本文件到「导出」目录"
          @click="exportNovel('txt')"
        >导出 TXT</button>
      </div>
      <!-- 导出结果：成功/失败都是内联提示，不弹模态、不动快照 -->
      <div v-if="exportHint" class="novel-export-hint" data-test="novel-export-hint">{{ exportHint }}</div>
      <div v-if="exportError" class="novel-export-error" data-test="novel-export-error">{{ exportError }}</div>

      <div class="novel-head">
        <div class="novel-title" data-test="novel-title">{{ titleText }}</div>
        <div class="novel-metrics">
          <span class="novel-metric">
            总字数 <b class="novel-num">{{ formatWords(totalWords) }}</b>
          </span>
          <span v-if="targetWords > 0" class="novel-metric">
            目标 <b class="novel-num">{{ formatWords(targetWords) }}</b>
            <span class="novel-pct">（{{ progressPercent }}%）</span>
          </span>
          <span class="novel-metric ok">
            今日 <b class="novel-num">{{ formatWords(todayWords) }}</b>
          </span>
        </div>
        <!-- 目标只在后端给出 target_words 时才有进度条；当前契约没有该字段，
             不编造分母，避免出现「永远 0%」的假进度 -->
        <div v-if="targetWords > 0" class="novel-bar" data-test="novel-progress">
          <span class="novel-bar-fill" :style="{ width: progressPercent + '%' }"></span>
        </div>
      </div>

      <div class="novel-sec">
        <div class="novel-sec-head">
          <span class="novel-sec-title">章节</span>
          <span class="novel-sec-meta">已写 {{ chaptersWritten }} 章</span>
        </div>
        <div class="novel-chapters" data-test="novel-chapter-list">
          <div v-if="chapters.length === 0" class="novel-hint">正文目录还没有章节</div>
          <div
            v-for="(ch, i) in chapters"
            :key="i"
            class="novel-chapter"
            :class="{ pending: !ch.written, active: editorChapter === ch.number }"
            :title="ch.written ? '点击读取/编辑这一章正文' : '这一章还没写'"
            data-test="novel-chapter"
            @click="openChapter(ch)"
          >
            <span class="novel-chapter-name">{{ ch.name }}</span>
            <span v-if="ch.words !== null" class="novel-chapter-words">{{ formatWords(ch.words) }} 字</span>
            <span v-else-if="ch.written" class="novel-chapter-words dim">字数未知</span>
            <span v-else class="novel-chapter-tag">待写</span>
          </div>
        </div>
        <!-- 后端只回章数、不回逐章明细时明确告知，不让用户以为章节列表坏了 -->
        <div v-if="chaptersArePlaceholder" class="novel-note" data-test="novel-chapter-note">
          接口只返回章数与总字数，逐章章名/字数未知，此处按章号列出
        </div>

        <!-- 章节编辑器：点章节列表项展开，读原文 → 编辑 → 覆盖保存（不新建章节） -->
        <div v-if="editorChapter > 0" class="novel-editor-box" data-test="novel-editor-box">
          <div class="novel-editor-head">
            <span class="novel-editor-title">{{ editorName }}</span>
            <span v-if="editorWords !== null" class="novel-editor-words">{{ formatWords(editorWords) }} 字</span>
            <span class="novel-editor-spacer"></span>
            <button class="novel-btn" data-test="novel-editor-close" @click="closeEditor()">关闭</button>
          </div>
          <div v-if="editorLoading" class="novel-hint">
            <AppIcon name="loader" class="icon-spin" :size="14" />
            <span>正在读取正文…</span>
          </div>
          <template v-else>
            <textarea
              v-model="editorText"
              class="novel-editor"
              data-test="novel-editor"
              spellcheck="false"
              placeholder="这一章还没有正文内容"
            ></textarea>
            <div class="novel-editor-foot">
              <button
                class="novel-btn primary"
                data-test="novel-save"
                :disabled="saving"
                @click="saveChapter()"
              >{{ saving ? '保存中…' : '保存' }}</button>
              <span v-if="saveHint" class="novel-save-hint" data-test="novel-save-hint">{{ saveHint }}</span>
              <span v-if="saveError" class="novel-save-error" data-test="novel-save-error">{{ saveError }}</span>
            </div>
          </template>
        </div>
      </div>

      <!-- 设定库（P3-C）：伏笔 / 角色 / 卷纲三张只读表，数据来自 get_novel_workbench，
           一次拉取、纯前端切 tab（不重复请求）；读取失败只在本分区提示，不影响章节与导出 -->
      <div class="novel-sec">
        <div class="novel-sec-head">
          <span class="novel-sec-title">设定库</span>
          <div class="novel-tabs">
            <button
              v-for="tab in settingTabs"
              :key="tab.key"
              class="novel-tab"
              :class="{ active: settingTab === tab.key }"
              data-test="novel-setting-tab"
              @click="settingTab = tab.key"
            >{{ tab.label }}</button>
          </div>
        </div>
        <!-- 聚合读取失败（含老桥接缺方法）：只提示可读文案，表格位置让给错误行 -->
        <div v-if="workbenchError" class="novel-sec-error" data-test="novel-workbench-error">
          {{ workbenchError }}
        </div>
        <template v-else>
          <NovelForeshadowTable v-if="settingTab === 'foreshadow'" :rows="foreshadowRows" />
          <NovelCharacterTable v-else-if="settingTab === 'character'" :rows="characterRows" />
          <NovelVolumeList v-else :rows="volumeRows" @select="openVolumeDoc" />
        </template>
      </div>

      <!-- 近 7 天字数：口径由后端给出（章节文件 mtime），合计只在展示层累加 -->
      <div class="novel-sec">
        <div class="novel-sec-head">
          <span class="novel-sec-title">近 7 天</span>
          <span class="novel-sec-meta">合计 {{ formatWords(recentDailyTotal) }} 字</span>
        </div>
        <NovelDailyTrend :rows="recentDailyRows" />
      </div>

      <!-- 一致性审查：只在点击后跑一次只读体检，不自动触发、不轮询 -->
      <div class="novel-sec">
        <div class="novel-sec-head"><span class="novel-sec-title">一致性审查</span></div>
        <NovelAuditPanel
          :result="auditResult"
          :loading="auditLoading"
          :error="auditError"
          :fixing="auditFixing"
          @run="runAudit()"
          @fix="fixFromAudit"
          @fix-all="fixAllFromAudit"
        />
      </div>

      <div class="novel-sec">
        <div class="novel-sec-head"><span class="novel-sec-title">写作状态</span></div>
        <div class="novel-cards" data-test="novel-status">
          <div class="novel-card">
            <span class="novel-card-label">活跃伏笔</span>
            <span class="novel-card-value" :class="{ warn: activeForeshadows > 0 }">
              {{ activeForeshadows }}
            </span>
          </div>
          <div class="novel-card">
            <span class="novel-card-label">最近提交</span>
            <span class="novel-card-value">{{ lastCommitText }}</span>
          </div>
          <div class="novel-card">
            <span class="novel-card-label">下一章细纲</span>
            <span class="novel-card-value" :class="outlineReady ? 'ok' : 'warn'">
              {{ outlineReady ? '已就绪' : '未就绪' }}
            </span>
          </div>
        </div>
      </div>

      <!-- 卷纲只读预览：内联区块排在内容末尾（不用 position:fixed 逃出面板），
           关闭即清空标题与正文，回到普通列表 -->
      <NovelDocViewer
        v-if="docVisible"
        :title="docTitle"
        :doc="docData"
        :loading="docLoading"
        :error="docError"
        @close="closeDoc()"
      />
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import { useAppStore } from '../stores/appStore';
import AppIcon from './AppIcon.vue';
import NovelForeshadowTable from './NovelForeshadowTable.vue';
import NovelCharacterTable from './NovelCharacterTable.vue';
import NovelVolumeList from './NovelVolumeList.vue';
import NovelDailyTrend from './NovelDailyTrend.vue';
import NovelAuditPanel from './NovelAuditPanel.vue';
import NovelDocViewer from './NovelDocViewer.vue';
import type {
  NovelAuditDto,
  NovelAuditFindingDto,
  NovelDocReadDto,
  NovelStatusDto,
  NovelWorkbenchCharacterDto,
  NovelWorkbenchDailyDto,
  NovelWorkbenchDto,
  NovelWorkbenchForeshadowDto,
  NovelWorkbenchVolumeDto,
} from '../types/pywebview.d';

// 激活态由 RightPanel 传入（页签可见、面板展开且停在本工作台）。
// 只在「激活 → 拉一次」时取数，不做轮询（设计 §7.3 刷新策略）
const props = defineProps<{ active: boolean }>();

const store = useAppStore();

type Phase = 'loading' | 'ready' | 'empty' | 'error';

const phase = ref<Phase>('loading');
const status = ref<NovelStatusDto | null>(null);
const errorText = ref('');
// 本次快照是否有逐章明细；false 表示章节列表按章号兜底生成
const chaptersArePlaceholder = ref(false);

// 桥接访问：缺桥时给出可读错误，不弹后端连接失败模态（页签只显示内联提示）
function bridgeApi() {
  try {
    return window.pywebview?.api ?? null;
  } catch {
    return null;
  }
}

function toInt(value: unknown): number {
  const n = Number(value);
  return Number.isFinite(n) && n > 0 ? Math.floor(n) : 0;
}

const titleText = computed(() => (status.value?.title || '').trim() || '未命名小说');
const totalWords = computed(() => toInt(status.value?.total_words));
const todayWords = computed(() => toInt(status.value?.today_words));
const targetWords = computed(() => toInt(status.value?.target_words));
const chaptersWritten = computed(() => toInt(status.value?.chapters_written));
const nextChapter = computed(() => toInt(status.value?.next_chapter));
const outlineReady = computed(() => status.value?.outline_ready === true);
const activeForeshadows = computed(() => toInt(status.value?.active_foreshadows));
const lastCommit = computed(() => toInt(status.value?.last_commit));

const progressPercent = computed(() => {
  if (targetWords.value <= 0) return 0;
  return Math.min(100, Math.round((totalWords.value / targetWords.value) * 100));
});

const lastCommitText = computed(() => (lastCommit.value > 0 ? `第 ${lastCommit.value} 章` : '暂无提交'));

// 空态文案：未进入项目目录时引导先选目录，其余引导开书
const canInit = computed(() => {
  const wd = String(store.workdir || '').trim();
  return !!wd && wd !== 'GLOBAL';
});
const emptyTitle = computed(() => (canInit.value ? '当前项目还不是小说项目' : '还没有进入项目目录'));
const emptyDesc = computed(() =>
  canInit.value
    ? '初始化后会在当前目录生成 .novel.json、设定与卷纲目录。点击下方按钮会把初始化指令填进输入框，由你在对话里确认后发送——不会直接写文件。'
    : '小说工作台基于项目目录工作。先在对话页引入一个项目文件夹，再回来初始化小说项目。',
);

type ChapterRow = { number: number; name: string; words: number | null; written: boolean };

function chapterNameFromIndex(index: number): string {
  return `第 ${String(index).padStart(3, '0')} 章`;
}

// 保存成功后覆盖该章字数：状态快照可能只有章数，覆盖表让「刚存完的字数」立刻生效
const chapterWordsOverride = ref<Record<number, number>>({});

function chapterWordsOf(number: number, fallback: number | null): number | null {
  const overrides = chapterWordsOverride.value;
  return Object.prototype.hasOwnProperty.call(overrides, number) ? overrides[number] : fallback;
}

// 章节列表：后端若回逐章明细（chapters）直接渲染章名+字数；只回章数时按章号
// 兜底列出已写章节，并把下一章作为「待写」占位（不伪造字数）
const chapters = computed<ChapterRow[]>(() => {
  const snap = status.value;
  if (!snap) return [];
  const raw = Array.isArray(snap.chapters) ? snap.chapters : [];
  if (raw.length > 0) {
    const rows: ChapterRow[] = raw.map((ch, i) => {
      const number = toInt(ch.number) || i + 1;
      const name = String(ch.name ?? ch.title ?? '').trim() || chapterNameFromIndex(number);
      const words = typeof ch.words === 'number' && Number.isFinite(ch.words) ? Math.max(0, Math.floor(ch.words)) : null;
      return { number, name, words: chapterWordsOf(number, words), written: true };
    });
    const pending = nextChapter.value;
    if (pending > 0 && !raw.some((ch, i) => (toInt(ch.number) || i + 1) === pending)) {
      rows.push({ number: pending, name: chapterNameFromIndex(pending), words: null, written: false });
    }
    return rows;
  }

  const written = chaptersWritten.value;
  const rows: ChapterRow[] = [];
  for (let i = 1; i <= written; i += 1) {
    rows.push({ number: i, name: chapterNameFromIndex(i), words: chapterWordsOf(i, null), written: true });
  }
  const pending = nextChapter.value;
  if (pending > 0 && pending > written) {
    rows.push({ number: pending, name: chapterNameFromIndex(pending), words: null, written: false });
  }
  return rows;
});

watch(chapters, (rows) => {
  chaptersArePlaceholder.value = rows.some(row => row.written && row.words === null);
}, { immediate: true });

function formatWords(value: number): string {
  if (value >= 10000) return `${(value / 10000).toFixed(2)} 万`;
  return String(value);
}

// ---- 多书切换（P3-B）：同一工作区内可能有多个小说项目 ----
// books 来自 list_novel_projects 的只读扫描；activeWorkdir 为空串表示跟随
// store.workdir（即当前会话工作区本身那本书）
const books = ref<{ path: string; title: string; chapters_written: number }[]>([]);
const activeWorkdir = ref('');

// 相对工作区的 posix 路径 → 绝对路径（"." 表示工作区自身，直接用工作区路径）
function joinBookPath(path: string): string {
  const base = String(store.workdir || '').trim().replace(/[\\/]+$/, '');
  const rel = String(path || '').trim().replace(/\\/g, '/').replace(/^\.\//, '');
  if (!rel || rel === '.') return base;
  return `${base}/${rel}`;
}

const bookSelectValue = computed(() => currentWorkdir());

// 拉一次项目列表：纯增强信息，失败静默（面板保持原状态，不退化成错误态）
async function loadBooks() {
  const workdir = String(store.workdir || '').trim();
  if (!workdir || workdir === 'GLOBAL') {
    books.value = [];
    return;
  }
  const api = bridgeApi();
  // 老版本桥接没有该方法时按「只有一本书」处理，不做任何提示
  if (!api || typeof api.list_novel_projects !== 'function') return;
  try {
    const res = await api.list_novel_projects(workdir);
    if (!res || typeof res !== 'object' || !res.ok) return;
    const list = Array.isArray(res.projects) ? res.projects : [];
    if (list.length === 0) return;
    books.value = list
      .filter(item => item && typeof item.path === 'string' && item.path.trim() !== '')
      .map(item => ({
        path: String(item.path).trim(),
        title: String(item.title || '').trim() || '未命名小说',
        chapters_written: toInt(item.chapters_written),
      }));
  } catch {
    // 列表失败不影响主面板：保持原有的单书展示
  }
}

// 切书：先收掉编辑器与本地字数缓存（属于上一本书），再按新目录重拉快照
function onBookChange(event: Event) {
  const target = event.target as HTMLSelectElement | null;
  const next = String(target?.value || '').trim();
  if (!next || next === currentWorkdir()) return;
  closeEditor();
  chapterWordsOverride.value = {};
  exportHint.value = '';
  exportError.value = '';
  // 上一本书的设定库与审查结论一并作废：切书后由 load() 按新目录重拉
  resetWorkbench();
  activeWorkdir.value = next;
  void load();
}

// ---- 导出全书（P3-B）：产物由后端写到 {项目}/导出/{书名}.{fmt} ----
const exporting = ref(false);
const exportHint = ref('');
const exportError = ref('');

async function exportNovel(fmt: 'md' | 'txt') {
  if (exporting.value) return;
  const workdir = currentWorkdir();
  if (!workdir) return;
  const api = bridgeApi();
  if (!api || typeof api.export_novel !== 'function') {
    exportHint.value = '';
    exportError.value = '后端连接不可用：pywebview API 尚未就绪';
    return;
  }
  exporting.value = true;
  exportHint.value = '';
  exportError.value = '';
  try {
    const res = await api.export_novel(workdir, fmt);
    if (!res || typeof res !== 'object') {
      exportError.value = '导出失败：后端没有返回结果';
      return;
    }
    if (res.error || res.ok === false) {
      exportError.value = String(res.error || '导出失败，请稍后重试');
      return;
    }
    const path = String(res.path || '').trim();
    exportHint.value = `已导出：${path || '导出目录'}（${toInt(res.chapters)} 章）`;
  } catch (err) {
    exportError.value = err instanceof Error ? err.message : String(err);
  } finally {
    exporting.value = false;
  }
}

// ---- 章节编辑器（P3-A）：点章节 → 读正文 → 编辑 → 覆盖保存，不新建章节 ----
const editorChapter = ref(0);
const editorName = ref('');
const editorText = ref('');
const editorWords = ref<number | null>(null);
const editorLoading = ref(false);
const saving = ref(false);
const saveHint = ref('');
const saveError = ref('');

// 当前生效目录：优先显式选中的那本书（activeWorkdir），为空才回落会话工作区，
// GLOBAL（自由对话）没有项目目录，统一返回空串
function currentWorkdir(): string {
  const active = String(activeWorkdir.value || '').trim();
  const wd = active || String(store.workdir || '').trim();
  return wd === 'GLOBAL' ? '' : wd;
}

function toCount(value: unknown): number | null {
  const n = Number(value);
  return Number.isFinite(n) && n >= 0 ? Math.floor(n) : null;
}

function closeEditor() {
  editorChapter.value = 0;
  editorName.value = '';
  editorText.value = '';
  editorWords.value = null;
  editorLoading.value = false;
  saving.value = false;
  saveHint.value = '';
  saveError.value = '';
}

// 点章节：已写章节才可进入编辑；编辑中切章直接丢弃未保存内容（不做多章草稿缓存）
async function openChapter(ch: ChapterRow) {
  if (!ch.written) return;
  const workdir = currentWorkdir();
  if (!workdir) return;
  const api = bridgeApi();
  closeEditor();
  editorChapter.value = ch.number;
  editorName.value = ch.name;
  editorWords.value = ch.words;
  if (!api) {
    saveError.value = '后端连接不可用：pywebview API 尚未就绪';
    return;
  }
  editorLoading.value = true;
  try {
    const res = await api.get_novel_chapter(workdir, ch.number);
    // 期间又点了别的章节：只认最后一次点击，丢弃这次结果
    if (editorChapter.value !== ch.number) return;
    if (!res || typeof res !== 'object') {
      saveError.value = '后端返回了空章节内容，无法编辑';
      return;
    }
    if (res.error) {
      saveError.value = String(res.error);
      return;
    }
    editorText.value = String(res.content ?? '');
    const words = toCount(res.visible_chars);
    if (words !== null) editorWords.value = words;
  } catch (err) {
    if (editorChapter.value === ch.number) {
      saveError.value = err instanceof Error ? err.message : String(err);
    }
  } finally {
    if (editorChapter.value === ch.number) editorLoading.value = false;
  }
}

// 保存：成功后用返回字数更新该章并重拉整体状态；失败只显示错误，不动本地正文
async function saveChapter() {
  const workdir = currentWorkdir();
  const number = editorChapter.value;
  if (!workdir || number <= 0 || saving.value) return;
  const api = bridgeApi();
  if (!api) {
    saveError.value = '后端连接不可用：pywebview API 尚未就绪';
    return;
  }
  saving.value = true;
  saveHint.value = '';
  saveError.value = '';
  try {
    const res = await api.save_novel_chapter(workdir, number, editorText.value);
    if (!res || typeof res !== 'object') {
      saveError.value = '保存失败：后端没有返回结果';
      return;
    }
    if (res.error || res.ok === false) {
      saveError.value = String(res.error || '保存失败，请稍后重试');
      return;
    }
    const words = toCount(res.visible_chars) ?? 0;
    editorWords.value = words;
    chapterWordsOverride.value = { ...chapterWordsOverride.value, [number]: words };
    saveHint.value = '已保存';
    // 总字数/今日字数由后端统一口径统计，保存后重拉一次（复用 load，不前端自算）
    void load();
  } catch (err) {
    saveError.value = err instanceof Error ? err.message : String(err);
  } finally {
    saving.value = false;
  }
}

// ---- 设定库 / 近 7 天 / 一致性审查 / 文档预览（P3-C，全部只读） ----
// 这三块是「增强数据」：各自保存自己的结果与错误文本，任何一个请求失败都只
// 在本分区给可读提示，不改主面板 phase、不清空章节与字数快照
const workbench = ref<NovelWorkbenchDto | null>(null);
const workbenchError = ref('');
const settingTab = ref<'foreshadow' | 'character' | 'volume'>('foreshadow');
const auditResult = ref<NovelAuditDto | null>(null);
const auditLoading = ref(false);
const auditError = ref('');
// 修复入口只是把指令填进输入框（同步完成）：短暂上锁，避免连点重复预填
const auditFixing = ref(false);
const docTitle = ref('');
const docData = ref<NovelDocReadDto | null>(null);
const docLoading = ref(false);
const docError = ref('');

// 请求序号：切书 / 换目录时递增，迟到的在途结果直接丢弃（避免上一本书的
// 伏笔或审查结论落到新书下）
let workbenchSeq = 0;
let auditSeq = 0;

// 契约里四个数组字段都是可选的，缺失或非数组一律按空数组兜底
function rowsOf<T>(value: unknown): T[] {
  return Array.isArray(value) ? (value as T[]) : [];
}

const foreshadowRows = computed<NovelWorkbenchForeshadowDto[]>(
  () => rowsOf<NovelWorkbenchForeshadowDto>(workbench.value?.foreshadows),
);
const characterRows = computed<NovelWorkbenchCharacterDto[]>(
  () => rowsOf<NovelWorkbenchCharacterDto>(workbench.value?.characters),
);
const volumeRows = computed<NovelWorkbenchVolumeDto[]>(
  () => rowsOf<NovelWorkbenchVolumeDto>(workbench.value?.volumes),
);
const recentDailyRows = computed<NovelWorkbenchDailyDto[]>(
  () => rowsOf<NovelWorkbenchDailyDto>(workbench.value?.recent_daily),
);
const recentDailyTotal = computed(() => recentDailyRows.value.reduce((acc, row) => acc + toInt(row.words), 0));

// 三个 tab 的文案直接带数量：数量来自同一次快照，切 tab 不再请求后端
const settingTabs = computed(() => [
  { key: 'foreshadow' as const, label: `伏笔 ${foreshadowRows.value.length}` },
  { key: 'character' as const, label: `角色 ${characterRows.value.length}` },
  { key: 'volume' as const, label: `卷纲 ${volumeRows.value.length}` },
]);

const docVisible = computed(() => docTitle.value !== '');

// 拉一次工作台聚合（伏笔 / 角色 / 卷纲 / 近 7 天）：失败只写 workbenchError
async function loadWorkbench() {
  const workdir = currentWorkdir();
  const seq = workbenchSeq + 1;
  workbenchSeq = seq;
  if (!workdir) {
    workbench.value = null;
    workbenchError.value = '';
    return;
  }
  const api = bridgeApi();
  // 老版本桥接没有该方法：只在本分区提示不支持，其余功能照常
  if (!api || typeof api.get_novel_workbench !== 'function') {
    workbench.value = null;
    workbenchError.value = '当前后端版本不支持该功能，请更新后再查看设定库';
    return;
  }
  try {
    const res = await api.get_novel_workbench(workdir);
    // 期间切了书 / 换了目录：这次结果已作废，不覆盖新书数据
    if (seq !== workbenchSeq) return;
    if (!res || typeof res !== 'object') {
      workbench.value = null;
      workbenchError.value = '设定库数据读取失败：后端没有返回结果';
      return;
    }
    if (res.error || res.is_novel === false) {
      workbench.value = null;
      workbenchError.value = `设定库数据读取失败：${String(res.error || '当前项目还不是小说项目')}`;
      return;
    }
    workbench.value = res;
    workbenchError.value = '';
  } catch (err) {
    if (seq !== workbenchSeq) return;
    workbench.value = null;
    workbenchError.value = `设定库数据读取失败：${err instanceof Error ? err.message : String(err)}`;
  }
}

// 跑一次只读体检：审最新已写章节（不传 chapter，由后端取最新章）
async function runAudit() {
  if (auditLoading.value) return;
  const workdir = currentWorkdir();
  if (!workdir) return;
  const api = bridgeApi();
  if (!api || typeof api.run_novel_audit !== 'function') {
    auditResult.value = null;
    auditError.value = '当前后端版本不支持该功能，请更新后再运行一致性审查';
    return;
  }
  auditLoading.value = true;
  auditError.value = '';
  const seq = auditSeq + 1;
  auditSeq = seq;
  try {
    const res = await api.run_novel_audit(workdir, 'chapter');
    if (seq !== auditSeq) return;
    if (!res || typeof res !== 'object') {
      auditResult.value = null;
      auditError.value = '审查失败：后端没有返回结果';
      return;
    }
    auditResult.value = res;
    // ok=false 时结果里也带 error：统一收进 auditError，面板只出一处红字
    auditError.value = res.error || (res.ok === false ? '审查未完成' : '');
  } catch (err) {
    if (seq !== auditSeq) return;
    auditResult.value = null;
    auditError.value = err instanceof Error ? err.message : String(err);
  } finally {
    if (seq === auditSeq) auditLoading.value = false;
  }
}

// ---- 审查结论 → 修复入口：只把问题拼成对话指令预填进输入框，不在面板里直接改文件 ----

// 五种检查项（后端 AUDIT_TYPES）各自的修复方向；未知 type 落到通用兜底文案
const AUDIT_FIX_HINTS: Record<string, string> = {
  foreshadow_overdue:
    '核对伏笔台账的埋设章、计划回收章与最新已提交章，补齐回收计划或按正文实际回收并更新状态；不要为消除告警而伪造回收。',
  ai_patterns: '按《设定/文风.md》与禁用词表替换或改写，保留原意与叙述节奏。',
  chapter_wordcount: '按细纲字数目标补写该章（先看细纲，只补该章）。',
  outline_gap: '补写缺失的细纲。',
  tracking_state: '把正文与追踪状态对齐（缺哪章补哪章的事务）。',
};

// 分组顺序：固定按后端检查项顺序排，组内保持 findings 原顺序
const AUDIT_TYPE_ORDER = ['tracking_state', 'foreshadow_overdue', 'chapter_wordcount', 'outline_gap', 'ai_patterns'];

function auditTypeLabel(type: unknown): string {
  const key = String(type || '').trim();
  return key || '未标注类型';
}

function auditFixHint(type: string): string {
  return AUDIT_FIX_HINTS[String(type || '').trim()] || '按审查结论逐条核对原文后修正。';
}

// 预填与提示都是同步的：短暂上锁只为挡住连点造成的重复预填
function tryLockAuditFix(): boolean {
  if (auditFixing.value) return false;
  auditFixing.value = true;
  window.setTimeout(() => {
    auditFixing.value = false;
  }, 800);
  return true;
}

// 单条修复：原样带上级别/类型/文件/问题全文，只动这一处
function fixFromAudit(finding: NovelAuditFindingDto) {
  const workdir = currentWorkdir();
  if (!workdir || !tryLockAuditFix()) return;
  const level = String(finding?.level || '').trim() || '未标注级别';
  const type = auditTypeLabel(finding?.type);
  const file = String(finding?.file || '').trim() || '（未标注文件）';
  const message = String(finding?.message || '').trim() || '（后端没有给出问题描述）';
  const text = [
    `在小说项目（${workdir}）里，修复一致性审查发现的这条问题：`,
    '',
    `- 级别：${level}`,
    `- 类型：${type}`,
    `- 文件：${file}`,
    `- 问题：${message}`,
    '',
    `本条修复方向：${auditFixHint(type)}`,
    '请先读原文核对证据，只做最小改动；改完请重跑一致性审查（scope=chapter）确认该类问题消失，' +
      '不要改动与本条无关的内容。',
  ].join('\n');
  store.prefillComposer(text);
  store.notifyChat('已把这条问题的修复指令填进输入框，确认后回车发送');
}

// 全部修复：按 type 分组汇总当前审查结论，逐组给修复方向，一次交给星瑶
function fixAllFromAudit() {
  const workdir = currentWorkdir();
  const findings = Array.isArray(auditResult.value?.findings) ? auditResult.value.findings : [];
  if (!workdir || findings.length === 0 || !tryLockAuditFix()) return;
  const groups = new Map<string, NovelAuditFindingDto[]>();
  for (const finding of findings) {
    const key = auditTypeLabel(finding?.type);
    const list = groups.get(key);
    if (list) list.push(finding);
    else groups.set(key, [finding]);
  }
  const orderedKeys = [
    ...AUDIT_TYPE_ORDER.filter((key) => groups.has(key)),
    ...[...groups.keys()].filter((key) => !AUDIT_TYPE_ORDER.includes(key)),
  ];
  const lines: string[] = [
    `在小说项目（${workdir}）里，修复最近一次一致性审查发现的问题（共 ${findings.length} 条，按类型分组）：`,
  ];
  for (const key of orderedKeys) {
    const items = groups.get(key) ?? [];
    lines.push('', `## ${key}（${items.length} 条）`);
    for (const item of items) {
      const level = String(item?.level || '').trim() || '未标注级别';
      const file = String(item?.file || '').trim() || '（未标注文件）';
      const message = String(item?.message || '').trim() || '（后端没有给出问题描述）';
      lines.push(`- [${level}] ${file}：${message}`);
    }
    lines.push(`修复方向：${auditFixHint(key)}`);
  }
  lines.push(
    '',
    '逐组处理，每处理完一组报一次进度；遇到需要作者拍板的口径冲突先列出来不要直接改。',
    '每组都先读原文核对证据，只做最小改动，不要改动与这些问题无关的内容。',
    '全部处理完请重跑一致性审查（scope=chapter）确认这些条目消失。',
  );
  store.prefillComposer(lines.join('\n'));
  store.notifyChat('已把全部问题的修复指令填进输入框，确认后回车发送');
}

// 点卷纲：拼相对项目根的 posix 路径读文档；失败也打开浮层，在浮层里显示错误
async function openVolumeDoc(volume: NovelWorkbenchVolumeDto) {
  const name = String(volume?.name || '').trim();
  if (!name) return;
  const workdir = currentWorkdir();
  docTitle.value = name;
  docData.value = null;
  docError.value = '';
  docLoading.value = false;
  if (!workdir) {
    docError.value = '当前没有项目目录，无法读取文档';
    return;
  }
  const api = bridgeApi();
  if (!api || typeof api.read_novel_doc !== 'function') {
    docError.value = '当前后端版本不支持该功能，请更新后再预览文档';
    return;
  }
  docLoading.value = true;
  try {
    // name 已是文件名 stem（如 卷纲_第001卷），后端在 core 内做白名单与越界校验
    const res = await api.read_novel_doc(workdir, `大纲/${name}.md`);
    // 期间关了浮层或点了别的卷：丢弃这次结果
    if (docTitle.value !== name) return;
    if (!res || typeof res !== 'object') {
      docError.value = '读取失败：后端没有返回结果';
      return;
    }
    if (res.ok === false || res.error) {
      docError.value = String(res.error || '读取失败');
      return;
    }
    docData.value = res;
  } catch (err) {
    if (docTitle.value !== name) return;
    docError.value = err instanceof Error ? err.message : String(err);
  } finally {
    if (docTitle.value === name) docLoading.value = false;
  }
}

function closeDoc() {
  docTitle.value = '';
  docData.value = null;
  docLoading.value = false;
  docError.value = '';
}

// 切书 / 换目录 / 回合结束：设定库与审查结论都属于「上一本书」，一律作废；
// 序号随之 +1，迟到的在途结果会被丢弃，不会回填到新书下
function resetWorkbench() {
  workbenchSeq += 1;
  auditSeq += 1;
  workbench.value = null;
  workbenchError.value = '';
  auditResult.value = null;
  auditLoading.value = false;
  auditError.value = '';
  closeDoc();
}

// 拉一次状态：只读接口，失败按错误态展示，不抛异常
async function load() {
  // 目录取「当前生效目录」：多书切换后 activeWorkdir 覆盖会话工作区
  const workdir = currentWorkdir();
  if (!workdir) {
    status.value = null;
    errorText.value = '';
    phase.value = 'empty';
    resetWorkbench();
    return;
  }
  const api = bridgeApi();
  if (!api) {
    phase.value = 'error';
    errorText.value = '后端连接不可用：pywebview API 尚未就绪';
    resetWorkbench();
    return;
  }
  // 已有快照时保留旧内容，只做一次静默重拉（避免切工作目录时闪白）
  if (!status.value) phase.value = 'loading';
  try {
    const res = await api.get_novel_status(workdir);
    if (!res || typeof res !== 'object') {
      status.value = null;
      phase.value = 'error';
      errorText.value = '后端返回了空状态，无法展示小说项目信息';
      resetWorkbench();
      return;
    }
    if (res.error) {
      status.value = null;
      phase.value = 'error';
      errorText.value = String(res.error);
      resetWorkbench();
      return;
    }
    if (!res.is_novel) {
      status.value = null;
      errorText.value = '';
      phase.value = 'empty';
      resetWorkbench();
      return;
    }
    status.value = res;
    errorText.value = '';
    phase.value = 'ready';
    // 设定库 / 近 7 天是增强数据：单独一次只读请求，失败只落在 workbenchError，
    // 不回滚上面已就绪的章节与字数快照
    void loadWorkbench();
  } catch (err) {
    status.value = null;
    phase.value = 'error';
    errorText.value = err instanceof Error ? err.message : String(err);
    resetWorkbench();
  }
}

// 初始化引导：只预填输入框，不直接写文件（设计 §7.3-4）
function initNovel() {
  const workdir = String(store.workdir || '').trim();
  if (!workdir || workdir === 'GLOBAL') return;
  store.prefillComposer(
    `帮我在当前项目（${workdir}）初始化小说项目：先读 .novel.json 与设定/卷纲，` +
    '然后按 story-long-write 流程开书。书名、题材、平台、目标字数我接下来补充。',
  );
  store.notifyChat('已把初始化小说项目的指令填进输入框，确认后回车发送');
}

// 页签激活时拉一次；工作目录切换后再按需重拉（同一页签不会重复轮询）
watch(() => props.active, (active) => {
  if (!active) return;
  void loadBooks();
  void load();
}, { immediate: true });
// 换工作区：上一本书的选择与本地缓存全部作废，多书列表按新工作区重扫
watch(() => store.workdir, () => {
  closeEditor();
  chapterWordsOverride.value = {};
  activeWorkdir.value = '';
  books.value = [];
  exportHint.value = '';
  exportError.value = '';
  resetWorkbench();
  if (props.active) {
    void loadBooks();
    void load();
  }
});
// 对话回合结束（running true→false）时按需重拉一次：对话里初始化出的项目、
// 写出的新章节、新建的书立刻反映到面板，不必切页签才看到变化。
// 只在页签可见且真的经历了「跑→停」时刷新，不做轮询；回合开始不打扰。
watch(() => store.running, (running, prev) => {
  if (!props.active || running || prev !== true) return;
  // 刚写完一章：设定库（伏笔 / 字数）随之重拉，但审查结论作废、不自动重跑
  resetWorkbench();
  void loadBooks();
  void load();
});
</script>

<style scoped>
/* 深色主题跟随全局 CSS 变量（--text / --border / --accent…），不引入新 UI 库 */
.novel-wrap {
  display: flex;
  flex-direction: column;
  gap: 12px;
  height: 100%;
  min-height: 0;
  padding: 12px;
  overflow-y: auto;
  color: var(--text);
  font-size: 12.5px;
}

/* 工具区：多书下拉 + 导出按钮，紧贴标题上方 */
.novel-toolbar { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.novel-toolbar-spacer { flex: 1; }
.novel-select {
  max-width: 100%;
  padding: 3px 6px;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--code-bg);
  color: var(--text);
  font-size: 12px;
  cursor: pointer;
}
.novel-select:hover { border-color: var(--accent); }
.novel-select:focus { outline: none; border-color: var(--accent); }
.novel-export-hint { font-size: 11.5px; color: var(--green); word-break: break-word; }
.novel-export-error { font-size: 11.5px; color: var(--red); word-break: break-word; }

.novel-head { display: flex; flex-direction: column; gap: 6px; }
.novel-title { font-size: 14px; font-weight: 600; color: var(--text-bright); }
.novel-metrics { display: flex; flex-wrap: wrap; gap: 12px; color: var(--text-dim); }
.novel-metric { font-size: 12px; }
.novel-num { color: var(--text-bright); font-family: var(--mono); }
.novel-metric.ok .novel-num { color: var(--green); }
.novel-pct { color: var(--text-faint); }

.novel-bar {
  height: 4px;
  border-radius: 2px;
  background: var(--border);
  overflow: hidden;
}
.novel-bar-fill {
  display: block;
  height: 100%;
  background: var(--accent);
  transition: width 0.2s ease;
}

.novel-sec { display: flex; flex-direction: column; gap: 6px; }
.novel-sec-head { display: flex; align-items: baseline; justify-content: space-between; gap: 8px; }
.novel-sec-title { font-size: 12px; font-weight: 600; color: var(--text-dim); }
.novel-sec-meta { font-size: 11.5px; color: var(--text-faint); }
/* 设定库分区内错误：只占本分区一行，不挡下面的近 7 天与审查 */
.novel-sec-error { font-size: 11.5px; line-height: 1.5; color: var(--red); word-break: break-word; }

/* 设定库 tab：数量直接写进文案，当前项用 accent 描边高亮 */
.novel-tabs { display: flex; align-items: center; gap: 4px; flex-wrap: wrap; justify-content: flex-end; }
.novel-tab {
  padding: 2px 8px;
  border: 1px solid var(--border);
  border-radius: 999px;
  background: transparent;
  color: var(--text-dim);
  font-size: 11.5px;
  line-height: 1.5;
  cursor: pointer;
}
.novel-tab:hover { border-color: var(--accent); color: var(--text-bright); }
.novel-tab.active { border-color: var(--accent); background: var(--accent-soft); color: var(--accent); }

.novel-chapters {
  display: flex;
  flex-direction: column;
  gap: 2px;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 4px;
  max-height: 320px;
  overflow-y: auto;
}
.novel-chapter {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 3px 6px;
  border-radius: 5px;
  font-size: 12px;
  cursor: pointer;
}
.novel-chapter:hover { background: var(--accent-soft); }
.novel-chapter.active { background: var(--accent-soft); }
.novel-chapter.pending { opacity: 0.55; cursor: default; }
.novel-chapter.pending:hover { background: transparent; }
.novel-chapter-name { color: var(--text); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.novel-chapter-words { color: var(--text-dim); font-family: var(--mono); font-size: 11.5px; }
.novel-chapter-words.dim { color: var(--text-faint); }
.novel-chapter-tag { color: var(--yellow); font-size: 11.5px; }

/* 章节编辑器：只出现在点击章节后，不占用默认状态的高度 */
.novel-editor-box {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 8px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--code-bg);
}
.novel-editor-head { display: flex; align-items: center; gap: 8px; }
.novel-editor-title { font-size: 12.5px; font-weight: 600; color: var(--text-bright); }
.novel-editor-words { font-size: 11.5px; color: var(--text-dim); font-family: var(--mono); }
.novel-editor-spacer { flex: 1; }
.novel-editor {
  width: 100%;
  min-height: 200px;
  resize: vertical;
  padding: 8px;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--bg);
  color: var(--text);
  font-family: var(--mono);
  font-size: 12px;
  line-height: 1.7;
}
.novel-editor:focus { outline: none; border-color: var(--accent); }
.novel-editor-foot { display: flex; align-items: center; gap: 8px; }
.novel-editor-foot .novel-btn { align-self: auto; }
.novel-save-hint { font-size: 11.5px; color: var(--green); }
.novel-save-error { font-size: 11.5px; color: var(--red); word-break: break-word; }

.novel-cards { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px; }
.novel-card {
  display: flex;
  flex-direction: column;
  gap: 3px;
  padding: 8px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--code-bg);
}
.novel-card-label { font-size: 11.5px; color: var(--text-dim); }
.novel-card-value { font-size: 13px; font-weight: 600; color: var(--text-bright); }
.novel-card-value.ok { color: var(--green); }
.novel-card-value.warn { color: var(--yellow); }

.novel-hint {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 8px 2px;
  color: var(--text-faint);
  font-size: 12px;
}
.novel-note { color: var(--text-faint); font-size: 11.5px; }

.novel-empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 8px;
  padding: 26px 12px;
  text-align: center;
}
.novel-empty-icon { color: var(--text-faint); }
.novel-empty-title { font-size: 13px; font-weight: 600; color: var(--text-bright); }
.novel-empty-desc { margin: 0; font-size: 12px; line-height: 1.6; color: var(--text-dim); }

.novel-block {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 8px;
  padding: 12px;
  border: 1px solid var(--border);
  border-radius: 8px;
}
.novel-block-title {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12.5px;
  font-weight: 600;
  color: var(--red);
}
.novel-error-text {
  margin: 0;
  font-size: 12px;
  line-height: 1.6;
  color: var(--text-dim);
  word-break: break-word;
}

.novel-btn {
  align-self: flex-start;
  padding: 4px 12px;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: transparent;
  color: var(--text);
  font-size: 12px;
  cursor: pointer;
}
.novel-btn:hover { border-color: var(--accent); color: var(--text-bright); }
/* 工具区按钮：比正文按钮更紧凑，不抢主操作（保存/初始化）的视觉权重 */
.novel-btn.small { padding: 3px 8px; font-size: 11.5px; align-self: center; }
.novel-btn.primary { border-color: var(--accent); color: var(--accent); }
.novel-btn.primary:hover { background: var(--accent-soft); }
.novel-btn:disabled { opacity: 0.5; cursor: not-allowed; }
</style>

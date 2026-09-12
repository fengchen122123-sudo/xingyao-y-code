<template>
  <div class="cfp">
    <!-- 头部：展开区（左）与撤销操作区（右）是独立交互元素，不嵌套（规格 16.2） -->
    <div class="cfp-head-row">
      <button class="cfp-head" type="button" :aria-expanded="open" @click="toggleOpen">
        <AppIcon class="cfp-ico" name="file" :size="14" />
        <span class="cfp-title">{{ headTitle }}</span>
        <span v-if="headStatsVisible" class="cfp-stats">
          <span class="cfp-add">+{{ headAdded }}</span>
          <span class="cfp-del">-{{ headRemoved }}</span>
        </span>
        <AppIcon
          class="cfp-caret"
          :class="{ 'is-open': open }"
          name="chevron-right"
          :size="12"
          :stroke-width="2"
        />
      </button>
      <!-- 撤销按钮：只调用按回合预检；状态机驱动展示（规格 16.3） -->
      <button
        v-if="undoButton"
        class="cfp-undo-btn"
        :class="undoButton.kind"
        type="button"
        :disabled="undoButton.disabled"
        :aria-label="undoButton.aria"
        :title="undoButton.title"
        @click="onUndoClick"
      >
        <AppIcon v-if="undoButton.icon" :name="undoButton.icon" :size="13" />
        {{ undoButton.label }}
      </button>
    </div>

    <div v-show="open" class="cfp-body">
      <!-- 冲突视图：结构化原因 + 预期/实际，不提供强制覆盖（规格 16.4） -->
      <template v-if="conflicts.length">
        <div class="cfp-conflict-note">本次没有修改任何文件。以下文件与回合结束时的状态不一致：</div>
        <div v-for="c in conflicts" :key="c.file_id" class="cfp-conflict">
          <div class="cfp-conflict-path">{{ c.display_path }}</div>
          <div v-for="(r, ri) in c.reasons" :key="ri" class="cfp-conflict-reason">· {{ r }}</div>
          <div class="cfp-conflict-detail">预期：{{ nodeBrief(c.expected) }}</div>
          <div class="cfp-conflict-detail">当前：{{ nodeBrief(c.actual) }}</div>
        </div>
        <div class="cfp-conflict-note">在磁盘上恢复这些文件后，可点击“重新检查”再次预检。</div>
        <button class="cfp-recheck" type="button" @click="onUndoClick">重新检查</button>
      </template>

      <!-- 恢复选择视图（M2）：interrupted 状态点击按钮后先展示两个恢复方向 -->
      <template v-else-if="recoveryOptions">
        <div class="cfp-conflict-note">撤销事务中断：文件可能处于部分恢复状态，请选择恢复方向。</div>
        <div v-if="recoveryOptions.processed?.length" class="cfp-conflict-detail">
          已恢复：{{ recoveryOptions.processed.join('、') }}
        </div>
        <div v-if="recoveryOptions.remaining?.length" class="cfp-conflict-detail">
          未处理：{{ recoveryOptions.remaining.join('、') }}
        </div>
        <div class="cfp-recovery-actions">
          <button class="cfp-recheck" type="button" :disabled="busy" @click="startRecovery('continue_revert')">继续撤销剩余文件</button>
          <button class="cfp-recheck" type="button" :disabled="busy" @click="startRecovery('cancel_revert')">取消撤销（恢复到撤销前）</button>
        </div>
      </template>

      <!-- 恢复确认视图：仅在事务未知状态（recovery_required）时展示只读详情 -->
      <template v-else-if="recoveryPreview">
        <div class="cfp-conflict-note">撤销事务中断：文件可能处于部分恢复状态。</div>
        <div v-if="recoveryPreview.processed_files.length" class="cfp-conflict-detail">
          已恢复：{{ recoveryPreview.processed_files.join('、') }}
        </div>
        <div v-if="recoveryPreview.remaining_files.length" class="cfp-conflict-detail">
          未处理：{{ recoveryPreview.remaining_files.join('、') }}
        </div>
        <div class="cfp-conflict-warn">
          文件处于事务未知状态，为安全起见已拒绝自动写入，请人工核对后在外部处理，
          或使用 CLI /undo status 查看更多细节。
        </div>
      </template>

      <!-- 权威文件列表：检查点 ready 之后，展示持久化 before→after 净摘要 -->
      <template v-else-if="authFiles">
        <div v-for="f in authFiles" :key="f.file_id" class="cfp-item">
          <div
            class="cfp-row"
            role="button"
            tabindex="0"
            :aria-expanded="expandedFileId === f.file_id"
            :title="expandedFileId === f.file_id ? '收起改动详情' : '查看改动详情'"
            @click="toggleAuthExpand(f)"
            @keydown.enter.prevent="toggleAuthExpand(f)"
          >
            <AppIcon class="cfp-row-ico" name="file" :size="13" />
            <span class="cfp-row-name">{{ baseName(f.display_path) }}</span>
            <span class="cfp-row-path">{{ f.display_path }}</span>
            <span class="cfp-row-stats">
              <span class="cfp-add">+{{ f.net_added }}</span>
              <span class="cfp-del">-{{ f.net_removed }}</span>
            </span>
            <AppIcon
              class="cfp-row-caret"
              :class="{ 'is-open': expandedFileId === f.file_id }"
              name="chevron-right"
              :size="11"
              :stroke-width="2"
            />
          </div>
          <!-- 展开：按 file_id 惰性请求权威 diff -->
          <div v-if="expandedFileId === f.file_id" class="cfp-diff">
            <template v-if="authDiffLoading">加载中…</template>
            <template v-else-if="authDiff">
              <div v-if="authDiff.diff_kind === 'binary'" class="cfp-diff-empty">
                二进制文件：{{ describeBinary(authDiff) }}
              </div>
              <template v-else-if="authDiff.diff_text">
                <template v-for="(block, bi) in (authDiffBlocks || [])" :key="bi">
                  <div v-if="bi > 0 || block.topHidden > 0" class="cfp-diff-gap">⋯</div>
                  <div
                    v-for="(l, li) in block.lines"
                    :key="li"
                    class="cfp-diff-line"
                    :class="`d-${l.kind}`"
                  >
                    <span class="cfp-no">{{ l.oldNo }}</span>
                    <span class="cfp-no">{{ l.newNo }}</span>
                    <span class="cfp-sign">{{ l.kind === 'add' ? '+' : l.kind === 'del' ? '-' : ' ' }}</span>
                    <span class="cfp-code">{{ l.content }}</span>
                  </div>
                  <div v-if="block.bottomHidden > 0" class="cfp-diff-gap">⋯</div>
                </template>
              </template>
              <div v-else class="cfp-diff-empty">无可用改动详情</div>
            </template>
          </div>
        </div>
      </template>

      <!-- 旧数据 / 运行中回合：实时 diff 聚合展示（既有行为） -->
      <template v-else>
        <div v-for="c in changes" :key="c.path" class="cfp-item">
          <div
            class="cfp-row"
            role="button"
            tabindex="0"
            :aria-expanded="expandedPath === c.path"
            :title="expandedPath === c.path ? '收起改动详情' : '查看改动详情'"
            @click="toggleExpand(c.path)"
            @keydown.enter.prevent="toggleExpand(c.path)"
          >
            <AppIcon class="cfp-row-ico" name="file" :size="13" />
            <span class="cfp-row-name">{{ c.name }}</span>
            <span class="cfp-row-path">{{ c.path }}</span>
            <span class="cfp-row-stats">
              <span class="cfp-add">+{{ c.added }}</span>
              <span class="cfp-del">-{{ c.removed }}</span>
            </span>
            <button
              class="cfp-open-btn"
              type="button"
              :title="`在默认程序中打开 ${c.path}`"
              @click.stop="openFile(c.path)"
            ><AppIcon name="external-link" :size="12" /></button>
            <AppIcon
              class="cfp-row-caret"
              :class="{ 'is-open': expandedPath === c.path }"
              name="chevron-right"
              :size="11"
              :stroke-width="2"
            />
          </div>

          <div v-if="expandedPath === c.path" class="cfp-diff">
            <template v-for="(block, bi) in (diffBlocksByPath.get(c.path) || [])" :key="bi">
              <div v-if="bi > 0 || block.topHidden > 0" class="cfp-diff-gap">⋯</div>
              <div
                v-for="(l, li) in block.lines"
                :key="li"
                class="cfp-diff-line"
                :class="`d-${l.kind}`"
              >
                <span class="cfp-no">{{ l.oldNo }}</span>
                <span class="cfp-no">{{ l.newNo }}</span>
                <span class="cfp-sign">{{ l.kind === 'add' ? '+' : l.kind === 'del' ? '-' : ' ' }}</span>
                <span class="cfp-code">{{ l.content }}</span>
              </div>
              <div v-if="block.bottomHidden > 0" class="cfp-diff-gap">⋯</div>
            </template>
            <div v-if="!c.diffText" class="cfp-diff-empty">无可用改动详情</div>
          </div>
        </div>
      </template>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue';
import AppIcon from './AppIcon.vue';
import { useAppStore } from '../stores/appStore';
import type { FileChangeEntry } from '../utils/turnGroups';
import { buildDiffBlocks, type DiffViewBlock } from '../utils/diffView';
import type {
  TurnUndoStateSummary,
  TurnUndoFileSummary,
  TurnUndoFileConflict,
  TurnUndoRecoveryPreviewPayload,
} from '../types/pywebview';

const props = defineProps<{
  changes: FileChangeEntry[];
  /** 按回合撤销：权威检查点状态摘要（旧后端 / 无关联回合缺省为 null） */
  undoState?: TurnUndoStateSummary | null;
  /** 权威回合身份；撤销预检/执行的定位键 */
  turnId?: string;
}>();

const store = useAppStore();
const open = ref(false);
// 当前展开查看 diff 的文件路径；同一时间只展开一个，再次点击收起
const expandedPath = ref('');

// --- 权威模式（检查点 ready 之后）的状态 ---
const authFiles = ref<TurnUndoFileSummary[] | null>(null);
const expandedFileId = ref('');
const authDiff = ref<Awaited<ReturnType<typeof store.fetchTurnUndoFileDiff>>>(null);
const authDiffLoading = ref(false);
const conflicts = ref<TurnUndoFileConflict[]>([]);
const recoveryOptions = ref<{ operationId: string; processed: string[]; remaining: string[] } | null>(null);
const recoveryPreview = ref<TurnUndoRecoveryPreviewPayload | null>(null);
const busy = ref(false);

const totalAdded = computed(() => props.changes.reduce((s, c) => s + c.added, 0));
const totalRemoved = computed(() => props.changes.reduce((s, c) => s + c.removed, 0));
const diffBlocksByPath = computed(() => new Map<string, DiffViewBlock[]>(
  props.changes.map((change) => [change.path, buildDiffBlocks(change.diffText, 5)]),
));
const authDiffBlocks = computed(() => {
  if (!authDiff.value || !authDiff.value.diff_text) return null;
  return buildDiffBlocks(authDiff.value.diff_text, 5);
});

function baseName(p: string): string {
  const norm = p.replace(/\\/g, '/');
  const i = norm.lastIndexOf('/');
  return i >= 0 ? norm.slice(i + 1) : norm;
}

/** 展开时进入权威模式：按 turn_id 拉取持久化文件清单（惰性，单次请求） */
async function toggleOpen() {
  open.value = !open.value;
  if (open.value && props.turnId && props.undoState && !authFiles.value && !conflicts.value.length) {
    const status = await store.fetchTurnUndoStatus(props.turnId);
    if (status && Array.isArray(status.files)) authFiles.value = status.files;
  }
}

/** 撤销按钮状态机（规格 16.3） */
const undoButton = computed<null | {
  label: string; kind: string; disabled: boolean; icon?: string;
  aria: string; title?: string;
}>(() => {
  const st = props.undoState;
  if (!props.turnId || !st) return null;
  const availability = st.snapshot_availability;
  if (st.status === 'recording') return null;
  if (st.status === 'no_changes') return null;
  if (st.status === 'ready') {
    if (availability === 'expired') {
      return { label: '撤销记录已过期', kind: 'muted', disabled: true, aria: '撤销记录已过期' };
    }
    return { label: '撤销 ↶', kind: 'primary', disabled: busy.value, icon: 'chevron-right', aria: '撤销本轮文件修改' };
  }
  if (st.status === 'conflicted') {
    return { label: '撤销受阻 · 重新检查', kind: 'warn', disabled: busy.value, aria: '撤销受阻，重新检查', title: '查看冲突详情' };
  }
  if (st.status === 'reverting') return { label: '正在撤销…', kind: 'muted', disabled: true, aria: '正在撤销' };
  if (st.status === 'recovering') return { label: '正在恢复撤销事务…', kind: 'muted', disabled: true, aria: '正在恢复撤销事务' };
  if (st.status === 'reverted') {
    if (availability === 'expired') return { label: '已撤销 · 原 diff 已过期', kind: 'muted', disabled: true, aria: '已撤销，原 diff 已过期' };
    return { label: '已撤销', kind: 'muted', disabled: true, aria: '已撤销' };
  }
  if (st.status === 'interrupted') {
    return { label: '撤销中断 · 查看恢复选项', kind: 'warn', disabled: busy.value, aria: '撤销中断，查看恢复选项' };
  }
  if (st.status === 'failed') {
    return { label: '撤销失败 · 重试', kind: 'warn', disabled: busy.value, aria: '撤销失败，可重新预检' };
  }
  if (st.status === 'unavailable') {
    return { label: '不可撤销', kind: 'muted', disabled: true, aria: '不可撤销', title: st.unavailable_code || '' };
  }
  return null;
});

const headTitle = computed(() => {
  const st = props.undoState;
  if (st && st.status === 'reverted') return `已撤销 ${st.file_count} 个文件`;
  if (st && (st.status === 'ready' || st.status === 'conflicted' || st.status === 'failed')) {
    return `${st.file_count} 个文件已编辑`;
  }
  return `${props.changes.length} 个文件已更改`;
});
const headStatsVisible = computed(() => {
  const st = props.undoState;
  if (st && st.status === 'reverted') return true;
  if (st && (st.status === 'ready' || st.status === 'conflicted' || st.status === 'failed')) return true;
  return true;
});
const headAdded = computed(() => {
  const st = props.undoState;
  if (st && (st.status === 'ready' || st.status === 'reverted' || st.status === 'conflicted' || st.status === 'failed')) {
    return st.net_added;
  }
  return totalAdded.value;
});
const headRemoved = computed(() => {
  const st = props.undoState;
  if (st && (st.status === 'ready' || st.status === 'reverted' || st.status === 'conflicted' || st.status === 'failed')) {
    return st.net_removed;
  }
  return totalRemoved.value;
});

function nodeBrief(n: { kind: string; exists: boolean; size: number | null; sha256: string | null }): string {
  if (n.kind === 'missing') return '不存在';
  const parts = [`普通文件`];
  if (n.size !== null && n.size !== undefined) parts.push(`${n.size} 字节`);
  if (n.sha256) parts.push(`sha=${n.sha256.slice(0, 8)}…`);
  return parts.join('，');
}

async function onUndoClick() {
  if (!props.turnId || busy.value) return;
  // M2：interrupted 状态点击按钮 → 打开恢复选项视图（而非走撤销预检死胡同）
  if (props.undoState?.status === 'interrupted') {
    conflicts.value = [];
    recoveryPreview.value = null;
    busy.value = true;
    try {
      const status = await store.fetchTurnUndoStatus(props.turnId);
      if (!status || !status.last_operation_id) {
        store.notifyChat('未找到中断的撤销操作记录');
        return;
      }
      recoveryOptions.value = {
        operationId: status.last_operation_id,
        processed: [],
        remaining: [],
      };
      open.value = true;
    } finally {
      busy.value = false;
    }
    return;
  }
  busy.value = true;
  conflicts.value = [];
  recoveryPreview.value = null;
  try {
    const first = await store.previewTurnUndo(props.turnId);
    if (!first) return;
    if (first.preview.conflicts && first.preview.conflicts.length) {
      // 冲突：不显示执行，展示结构化冲突；状态由后端置为 conflicted
      conflicts.value = first.preview.conflicts;
      open.value = true;
      return;
    }
    const preview = first.preview;
    // 组装确认文案（规格 16.4）：影响数量、删除新建清单、历史保留与副作用边界
    const lines: string[] = [
      `共 ${preview.file_count} 个文件：恢复原内容 ${preview.restore_content_count} 个、`
        + `删除本轮新建 ${preview.delete_created_count} 个、恢复本轮删除 ${preview.restore_deleted_count} 个。`,
    ];
    if (preview.created_files.length) {
      lines.push(`将删除的新建文件：${preview.created_files.join('、')}`);
    }
    lines.push('聊天历史保留，星瑶将记住文件结果已失效。');
    if (preview.has_uncovered_command_effects) {
      lines.push('⚠️ 本轮执行过通用命令：仅恢复列出的受控文件，命令的其他副作用不撤销。');
    }
    if (preview.subsequent_completed_turns > 0) {
      lines.push(
        `⚠️ 目标回合之后还有 ${preview.subsequent_completed_turns} 个已完成回合，`
        + '可能存在语义依赖，撤销后请重新检查或运行测试。',
      );
    }
    lines.push('建议撤销期间暂停其他工具对目标文件的写入。');
    const ok = await store.askConfirm('撤销本轮修改', '按回合撤销文件修改', lines.join('\n'), '撤销本轮修改 ⏎');
    if (!ok) return;
    // 重新预检拿新令牌（确认期间文件可能已变化，执行前会再次校验）
    const fresh = await store.previewTurnUndo(props.turnId);
    if (!fresh) return;
    if (fresh.preview.conflicts && fresh.preview.conflicts.length) {
      conflicts.value = fresh.preview.conflicts;
      open.value = true;
      return;
    }
    const res = await store.executeTurnUndo(props.turnId, fresh.token);
    if (res) {
      conflicts.value = [];
      open.value = false;
    }
  } finally {
    busy.value = false;
  }
}

async function startRecovery(action: 'continue_revert' | 'cancel_revert') {
  if (!recoveryOptions.value || busy.value) return;
  busy.value = true;
  try {
    const result = await store.previewTurnUndoRecovery(
      recoveryOptions.value.operationId, action,
    );
    if (!result) return;
    if (result.preview.recovery_required) {
      // 事务未知状态：只读详情，拒绝自动写入
      recoveryPreview.value = result.preview;
      return;
    }
    const ok = await store.askConfirm(
      action === 'continue_revert' ? '继续撤销' : '取消撤销',
      '撤销中断恢复',
      action === 'continue_revert'
        ? `将使用本轮开始前快照完成剩余撤销：${result.preview.remaining_files.join('、') || '无剩余文件'}`
        : '将把已处理路径恢复到点击撤销之前的状态',
      action === 'continue_revert' ? '继续撤销 ⏎' : '取消撤销 ⏎',
    );
    if (!ok) return;
    const done = await store.executeTurnUndoRecovery(
      recoveryOptions.value.operationId, action, result.token,
    );
    if (done) {
      recoveryPreview.value = null;
      recoveryOptions.value = null;
      open.value = false;
    }
  } finally {
    busy.value = false;
  }
}

function toggleExpand(path: string) {
  expandedPath.value = expandedPath.value === path ? '' : path;
}

async function toggleAuthExpand(f: TurnUndoFileSummary) {
  if (expandedFileId.value === f.file_id) {
    expandedFileId.value = '';
    authDiff.value = null;
    return;
  }
  expandedFileId.value = f.file_id;
  authDiff.value = null;
  if (!props.turnId) return;
  authDiffLoading.value = true;
  try {
    authDiff.value = await store.fetchTurnUndoFileDiff(props.turnId, f.file_id);
  } finally {
    authDiffLoading.value = false;
  }
}

function describeBinary(d: { before_size: number | null; after_size: number | null }): string {
  const before = d.before_size === null || d.before_size === undefined ? '?' : `${d.before_size} 字节`;
  const after = d.after_size === null || d.after_size === undefined ? '?' : `${d.after_size} 字节`;
  return `${before} → ${after}，不生成文本 diff`;
}

async function openFile(path: string) {
  const api = window.pywebview && window.pywebview.api;
  if (!api || !path) return;
  // diff 的 file_path 是工具参数原始值（模型常给相对路径），后端 open_path 按
  // GUI 进程 CWD 解析相对路径——多会话下进程 CWD 不随会话变，校验必然失败。
  // 在前端按当前会话工作目录拼出绝对路径再交给后端
  let target = path;
  const isAbsolute = /^[a-zA-Z]:[\\/]/.test(path) || path.startsWith('\\\\') || path.startsWith('/');
  if (!isAbsolute && store.workdir) {
    target = store.workdir.replace(/[\\/]+$/, '') + '\\' + path.replace(/^[\\/]+/, '');
  }
  try {
    const res = await api.open_path(target);
    if (res && res.ok === false && res.msg) {
      store.notifyModal('打开失败', res.msg);
    }
  } catch (err) {
    store.notifyModal('打开失败', String(err));
  }
}
</script>

<style scoped>
.cfp {
  border: 1px solid var(--border);
  border-radius: 10px;
  background: var(--bg2);
  overflow: hidden;
}
.cfp-head-row {
  display: flex;
  align-items: stretch;
}
.cfp-head {
  display: flex;
  align-items: center;
  gap: 8px;
  flex: 1;
  min-width: 0;
  padding: 9px 12px;
  background: none;
  border: none;
  cursor: pointer;
  font-family: inherit;
  font-size: 13px;
  color: var(--text-dim);
  text-align: left;
}
.cfp-head:hover {
  background: var(--bg3);
}
.cfp-head:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: -2px;
}
/* 撤销按钮：头部右侧独立操作区，与展开区互不嵌套、互不触发 */
.cfp-undo-btn {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  flex-shrink: 0;
  margin: 5px 8px;
  padding: 3px 10px;
  border-radius: 999px;
  border: 1px solid var(--border);
  background: var(--bg1);
  font-family: inherit;
  font-size: 12px;
  color: var(--text);
  cursor: pointer;
  white-space: nowrap;
}
.cfp-undo-btn.primary:hover {
  border-color: var(--accent);
  color: var(--accent);
}
.cfp-undo-btn.warn {
  border-color: color-mix(in srgb, var(--red) 45%, var(--border));
  color: var(--red);
}
.cfp-undo-btn.muted {
  color: var(--text-faint);
  cursor: default;
}
.cfp-undo-btn:disabled {
  opacity: 0.75;
  cursor: default;
}
.cfp-undo-btn:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 1px;
}
.cfp-ico {
  flex-shrink: 0;
  color: var(--text-faint);
}
.cfp-title {
  font-weight: 600;
  color: var(--text);
}
.cfp-stats {
  margin-left: auto;
  display: flex;
  gap: 8px;
  font-size: 12px;
  font-variant-numeric: tabular-nums;
}
.cfp-caret {
  flex-shrink: 0;
  color: var(--text-faint);
  transition: transform 0.15s ease;
}
.cfp-caret.is-open {
  transform: rotate(90deg);
}
.cfp-body {
  border-top: 1px solid var(--border);
  padding: 4px 0;
}
.cfp-row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 12px;
  cursor: pointer;
  font-size: 12px;
  user-select: none;
}
.cfp-row:hover {
  background: var(--bg3);
}
.cfp-row:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: -2px;
}
.cfp-row-ico {
  flex-shrink: 0;
  color: var(--text-faint);
}
.cfp-row-name {
  font-weight: 600;
  color: var(--text);
  flex-shrink: 0;
}
.cfp-row-path {
  color: var(--text-faint);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  min-width: 0;
}
.cfp-row-stats {
  margin-left: auto;
  display: flex;
  gap: 6px;
  font-size: 12px;
  font-variant-numeric: tabular-nums;
  flex-shrink: 0;
}
.cfp-open-btn {
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 20px;
  height: 20px;
  border: none;
  border-radius: 4px;
  background: none;
  color: var(--text-faint);
  cursor: pointer;
  padding: 0;
}
.cfp-open-btn:hover {
  background: var(--bg1);
  color: var(--accent);
}
.cfp-row-caret {
  flex-shrink: 0;
  color: var(--text-faint);
  transition: transform 0.15s ease;
}
.cfp-row-caret.is-open {
  transform: rotate(90deg);
}
.cfp-add {
  color: var(--green);
}
.cfp-del {
  color: var(--red);
}
.cfp-conflict-note {
  padding: 8px 12px 4px;
  font-size: 12px;
  color: var(--text-dim);
}
.cfp-conflict {
  margin: 6px 12px;
  padding: 6px 8px;
  border: 1px solid color-mix(in srgb, var(--red) 35%, var(--border));
  border-radius: 8px;
  font-size: 12px;
}
.cfp-conflict-path {
  font-weight: 600;
  color: var(--text);
}
.cfp-conflict-reason {
  color: var(--red);
}
.cfp-conflict-detail {
  color: var(--text-faint);
}
.cfp-conflict-warn {
  margin: 4px 12px;
  font-size: 12px;
  color: var(--red);
}
.cfp-recheck {
  margin: 4px 12px 8px;
  padding: 3px 10px;
  border-radius: 6px;
  border: 1px solid var(--border);
  background: var(--bg1);
  font-family: inherit;
  font-size: 12px;
  color: var(--text);
  cursor: pointer;
}
.cfp-recheck:hover {
  border-color: var(--accent);
  color: var(--accent);
}
.cfp-recovery-actions {
  display: flex;
  gap: 8px;
}

/* 展开的 diff 视图 */
.cfp-diff {
  margin: 0 8px 8px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--code-bg);
  overflow-x: auto;
  font-family: var(--mono);
  font-size: 11.5px;
  line-height: 1.5;
  padding: 4px 0;
}
.cfp-diff-line {
  display: flex;
  white-space: pre;
}
.cfp-no {
  width: 3.2em;
  flex-shrink: 0;
  text-align: right;
  padding-right: 0.6em;
  color: var(--text-faint);
  opacity: 0.75;
  user-select: none;
}
.cfp-sign {
  width: 1.2em;
  flex-shrink: 0;
  text-align: center;
  user-select: none;
}
.cfp-code {
  flex: 1;
  padding-right: 12px;
}
.cfp-diff-line.d-add {
  background: color-mix(in srgb, var(--green) 13%, transparent);
}
.cfp-diff-line.d-add .cfp-sign,
.cfp-diff-line.d-add .cfp-code {
  color: var(--green);
}
.cfp-diff-line.d-del {
  background: color-mix(in srgb, var(--red) 13%, transparent);
}
.cfp-diff-line.d-del .cfp-sign,
.cfp-diff-line.d-del .cfp-code {
  color: var(--red);
}
.cfp-diff-line.d-ctx .cfp-code {
  color: var(--text-dim);
}
.cfp-diff-gap {
  padding: 1px 0 1px 8.6em;
  color: var(--text-faint);
  user-select: none;
}
.cfp-diff-empty {
  padding: 8px 12px;
  color: var(--text-faint);
}
@media (prefers-reduced-motion: reduce) {
  .cfp-caret,
  .cfp-row-caret {
    transition: none;
  }
}
</style>

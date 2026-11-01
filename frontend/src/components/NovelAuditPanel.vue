<template>
  <!-- 只读体检面板：一个按钮 + 分级汇总 + 逐条发现 + 跳过项，不做折叠/分页 -->
  <div class="novel-audit">
    <div class="novel-audit-head">
      <!-- 运行审查 + 全部修复：修复入口只是把问题拼成指令交给星瑶，不在面板里直接改文件 -->
      <div class="novel-audit-actions">
        <button
          class="novel-audit-run"
          data-test="novel-audit-run"
          :disabled="loading"
          @click="emit('run')"
        >
          <AppIcon :name="loading ? 'loader' : 'shield'" :size="13" :class="{ 'icon-spin': loading }" />
          <span>{{ loading ? '审查中…' : '运行审查' }}</span>
        </button>
        <button
          v-if="findings.length > 0"
          class="novel-audit-fix-all"
          data-test="novel-audit-fix-all"
          :disabled="fixing || loading"
          @click="emit('fixAll')"
        >全部交给星瑶修（{{ findings.length }}）</button>
      </div>
      <span class="novel-audit-note">只读体检：追踪一致性 / 伏笔超期 / 字数达标 / 细纲缺口 / AI 味</span>
    </div>

    <div v-if="loading" class="novel-audit-loading" data-test="novel-audit-loading">
      <AppIcon name="loader" :size="12" class="icon-spin" />
      <span>正在扫描…</span>
    </div>

    <div v-if="errorMessage" class="novel-audit-error" data-test="novel-audit-error">
      {{ errorMessage }}
    </div>

    <template v-if="okResult">
      <div class="novel-audit-summary" data-test="novel-audit-summary">
        <div v-for="cell in summaryCells" :key="cell.level" class="novel-audit-cell" :class="cell.cls">
          <span class="novel-audit-cell-level">{{ cell.level }}</span>
          <span class="novel-audit-cell-count">{{ cell.count }}</span>
        </div>
      </div>

      <div v-if="findings.length === 0" class="novel-audit-clean">没有发现问题</div>
      <div v-else class="novel-audit-findings">
        <div
          v-for="(finding, index) in findings"
          :key="`${finding.type}-${finding.file}-${index}`"
          class="novel-audit-finding"
          data-test="novel-audit-finding"
        >
          <div class="novel-audit-finding-head">
            <span class="novel-audit-level" :class="levelClass(finding.level)">{{ finding.level }}</span>
            <span class="novel-audit-type">{{ finding.type }}</span>
            <span class="novel-audit-file">{{ finding.file }}</span>
            <button
              class="novel-audit-fix"
              data-test="novel-audit-fix"
              :disabled="fixing"
              @click="emit('fix', finding)"
            >修复</button>
          </div>
          <div class="novel-audit-message">{{ finding.message }}</div>
        </div>
      </div>

      <div v-if="skipped.length > 0" class="novel-audit-skipped" data-test="novel-audit-skipped">
        <div v-for="(item, index) in skipped" :key="`${item.check}-${index}`" class="novel-audit-skip-row">
          {{ item.check }}：{{ item.reason }}
        </div>
      </div>
    </template>

    <div v-else-if="nonNovel" class="novel-audit-clean">{{ result?.error || '当前项目还不是小说项目' }}</div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue';
import AppIcon from './AppIcon.vue';
import type { NovelAuditDto, NovelAuditFindingDto } from '../types/pywebview.d';

const props = defineProps<{
  /** 最近一次体检结果：null 表示还没跑过（只显示按钮与说明） */
  result: NovelAuditDto | null;
  /** 是否正在扫描：为真时按钮禁用并显示加载提示 */
  loading: boolean;
  /** 桥接层错误文本（调用失败 / 缺桥），非空时按 --red 显示 */
  error: string;
  /** 是否正在把问题交给星瑶：为真时禁用修复入口，避免重复点击 */
  fixing?: boolean;
}>();

const emit = defineEmits<{
  /** 点击「运行审查」：父组件负责调 run_novel_audit 并回填 result */
  (e: 'run'): void;
  /** 点击单条「修复」：把这条 finding 交给父组件拼成对话指令 */
  (e: 'fix', finding: NovelAuditFindingDto): void;
  /** 点击「全部修复」：父组件按 type 分组汇总出总指令 */
  (e: 'fixAll'): void;
}>();

// 分级固定四态（后端 AUDIT_LEVELS），缺项按 0 展示，不因缺键而漏格
const LEVELS: { level: string; cls: string }[] = [
  { level: 'S1', cls: 'is-s1' },
  { level: 'S2', cls: 'is-s2' },
  { level: 'S3', cls: 'is-s3' },
  { level: 'S4', cls: 'is-s4' },
];

// result.ok 为 true 才算有结论；is_novel=false 与非 ok 结果走提示/错误分支
const okResult = computed(() => (props.result?.ok === true ? props.result : null));
const nonNovel = computed(() => props.result?.is_novel === false);

const findings = computed(() => props.result?.findings ?? []);
const skipped = computed(() => props.result?.skipped ?? []);

// 错误文本优先级：桥接错误 > result.ok=false 时后端给的 error；
// 合成一条后只渲染一个 data-test="novel-audit-error"，不重复出块
const errorMessage = computed(() => {
  if (props.error) return props.error;
  const result = props.result;
  if (result && result.ok === false) return result.error || '审查未完成';
  return '';
});

function countOf(level: string): number {
  const summary = props.result?.summary;
  if (!summary) return 0;
  const n = Number(summary[level]);
  return Number.isFinite(n) && n > 0 ? Math.floor(n) : 0;
}

const summaryCells = computed(() => LEVELS.map((item) => ({ ...item, count: countOf(item.level) })));

// 结论等级配色：S1 红 / S2、S3 黄 / S4 灰（S3 是语雀黄绿，统一用 --yellow 更稳）
function levelClass(level: string): string {
  switch (String(level || '').trim().toUpperCase()) {
    case 'S1':
      return 'is-s1';
    case 'S2':
    case 'S3':
      return 'is-s2';
    default:
      return 'is-s4';
  }
}
</script>

<style scoped>
/* 颜色/字号只用全局 CSS 变量，跟随主题切换 */
.novel-audit {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.novel-audit-head {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

/* 运行审查与全部修复同一行：窄面板下允许换行，不挤压说明文案 */
.novel-audit-actions {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}

.novel-audit-run {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  align-self: flex-start;
  padding: 4px 12px;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: transparent;
  color: var(--text);
  font-size: 12px;
  cursor: pointer;
}
.novel-audit-run:hover:not(:disabled) {
  border-color: var(--accent);
  color: var(--text-bright);
}
.novel-audit-run:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

/* 修复入口：与 .novel-audit-run 同色系，尺寸更小，hover/disabled 口径一致 */
.novel-audit-fix-all,
.novel-audit-fix {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 2px 8px;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: transparent;
  color: var(--text-dim);
  font-size: 12px;
  cursor: pointer;
}
.novel-audit-fix-all:hover:not(:disabled),
.novel-audit-fix:hover:not(:disabled) {
  border-color: var(--accent);
  color: var(--text-bright);
}
.novel-audit-fix-all:disabled,
.novel-audit-fix:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
/* 单条修复贴在卡片头部最右侧：margin-left:auto 不挤掉 level/type/file */
.novel-audit-fix {
  margin-left: auto;
  flex: 0 0 auto;
}

.novel-audit-note {
  color: var(--text-faint);
  font-size: 11.5px;
  line-height: 1.5;
}

.novel-audit-loading {
  display: flex;
  align-items: center;
  gap: 6px;
  color: var(--text-dim);
  font-size: 12px;
}

.novel-audit-error {
  color: var(--red);
  font-size: 11.5px;
  line-height: 1.5;
  word-break: break-word;
}

.novel-audit-clean {
  color: var(--text-faint);
  font-size: 12px;
}

.novel-audit-summary {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 6px;
}
.novel-audit-cell {
  display: flex;
  flex-direction: column;
  gap: 1px;
  padding: 5px 6px;
  border: 1px solid var(--border);
  border-radius: 6px;
  background: var(--code-bg);
}
.novel-audit-cell-level {
  font-family: var(--mono);
  font-size: 10.5px;
  color: var(--text-dim);
}
.novel-audit-cell-count {
  font-family: var(--mono);
  font-size: 14px;
  font-weight: 600;
  color: var(--text-bright);
}
.novel-audit-cell.is-s1 .novel-audit-cell-count {
  color: var(--red);
}
.novel-audit-cell.is-s2 .novel-audit-cell-count,
.novel-audit-cell.is-s3 .novel-audit-cell-count {
  color: var(--yellow);
}
.novel-audit-cell.is-s4 .novel-audit-cell-count {
  color: var(--text-dim);
}

.novel-audit-findings {
  display: flex;
  flex-direction: column;
  gap: 6px;
  max-height: 260px;
  overflow-y: auto;
  padding-right: 2px;
}

.novel-audit-finding {
  display: flex;
  flex-direction: column;
  gap: 3px;
  padding: 6px 8px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--code-bg);
}
.novel-audit-finding-head {
  display: flex;
  align-items: baseline;
  gap: 6px;
  flex-wrap: wrap;
}
.novel-audit-level {
  padding: 0 5px;
  border: 1px solid currentColor;
  border-radius: 999px;
  font-family: var(--mono);
  font-size: 10.5px;
  line-height: 1.5;
}
.novel-audit-level.is-s1 {
  color: var(--red);
}
.novel-audit-level.is-s2 {
  color: var(--yellow);
}
.novel-audit-level.is-s4 {
  color: var(--text-dim);
}
.novel-audit-type {
  color: var(--text-dim);
  font-size: 11.5px;
}
.novel-audit-file {
  color: var(--text-faint);
  font-family: var(--mono);
  font-size: 10.5px;
  word-break: break-all;
}
.novel-audit-message {
  color: var(--text);
  font-size: 12px;
  line-height: 1.55;
  word-break: break-word;
}

.novel-audit-skipped {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 6px 8px;
  border: 1px dashed var(--border);
  border-radius: 8px;
}
.novel-audit-skip-row {
  color: var(--text-dim);
  font-size: 11.5px;
  line-height: 1.5;
  word-break: break-word;
}
</style>

<template>
  <!-- 角色快照卡片：窄面板里一角色一卡，空字段整行不渲染，避免出现大量空标签 -->
  <div v-if="cards.length === 0" class="novel-char-empty" data-test="novel-character-empty">
    追踪里还没有角色快照
  </div>
  <div v-else class="novel-char-list">
    <div
      v-for="(card, index) in cards"
      :key="`${card.name}-${index}`"
      class="novel-char-card"
      data-test="novel-character-card"
    >
      <div class="novel-char-head">
        <span class="novel-char-name">{{ card.name }}</span>
        <span v-if="card.identity" class="novel-char-identity">{{ card.identity }}</span>
      </div>
      <div v-for="field in card.fields" :key="field.label" class="novel-char-row">
        <span class="novel-char-label">{{ field.label }}：</span>
        <span class="novel-char-value">{{ field.value }}</span>
      </div>
      <div
        v-for="list in card.lists"
        :key="list.label"
        class="novel-char-row"
        :class="{ 'is-thread': list.warn }"
      >
        <span class="novel-char-label">{{ list.label }}：</span>
        <span class="novel-char-value" :class="{ 'is-thread': list.warn }">{{ list.text }}</span>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue';
import type { NovelWorkbenchCharacterDto } from '../types/pywebview.d';

const props = defineProps<{
  /** 角色表单项：来自 get_novel_workbench 的 characters 数组（缺失时父组件传空数组） */
  rows: NovelWorkbenchCharacterDto[];
}>();

type CharField = { label: string; value: string };
type CharList = { label: string; text: string; warn?: boolean };
type CharCard = { name: string; identity: string; fields: CharField[]; lists: CharList[] };

function text(value: unknown): string {
  return String(value ?? '').trim();
}

// 数组字段缺失时后端给空数组，这里再做一次防御：空串项过滤掉，
// 整组为空则整行不渲染（前端不补造内容）
function items(list: unknown): string[] {
  if (!Array.isArray(list)) return [];
  return list.map((item) => text(item)).filter((item) => item !== '');
}

const cards = computed<CharCard[]>(() =>
  props.rows.map((row) => {
    const fields: CharField[] = [];
    const state = text(row.state);
    if (state) fields.push({ label: '状态', value: state });
    const location = text(row.location);
    if (location) fields.push({ label: '位置', value: location });
    const goal = text(row.goal);
    if (goal) fields.push({ label: '目标', value: goal });

    const lists: CharList[] = [];
    const knowledge = items(row.knowledge);
    if (knowledge.length > 0) lists.push({ label: '已知', text: knowledge.join('，') });
    const abilities = items(row.abilities_resources);
    if (abilities.length > 0) lists.push({ label: '能力资源', text: abilities.join('，') });
    // 未决线索用黄色强调：它决定下一章该收哪条线
    const threads = items(row.open_threads);
    if (threads.length > 0) lists.push({ label: '未决线索', text: threads.join('，'), warn: true });
    const relations = items(row.relationships);
    if (relations.length > 0) lists.push({ label: '关系', text: relations.join('，') });

    return {
      name: text(row.name) || '未命名角色',
      identity: text(row.identity),
      fields,
      lists,
    };
  }),
);
</script>

<style scoped>
/* 颜色/字号只用全局 CSS 变量，跟随主题切换 */
.novel-char-empty {
  padding: 10px 2px;
  color: var(--text-faint);
  font-size: 12px;
}

.novel-char-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.novel-char-card {
  display: flex;
  flex-direction: column;
  gap: 3px;
  padding: 8px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--code-bg);
}

.novel-char-head {
  display: flex;
  align-items: baseline;
  gap: 6px;
  flex-wrap: wrap;
}
.novel-char-name {
  color: var(--text-bright);
  font-size: 12.5px;
  font-weight: 600;
}
.novel-char-identity {
  padding: 0 5px;
  border: 1px solid var(--border);
  border-radius: 999px;
  color: var(--accent);
  font-size: 10.5px;
  line-height: 1.5;
}

.novel-char-row {
  display: flex;
  align-items: baseline;
  gap: 2px;
  color: var(--text);
  font-size: 12px;
  line-height: 1.55;
  word-break: break-word;
}
.novel-char-label {
  flex-shrink: 0;
  color: var(--text-dim);
}
.novel-char-value {
  color: var(--text);
}
.novel-char-value.is-thread {
  color: var(--yellow);
}
</style>

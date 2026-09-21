<template>
  <div class="md" v-html="html" @click="onClick"></div>
</template>

<script setup lang="ts">
import { computed } from 'vue';
import { showToast } from 'vant';
import { renderMd } from '../utils/md';
import { copyText } from '../utils/clipboard';

const props = defineProps<{ source: string }>();
const html = computed(() => renderMd(props.source));

/** 代码块复制按钮（utils/md.ts 生成 .md-copy）——事件委托 */
function onClick(e: MouseEvent) {
  const btn = (e.target as HTMLElement | null)?.closest?.('.md-copy') as HTMLElement | null;
  if (!btn) return;
  const code = decodeURIComponent(btn.dataset.code || '');
  void copyText(code).then((ok) => showToast(ok ? '已复制' : '复制失败（浏览器限制）'));
}
</script>

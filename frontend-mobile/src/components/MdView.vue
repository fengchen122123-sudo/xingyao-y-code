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

function onClick(e: MouseEvent) {
  const el = e.target as HTMLElement | null;
  const toggle = el?.closest?.('.md-toggle') as HTMLElement | null;
  if (toggle) {
    const box = toggle.closest('.md-code') as HTMLElement | null;
    if (!box) return;
    const open = box.classList.toggle('open');
    toggle.textContent = open ? '收起' : '展开';
    return;
  }
  const btn = el?.closest?.('.md-copy') as HTMLElement | null;
  if (!btn) return;
  const code = decodeURIComponent(btn.dataset.code || '');
  void copyText(code).then((ok) => showToast(ok ? '已复制' : '复制失败（浏览器限制）'));
}
</script>

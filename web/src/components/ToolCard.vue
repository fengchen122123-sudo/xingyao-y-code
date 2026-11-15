<template>
  <article class="card tool-card">
    <span class="section-icon" aria-hidden="true">
      <svg class="icon" viewBox="0 0 24 24"><path d="m14.7 6.3 3 3M4 20l4.5-1 10-10a2.1 2.1 0 0 0-3-3l-10 10L4 20Z"/></svg>
    </span>
    <div><h2>{{ title }}</h2><p class="muted">{{ detail }}</p></div>
  </article>
</template>
<script setup lang="ts">
defineProps<{ title: string; detail: string }>();
</script>

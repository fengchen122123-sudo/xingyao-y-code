<template>
  <article class="card permission-card">
    <div class="permission-heading">
      <span class="section-icon" aria-hidden="true">
        <svg class="icon" viewBox="0 0 24 24"><path d="M12 3 3 7v5c0 5 3.8 8 9 9 5.2-1 9-4 9-9V7l-9-4Z"/><path d="M12 8v5M12 16h.01"/></svg>
      </span>
      <div><p class="eyebrow">等待你的确认</p><h2>{{ title }}</h2></div>
    </div>
    <code v-if="path" class="permission-path">{{ path }}</code>
    <div class="card-actions">
      <button class="button secondary-button" type="button" @click="$emit('deny')">先不做</button>
      <button class="button primary-button" type="button" @click="$emit('allow')">允许继续</button>
    </div>
  </article>
</template>
<script setup lang="ts">
defineProps<{ title: string; path: string }>();
defineEmits<{ allow: []; deny: [] }>();
</script>

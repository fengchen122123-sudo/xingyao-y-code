<template>
  <article class="card">
    <h2>{{ title }}</h2>
    <p class="muted">{{ path }}</p>
    <div class="row">
      <button class="text-button" type="button" @click="$emit('deny')">先不存</button>
      <button class="send" type="button" @click="$emit('allow')">存下来</button>
    </div>
  </article>
</template>
<script setup lang="ts">
defineProps<{ title: string; path: string }>();
defineEmits<{ allow: []; deny: [] }>();
</script>

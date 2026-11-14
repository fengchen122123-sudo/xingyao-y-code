<template>
  <article class="card">
    <h2>{{ title }}</h2>
    <p class="muted">{{ detail }}</p>
  </article>
</template>
<script setup lang="ts">
defineProps<{ title: string; detail: string }>();
</script>

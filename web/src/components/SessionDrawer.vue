<template>
  <aside class="card" v-if="open">
    <h2>会话</h2>
    <p v-if="!sessions.length" class="muted">还没有会话</p>
    <p v-for="session in sessions" :key="session.name">{{ session.name }}</p>
  </aside>
</template>
<script setup lang="ts">
defineProps<{ open: boolean; sessions: { name: string }[] }>();
</script>

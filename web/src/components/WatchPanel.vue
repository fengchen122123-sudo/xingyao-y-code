<template>
  <section class="card watch" v-if="src">
    <h2>看过的页面</h2>
    <img :src="src" alt="浏览器观察" />
  </section>
</template>
<script setup lang="ts">
defineProps<{ src: string }>();
</script>

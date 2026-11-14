<template>
  <section>
    <h2>用量</h2>
    <article class="card" v-for="row in admin.usage" :key="row.account_id + row.day + row.model">
      <p>{{ row.day }} · {{ row.model || "未命名模型" }}</p>
      <p class="muted">输入 {{ row.input_tokens }} · 输出 {{ row.output_tokens }} · 回合 {{ row.turns }}</p>
    </article>
  </section>
</template>
<script setup lang="ts">
import { onMounted } from "vue";
import { useAdminStore } from "../stores/admin";
const admin = useAdminStore();
onMounted(() => admin.loadUsage());
</script>

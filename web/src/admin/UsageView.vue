<template>
  <section class="admin-page">
    <header class="page-header admin-heading">
      <div><p class="eyebrow">Token 使用记录</p><h1>用量</h1><p>按日期、账号和模型查看真实消耗。</p></div>
      <span class="summary-chip">{{ admin.usage.length }} 条记录</span>
    </header>

    <div v-if="!admin.usage.length" class="card empty-card"><span class="empty-icon" aria-hidden="true"><svg class="icon" viewBox="0 0 24 24"><path d="M4 20V10M10 20V4M16 20v-7M22 20H2"/></svg></span><h2>还没有用量记录</h2><p class="muted">产生对话后，Token 用量会显示在这里。</p></div>
    <template v-else>
      <div class="usage-summary">
        <article class="card metric-card"><p>输入 Token</p><strong>{{ totalInput }}</strong></article>
        <article class="card metric-card"><p>输出 Token</p><strong>{{ totalOutput }}</strong></article>
        <article class="card metric-card"><p>对话回合</p><strong>{{ totalTurns }}</strong></article>
      </div>
      <div class="data-card"><div class="data-table-wrap"><table class="data-table usage-table">
        <thead><tr><th>日期 / 账号</th><th>模型</th><th>输入</th><th>输出</th><th>回合</th></tr></thead>
        <tbody><tr v-for="row in admin.usage" :key="row.account_id + row.day + row.model"><td><strong>{{ row.day }}</strong><small>{{ row.account_id }}</small></td><td>{{ row.model || "未命名模型" }}</td><td>{{ row.input_tokens }}</td><td>{{ row.output_tokens }}</td><td>{{ row.turns }}</td></tr></tbody>
      </table></div></div>
    </template>
  </section>
</template>
<script setup lang="ts">
import { computed, onMounted } from "vue";
import { useAdminStore } from "../stores/admin";
const admin = useAdminStore();
const totalInput = computed(() => admin.usage.reduce((sum, row) => sum + row.input_tokens, 0));
const totalOutput = computed(() => admin.usage.reduce((sum, row) => sum + row.output_tokens, 0));
const totalTurns = computed(() => admin.usage.reduce((sum, row) => sum + row.turns, 0));
onMounted(() => admin.loadUsage());
</script>

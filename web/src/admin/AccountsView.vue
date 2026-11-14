<template>
  <section>
    <h2>账号</h2>
    <article class="card" v-for="account in admin.accounts" :key="account.id">
      <strong>{{ account.username }}</strong>
      <p class="muted">{{ account.role }} · {{ account.disabled ? "已停用" : "可用" }}</p>
    </article>
  </section>
</template>
<script setup lang="ts">
import { onMounted } from "vue";
import { useAdminStore } from "../stores/admin";
const admin = useAdminStore();
onMounted(() => admin.loadAccounts());
</script>

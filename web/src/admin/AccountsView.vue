<template>
  <section>
    <h2>账号</h2>
    <form class="card" @submit.prevent="create">
      <label>用户名<input v-model="username" required /></label>
      <label>口令<input v-model="password" type="password" required /></label>
      <button class="send" type="submit">新建</button>
    </form>
    <article class="card" v-for="account in admin.accounts" :key="account.id">
      <strong>{{ account.username }}</strong>
      <p class="muted">{{ account.role }} · {{ account.disabled ? "已停用" : "可用" }}</p>
      <button v-if="!account.disabled" class="text-button" type="button" @click="admin.disableAccount(account.id)">停用</button>
    </article>
  </section>
</template>
<script setup lang="ts">
import { onMounted, ref } from "vue";
import { useAdminStore } from "../stores/admin";
const admin = useAdminStore();
const username = ref("");
const password = ref("");
onMounted(() => admin.loadAccounts());

async function create() {
  await admin.createAccount(username.value, password.value);
  username.value = "";
  password.value = "";
}
</script>

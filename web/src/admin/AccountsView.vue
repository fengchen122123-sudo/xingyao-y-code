<template>
  <section class="admin-page">
    <header class="page-header admin-heading">
      <div><p class="eyebrow">成员与权限</p><h1>账号</h1><p>为成员创建独立账号，管理登录状态。</p></div>
      <span class="summary-chip">{{ admin.accounts.length }} 个账号</span>
    </header>

    <details class="card create-panel">
      <summary><span><strong>新建账号</strong><small>创建后即可分配设备与模型</small></span><span class="summary-action">添加</span></summary>
      <form class="form-grid two-columns" @submit.prevent="create">
        <label class="field"><span>用户名</span><input v-model="username" required autocomplete="off" placeholder="成员用户名" /></label>
        <label class="field"><span>口令</span><input v-model="password" type="password" required autocomplete="new-password" placeholder="设置初始口令" /></label>
        <div class="form-actions"><button class="button primary-button" type="submit">创建账号</button></div>
      </form>
    </details>

    <div v-if="!admin.accounts.length" class="card empty-card"><h2>还没有账号</h2><p class="muted">从上方创建第一个成员账号。</p></div>
    <div v-else class="data-card">
      <div class="data-table-wrap">
        <table class="data-table">
          <thead><tr><th>账号</th><th>角色</th><th>状态</th><th class="align-right">操作</th></tr></thead>
          <tbody>
            <tr v-for="account in admin.accounts" :key="account.id">
              <td><div class="entity-name"><span class="avatar avatar-sm">{{ account.username.slice(0, 1) }}</span><div><strong>{{ account.username }}</strong><small>{{ account.id }}</small></div></div></td>
              <td>{{ account.role === "admin" ? "管理员" : account.role }}</td>
              <td><span class="status-pill" :class="account.disabled ? 'neutral' : 'success'">{{ account.disabled ? "已停用" : "可用" }}</span></td>
              <td class="align-right"><button v-if="!account.disabled" class="button quiet-danger-button small-button" type="button" @click="admin.disableAccount(account.id)">停用</button><span v-else class="muted">—</span></td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <section class="admin-subsection">
      <div class="section-heading"><div><h2>重置口令</h2><p>选择账号并设置新口令，原口令将不能继续使用。</p></div></div>
      <form class="card inline-form" @submit.prevent="reset">
        <label class="field"><span>账号</span><select v-model="resetAccountId" required><option value="" disabled>选择账号</option><option v-for="account in admin.accounts" :key="account.id" :value="account.id">{{ account.username }}</option></select></label>
        <label class="field"><span>新口令</span><input v-model="resetPassword" type="password" autocomplete="new-password" placeholder="输入新口令" required /></label>
        <button class="button secondary-button" type="submit">重置口令</button>
      </form>
    </section>
  </section>
</template>
<script setup lang="ts">
import { onMounted, ref } from "vue";
import { useAdminStore } from "../stores/admin";
const admin = useAdminStore();
const username = ref("");
const password = ref("");
const resetAccountId = ref("");
const resetPassword = ref("");
onMounted(() => admin.loadAccounts());

async function create() {
  await admin.createAccount(username.value, password.value);
  username.value = "";
  password.value = "";
}

async function reset() {
  await admin.resetPassword(resetAccountId.value, resetPassword.value);
  resetAccountId.value = "";
  resetPassword.value = "";
}
</script>

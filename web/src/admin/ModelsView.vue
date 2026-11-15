<template>
  <section class="admin-page">
    <header class="page-header admin-heading">
      <div><p class="eyebrow">统一模型服务</p><h1>模型</h1><p>配置给所有账号使用，受邀者不用自己准备 Key。</p></div>
      <span class="summary-chip">{{ admin.models.length }} 个服务</span>
    </header>

    <details class="card create-panel">
      <summary><span><strong>保存模型服务</strong><small>更新后对所有账号生效</small></span><span class="summary-action">配置</span></summary>
      <form class="form-grid two-columns" @submit.prevent="save">
        <label class="field"><span>服务名称</span><input v-model="form.name" required placeholder="深度求索" /></label>
        <label class="field"><span>接口地址</span><input v-model="form.base_url" required inputmode="url" placeholder="https://api.deepseek.com" /></label>
        <label class="field full-field"><span>Key</span><input v-model="form.api_key" required type="password" autocomplete="off" placeholder="服务密钥" /></label>
        <label class="field"><span>模型编号</span><input v-model="form.model_id" required placeholder="ds-v4-pro" /></label>
        <label class="field"><span>显示名称</span><input v-model="form.model_name" required placeholder="对话中展示的名称" /></label>
        <label class="field full-field"><span>调用名</span><input v-model="form.litellm_model" required placeholder="openai/deepseek-v4-pro" /></label>
        <div class="form-actions"><button class="button primary-button" type="submit">保存给所有账号</button></div>
      </form>
    </details>

    <div v-if="!admin.models.length" class="card empty-card"><h2>还没有模型服务</h2><p class="muted">从上方填写接口信息，保存后即可使用。</p></div>
    <div v-else class="model-list">
      <article class="card model-card" v-for="model in admin.models" :key="model.model_id">
        <div class="model-card-top"><span class="section-icon" aria-hidden="true"><svg class="icon" viewBox="0 0 24 24"><rect x="4" y="4" width="16" height="16" rx="3"/><path d="M9 9h6v6H9zM9 1v3M15 1v3M9 20v3M15 20v3M20 9h3M20 14h3M1 9h3M1 14h3"/></svg></span><span class="status-pill" :class="model.model_id === admin.activeModelId ? 'success' : 'neutral'">{{ model.model_id === admin.activeModelId ? "当前模型" : "已配置" }}</span></div>
        <h2>{{ model.model_name || model.model_id }}</h2>
        <p class="muted">{{ model.name }}</p>
        <dl class="detail-list"><div><dt>模型编号</dt><dd>{{ model.model_id }}</dd></div><div><dt>接口地址</dt><dd>{{ model.base_url }}</dd></div></dl>
      </article>
    </div>
  </section>
</template>
<script setup lang="ts">
import { onMounted, reactive } from "vue";
import { useAdminStore } from "../stores/admin";

const admin = useAdminStore();
const form = reactive({ name: "", base_url: "", api_key: "", model_id: "", model_name: "", litellm_model: "" });
onMounted(() => admin.loadModels());

async function save() {
  await admin.saveModels([{ ...form }], form.model_id);
  form.api_key = "";
}
</script>

<template>
  <div class="admin-layout">
    <aside class="admin-sidebar">
      <div class="admin-brand">
        <span class="avatar avatar-sm">星</span>
        <div><strong>星瑶</strong><small>管理后台</small></div>
      </div>
      <nav class="admin-nav" aria-label="管理导航">
        <router-link to="/admin/accounts">
          <svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="8" r="3"/><path d="M5 21v-2a7 7 0 0 1 14 0v2"/></svg><span>账号</span>
        </router-link>
        <router-link to="/admin/models">
          <svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><rect x="4" y="4" width="16" height="16" rx="3"/><path d="M9 9h6v6H9zM9 1v3M15 1v3M9 20v3M15 20v3M20 9h3M20 14h3M1 9h3M1 14h3"/></svg><span>模型</span>
        </router-link>
        <router-link to="/admin/devices">
          <svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><rect x="4" y="5" width="16" height="11" rx="2"/><path d="M2 19h20"/></svg><span>设备</span>
        </router-link>
        <router-link to="/admin/workers">
          <svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><rect x="3" y="4" width="18" height="6" rx="2"/><rect x="3" y="14" width="18" height="6" rx="2"/><path d="M7 7h.01M7 17h.01M11 7h6M11 17h6"/></svg><span>工人</span>
        </router-link>
        <router-link to="/admin/usage">
          <svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M4 20V10M10 20V4M16 20v-7M22 20H2"/></svg><span>用量</span>
        </router-link>
      </nav>
      <router-link class="admin-back" to="/chat">
        <svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M15 5 8 12l7 7"/></svg><span>返回对话</span>
      </router-link>
    </aside>
    <div class="admin-content">
      <router-view />
    </div>
  </div>
</template>

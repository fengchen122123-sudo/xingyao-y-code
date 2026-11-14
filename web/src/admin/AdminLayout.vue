<template>
  <main class="page">
    <nav class="nav">
      <router-link to="/admin/accounts">账号</router-link>
      <router-link to="/admin/devices">设备</router-link>
      <router-link to="/admin/workers">工人</router-link>
      <router-link to="/admin/usage">用量</router-link>
    </nav>
    <router-view />
  </main>
</template>

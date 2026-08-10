import { createApp } from 'vue';
import { createPinia } from 'pinia';
import './style.css';
import App from './App.vue';

// 启动画面在 get_initial_state 返回前就会渲染：先从 localStorage 镜像同步
// 恢复主题，避免浅色主题用户看到黑色 splash（后端真值返回后会再校正一次）
try {
  if (localStorage.getItem('ycode-theme-light') === '1') {
    document.body.classList.add('light');
  }
} catch { /* localStorage 不可用时保持默认深色 */ }

const app = createApp(App);
app.use(createPinia());
app.mount('#app');

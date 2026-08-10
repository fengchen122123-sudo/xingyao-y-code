import { createApp } from 'vue';
import { createPinia } from 'pinia';
import './style.css';
import App from './App.vue';

// 启动画面在 get_initial_state 返回前就会渲染：先同步恢复主题，避免浅色主题
// 用户看到黑色 splash（后端真值返回后会再校正一次）。
// 主题来源优先级：URL #theme= 片段（启动时由 Python 从 config.json 拼入，
// 最可靠）> localStorage 镜像（pywebview 默认无痕模式不持久化，仅作兜底）。
try {
  const m = location.hash.match(/theme=(light|dark)/);
  const light = m ? m[1] === 'light' : localStorage.getItem('ycode-theme-light') === '1';
  if (light) {
    document.body.classList.add('light');
  }
} catch { /* 保持默认深色 */ }

const app = createApp(App);
app.use(createPinia());
app.mount('#app');

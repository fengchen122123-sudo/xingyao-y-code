import { createApp } from 'vue';
import { createPinia } from 'pinia';
import './style.css';
import App from './App.vue';
import { applyBootTheme } from './themes/runtime';

// 启动画面在 get_initial_state 返回前就会渲染：先同步恢复主题，避免浅色主题
// 用户看到黑色 splash（后端真值返回后会再校正一次）。
// 启动片段由 Python 根据已校验的活动 GUI 主题生成。这里只接受固定枚举和
// 六位 HEX，不把片段内容作为 CSS 或 HTML 注入。
try {
  applyBootTheme(location.hash, document.body);
} catch { /* 保持默认深色 */ }

const app = createApp(App);
app.use(createPinia());
app.mount('#app');

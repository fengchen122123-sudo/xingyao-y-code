import { createApp } from 'vue';
import { createPinia } from 'pinia';
import './style.css';
import './themes/field-notes.css';
// 内置 QQ2009 皮肤的专属质感层（渐变标题栏/橙色悬停等平色 token 表达不了的效果）
import './themes/qq2009.css';
// 内置深夜工位皮肤的专属细节层（状态栏等宽读数、当前项脊线、助手消息签名线）
import './themes/lamp.css';
// 内置晨光皮肤的专属细节层（读数对齐、时间列等宽、助手消息去卡片化）
import './themes/daybreak.css';
import App from './App.vue';
import { applyBootTheme } from './themes/runtime';
import { useAppStore } from './stores/appStore';
import { registerFrontendUpdateHandlers } from './utils/frontendUpdate';
import { loadAppGlass } from './composables/appGlass';

// 启动画面在 get_initial_state 返回前就会渲染：先同步恢复主题，避免浅色主题
// 用户看到黑色 splash（后端真值返回后会再校正一次）。
// 启动片段由 Python 根据已校验的活动 GUI 主题生成。这里只接受固定枚举和
// 六位 HEX，不把片段内容作为 CSS 或 HTML 注入。
try {
  applyBootTheme(location.hash, document.body);
} catch { /* 保持默认深色 */ }

// 全屏玻璃层偏好（设置 → 外观与主题 → 玻璃效果）：启动即恢复，读不到就是默认关闭。
// 必须在 mount 之前应用，否则启动首帧会先闪一下无玻璃的版本。
loadAppGlass();

const app = createApp(App);
app.use(createPinia());

// 界面资源版本失配自愈（详见 utils/frontendUpdate.ts）：dist 运行期更新、
// 或旧构建哈希 chunk 动态 import 失败（vite:preloadError）时，等界面空闲
// 自动刷新到最新版本。注入闸门：任一会话有进行中回合就不刷新，避免打断
// 正在进行的对话；回合结束后由内部定时器复检补刷。
// 注册必须早于 app.mount：mount 阶段就可能发生懒 chunk 加载失败（vite:preloadError），
// 晚注册会错过这次自愈信号；连续自愈触顶时给出可关闭的告警提示。
const store = useAppStore();
registerFrontendUpdateHandlers({
  canReload: () => store.runningSessions.length === 0,
  onExhausted: () => store.notifyModal(
    '自愈刷新已暂停',
    '检测到前端资源持续异常（构建产物可能损坏）。已连续自动刷新多次仍未恢复，'
      + '为避免无限重载，本次会话已暂停自动刷新。请手动刷新页面，或重新构建前端后再试。',
  ),
});

app.mount('#app');

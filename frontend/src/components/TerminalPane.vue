<template>
  <div class="terminal-pane" data-test="terminal-pane">
    <div
      class="terminal-resizer"
      :class="{ resizing }"
      title="拖拽调整终端高度，双击恢复默认"
      data-test="terminal-resizer"
      @pointerdown="onResizerDown"
      @dblclick="resetHeight"
    ></div>

    <div class="terminal-head">
      <AppIcon name="square-terminal" :size="13" />
      <span class="term-title">终端</span>
      <span class="term-cwd" :title="cwd || ''">{{ cwd || 'PowerShell' }}</span>
      <span class="term-spacer"></span>
      <span v-if="!alive" class="term-dead" data-test="terminal-dead">已退出</span>
      <button class="term-btn" title="重启终端" data-test="terminal-restart" @click="restart">
        <AppIcon name="refresh" :size="13" />
      </button>
      <button class="term-btn" title="收起终端" data-test="terminal-hide" @click="store.toggleTerminalPanel()">
        <AppIcon name="x" :size="13" />
      </button>
    </div>
    <div ref="hostRef" class="terminal-host" data-test="terminal-host"></div>
  </div>
</template>

<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { Terminal } from '@xterm/xterm';
import { FitAddon } from '@xterm/addon-fit';
import '@xterm/xterm/css/xterm.css';
import AppIcon from './AppIcon.vue';
import { useAppStore } from '../stores/appStore';
import { onPtyData, onPtyExit } from '../utils/ptyBus';
import {
  TERMINAL_PANEL_DEFAULT_HEIGHT,
  TERMINAL_PANEL_MAX_HEIGHT,
  TERMINAL_PANEL_MIN_HEIGHT,
} from '../stores/appStore';

const store = useAppStore();
const hostRef = ref<HTMLElement | null>(null);
const alive = ref(true);
const cwd = ref('');
const resizing = ref(false);

let term: Terminal | null = null;
let fitAddon: FitAddon | null = null;
let resizeObserver: ResizeObserver | null = null;
let unsubs: Array<() => void> = [];
let resizePushTimer: number | undefined;
let started = false;

const ANSI_DARK: Record<string, string> = {
  black: '#2e2e2e',
  red: '#cd3131',
  green: '#0dbc79',
  yellow: '#e5e510',
  blue: '#2472c8',
  magenta: '#bc3fbc',
  cyan: '#11a8cd',
  white: '#e5e5e5',
  brightBlack: '#666666',
  brightRed: '#f14c4c',
  brightGreen: '#23d18b',
  brightYellow: '#f5f543',
  brightBlue: '#3b8eea',
  brightMagenta: '#d670d6',
  brightCyan: '#29b8db',
  brightWhite: '#ffffff',
};

const ANSI_LIGHT: Record<string, string> = {
  black: '#000000',
  red: '#cd3131',
  green: '#00bc00',
  yellow: '#949800',
  blue: '#0451a5',
  magenta: '#bc05bc',
  cyan: '#0598bc',
  white: '#555555',
  brightBlack: '#666666',
  brightRed: '#cd3131',
  brightGreen: '#14ce14',
  brightYellow: '#b5ba00',
  brightBlue: '#0451a5',
  brightMagenta: '#bc05bc',
  brightCyan: '#0598bc',
  brightWhite: '#a5a5a5',
};

function readThemeColors(): Record<string, string> {
  const cs = getComputedStyle(document.documentElement);
  const v = (name: string, fallback: string) => cs.getPropertyValue(name).trim() || fallback;
  const light = store.themeLight;
  return {
    background: v('--bg0', light ? '#ffffff' : '#16161a'),
    foreground: v('--text', light ? '#333333' : '#e8e8e6'),
    cursor: v('--accent', '#6c63ff'),
    cursorAccent: v('--bg0', light ? '#ffffff' : '#16161a'),
    selectionBackground: v('--accent-soft', 'rgba(108, 99, 255, 0.35)'),
    ...(light ? ANSI_LIGHT : ANSI_DARK),
  };
}

function applyTerminalTheme() {
  if (!term) return;
  term.options.theme = readThemeColors();
}

function fitAndSync(debouncePush: boolean) {
  if (!term || !fitAddon || !hostRef.value) return;
  try {
    fitAddon.fit();
  } catch {
    // 容器不可见（宽高为 0）时 fit 会抛，忽略等待下次
    return;
  }
  const { cols, rows } = term;
  if (debouncePush) {
    window.clearTimeout(resizePushTimer);
    resizePushTimer = window.setTimeout(() => {
      void store.ptyTerminalResize(cols, rows);
    }, 150);
  } else {
    void store.ptyTerminalResize(cols, rows);
  }
}

async function startSession() {
  if (started) return;
  started = true;
  const res = await store.ensurePtyTerminal(term?.cols ?? 80, term?.rows ?? 24);
  if (res?.ok) {
    alive.value = true;
    if (res.cwd) cwd.value = res.cwd;
    // 组件重建（如设置页往返）后重连到仍在运行的 shell：屏幕缓冲已丢，
    // 提示一行避免用户误以为终端空了
    if (res.reused && term) {
      term.writeln('\x1b[90m[已重新连接到运行中的终端，按回车刷新提示符]\x1b[0m');
    }
  } else {
    alive.value = false;
  }
}

function onResizerDown(e: PointerEvent) {
  const startY = e.clientY;
  const startHeight = store.terminalPanelHeight;
  const maxHeight = Math.max(TERMINAL_PANEL_MIN_HEIGHT, Math.min(TERMINAL_PANEL_MAX_HEIGHT, window.innerHeight - 260));
  const handle = e.currentTarget as HTMLElement;
  try { handle.setPointerCapture?.(e.pointerId); } catch { }
  resizing.value = true;
  const onMove = (ev: PointerEvent) => {
    const next = Math.min(maxHeight, Math.max(TERMINAL_PANEL_MIN_HEIGHT, startHeight + (startY - ev.clientY)));
    store.setTerminalPanelHeight(next);
  };
  const onUp = (ev: PointerEvent) => {
    try { handle.releasePointerCapture?.(ev.pointerId); } catch { }
    window.removeEventListener('pointermove', onMove);
    window.removeEventListener('pointerup', onUp);
    window.removeEventListener('pointercancel', onUp);
    resizing.value = false;
    void ev;
  };
  window.addEventListener('pointermove', onMove);
  window.addEventListener('pointerup', onUp);
  window.addEventListener('pointercancel', onUp);
  e.preventDefault();
}

function resetHeight() {
  store.setTerminalPanelHeight(TERMINAL_PANEL_DEFAULT_HEIGHT);
}

async function restart() {
  await store.ptyTerminalKill();
  if (term) term.reset();
  started = false;
  await startSession();
  fitAndSync(false);
}

// xterm 依赖完整浏览器 API（matchMedia/ResizeObserver）；jsdom 测试环境缺失，
// 跳过初始化避免拖垮所有挂载 App 的测试。真实 WebView2 中两者恒存在。
const xtermSupported =
  typeof window.matchMedia === 'function' && typeof window.ResizeObserver === 'function';

// 会话拉起时机：桥就绪 且 面板处于打开态（v-show 挂载不等于使用，启动即拉
// shell 只在恢复的打开状态下发生）。注册在 setup 顶层——不依赖 xterm 是否
// 初始化成功，且绝不早于 bridgeReady，否则会踩进 pywebview 注入前的空窗期
// 触发 _api() 的连接失败弹窗（启动竞态，曾让每次启动都弹「pywebview API 不可用」）
unsubs.push(
  watch(
    () => store.bridgeReady && store.terminalPanelOpen,
    (ready) => {
      if (ready) {
        void startSession();
        requestAnimationFrame(() => {
          applyTerminalTheme();
          fitAndSync(false);
          term?.focus();
        });
      }
    },
    { immediate: true }
  )
);

onMounted(() => {
  if (!hostRef.value || !xtermSupported) return;
  term = new Terminal({
    convertEol: false,
    cursorBlink: true,
    fontSize: 12,
    fontFamily: 'Consolas, "Courier New", monospace',
    scrollback: 5000,
    theme: readThemeColors(),
  });
  fitAddon = new FitAddon();
  term.loadAddon(fitAddon);
  term.open(hostRef.value);
  fitAndSync(false);

  term.onData((text) => {
    void store.ptyTerminalWrite(text);
  });

  unsubs.push(onPtyData((text) => {
    term?.write(text);
  }));
  unsubs.push(onPtyExit((code) => {
    alive.value = false;
    term?.write(`\r\n\x1b[90m[进程已退出，代码 ${code}，点击 ⟳ 重启]\x1b[0m\r\n`);
  }));

  resizeObserver = new ResizeObserver(() => fitAndSync(true));
  resizeObserver.observe(hostRef.value);

  unsubs.push(
    watch(() => store.themeLight, () => {
      requestAnimationFrame(applyTerminalTheme);
    })
  );
});

onBeforeUnmount(() => {
  window.clearTimeout(resizePushTimer);
  for (const unsub of unsubs) unsub();
  unsubs = [];
  resizeObserver?.disconnect();
  resizeObserver = null;
  term?.dispose();
  term = null;
  fitAddon = null;
});
</script>

<style scoped>
.terminal-pane {
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  height: v-bind("store.terminalPanelHeight + 'px'");
  border-top: 1px solid var(--border);
  background: var(--bg0);
  min-height: 0;
  position: relative;
}

.terminal-resizer {
  position: absolute;
  top: -3px;
  left: 0;
  right: 0;
  height: 6px;
  cursor: row-resize;
  z-index: 40;
}
.terminal-resizer::after {
  content: '';
  position: absolute;
  top: 2px;
  left: 0;
  right: 0;
  height: 2px;
  background: transparent;
  transition: background 0.15s;
}
.terminal-resizer:hover::after,
.terminal-resizer.resizing::after {
  background: var(--accent);
}

.terminal-head {
  display: flex;
  align-items: center;
  gap: 7px;
  height: 30px;
  padding: 0 10px;
  border-bottom: 1px solid var(--border);
  font-size: 11.5px;
  color: var(--text-dim);
  flex-shrink: 0;
  user-select: none;
}
.term-title {
  font-weight: 600;
  color: var(--text);
}
.term-cwd {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--text-faint);
}
.term-spacer {
  flex: 1;
}
.term-dead {
  color: var(--orange);
  font-size: 11px;
}
.term-btn {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 24px;
  height: 22px;
  border: none;
  background: transparent;
  color: var(--text-dim);
  border-radius: 5px;
  cursor: pointer;
  transition: background 0.12s, color 0.12s;
}
.term-btn:hover {
  background: var(--bg2);
  color: var(--text);
}

.terminal-host {
  flex: 1;
  min-height: 0;
  padding: 4px 6px 6px;
}
.terminal-host :deep(.xterm) {
  height: 100%;
}
</style>

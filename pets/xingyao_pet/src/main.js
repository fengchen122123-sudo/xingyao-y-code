// main.js — 桌宠主进程：窗口、托盘、持久化、门禁记录。
// 平台差异一律调 platform.js，本文件零 process.platform 判断。
const { app, BrowserWindow, Tray, Menu, ipcMain, screen } = require("electron");
const path = require("path");
const fs = require("fs");
const platform = require("./platform");

// 回归自检开关：PET_SELFTEST=1 时隔离 userData，避免与正在运行的桌宠抢单实例锁
if (process.env.PET_SELFTEST === "1") {
  app.setPath("userData", path.join(require("os").tmpdir(), "xingyao-pet-selftest"));
}
// 星瑶集成版：userData 固定到 ~/.ycode/xingyao-pet-data。
// 桌面宠物随星瑶一起分发，不能和开发目录里的同名实例共用单实例锁与位置设置，
// 否则彼此一启动就互相顶掉。PET_SELFTEST 分支的临时目录优先级更高，不受影响。
if (process.env.PET_SELFTEST !== "1") {
  try {
    app.setPath("userData", path.join(require("os").homedir(), ".ycode", "xingyao-pet-data"));
  } catch { /* 路径不可写时退回 Electron 默认值，不让桌宠起不来 */ }
}

const BASE_SIZE = 128; // 256@2x 显示为 128pt
// 气泡专用顶部带：窗口在角色正方形之上再加这段高度，气泡只画在这条带里，绝不压到头部。
// 角色区仍是「窗口宽 × 窗口宽」的正方形且贴底，角色显示尺寸与旧版完全一致。
const BUBBLE_BAND = 56;
const STATE_FILE = "pet-state.json";
const GATE_LOG = ".gate.log";

let win = null;
let tray = null;
let gateReady = false;
let settings = { alwaysOnTop: true, clickThrough: false, quiet: false, animate: true };

// 尺寸锁（关键）：150% DPI 的 Windows 上，把「读回的当前尺寸」再喂给 setBounds / setPosition，
// 每移一次窗口宽度就 +1px（实测 200 次 → +200px）。移窗时尺寸一律用这个常量显式传完整矩形，
// 绝不读回 width/height 参与换算，否则角色会越拖越大。
let lockedSize = { width: 0, height: 0 };
let correctingSize = false;

function clampSize(v, fallback) {
  return Number.isFinite(v) ? Math.min(Math.max(Math.round(v), 64), 480) : fallback;
}

// ---------- 持久化 ----------
function statePath() {
  return path.join(app.getPath("userData"), STATE_FILE);
}

function loadState() {
  try {
    return JSON.parse(fs.readFileSync(statePath(), "utf8"));
  } catch {
    return {};
  }
}

let saveTimer = null;
function saveState(patch) {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => {
    const cur = loadState();
    const next = { ...cur, ...patch };
    fs.mkdirSync(path.dirname(statePath()), { recursive: true });
    fs.writeFileSync(statePath(), JSON.stringify(next, "utf8"), "utf8");
  }, 500);
}

// ---------- 门禁记录 ----------
function gateLog(line) {
  try {
    fs.appendFileSync(path.join(app.getPath("userData"), GATE_LOG), line + "\n", "utf8");
  } catch { /* 日志不可写也不影响运行 */ }
  console.log(line);
}

// ---------- 尺寸（模块级：托盘菜单与 IPC 共用；原先定义在 registerIpc 内会 ReferenceError） ----------
function resizePet(pt) {
  if (!win) return;
  const d = screen.getPrimaryDisplay();
  const s = clampSize(platform.windowSize(d, pt), BASE_SIZE);
  const b = win.getBounds();
  const bottom = b.y + b.height;  // 底部锚定：换尺寸后角色脚底在屏幕上的位置不变
  lockedSize = { width: s, height: s + BUBBLE_BAND };
  win.setBounds({
    x: b.x, y: bottom - lockedSize.height,
    width: lockedSize.width, height: lockedSize.height
  });
  saveState({ width: s });        // 只存正方形边长，避免把 s+BUBBLE_BAND 当边长读回
}

// ---------- G3 窗口自检（模块级，普通启动与回归自检共用） ----------
function runG3Check() {
  if (!win) return;
  const b = win.getBounds();
  const inScreen = screen.getAllDisplays().some((d) => {
    const wa2 = d.workArea;
    return b.x >= wa2.x - b.width && b.x < wa2.x + wa2.width &&
           b.y >= wa2.y - b.height && b.y < wa2.y + wa2.height;
  });
  const checks = {
    visible: win.isVisible(),
    alwaysOnTop: win.isAlwaysOnTop(),
    notFocusable: !win.isFocusable(),
    inScreen,
    sizeLocked: b.width === lockedSize.width && b.height === lockedSize.height,
    bubbleBand: b.height - b.width === BUBBLE_BAND
  };
  const pass = Object.values(checks).every(Boolean);
  gateLog(`[gate] G3 ${pass ? "PASS" : "FAIL"} ${JSON.stringify(checks)}`);
}

// ---------- 移窗尺寸锁回归自检（仅 PET_SELFTEST=1 触发） ----------
function runMoveSelfTest() {
  if (!win) return;
  // 只把透明度设为 0（不 hide）：窗口仍视为可见，G3 能拿到真实状态，同时不打扰桌面
  try { win.setOpacity(0); } catch { /* 平台不支持时保持可见 */ }
  const before = win.getBounds();
  let n = 0, maxShift = 0;
  const t = setInterval(() => {
    const b = win.getBounds();
    // 往返移动：验证 600 次移窗的同时回到原位，避免自检把窗口挪出屏幕使 G3 误判
    win.setBounds({
      x: Math.round(b.x + (n < 300 ? 1 : -1)), y: Math.round(b.y),
      width: lockedSize.width, height: lockedSize.height
    });
    maxShift = Math.max(maxShift, Math.abs(b.x - before.x));
    n++;
    if (n >= 600) {
      clearInterval(t);
      const after = win.getBounds();
      const ok = after.width === lockedSize.width && after.height === lockedSize.height &&
                 after.width === before.width && after.height === before.height;
      gateLog(`[gate] MOVE-SELFTEST ${ok ? "PASS" : "FAIL"} moves=600 maxShift=${maxShift}px before=${JSON.stringify(before)} after=${JSON.stringify(after)}`);
      runG3Check();
      setTimeout(() => app.quit(), 120);
    }
  }, 2);
}

// ---------- 窗口 ----------
function createWindow() {
  const display = screen.getPrimaryDisplay();
  const size = platform.windowSize(display, BASE_SIZE);
  const saved = loadState();
  const wa = display.workArea;

  // 角色区保持正方形；窗口额外在顶部加 BUBBLE_BAND 给气泡，角色本身不缩小。
  // 持久化里若有异常尺寸（旧版漂移遗留）在此钳回合理范围。
  const s0 = clampSize(saved.width, size);
  lockedSize = { width: s0, height: s0 + BUBBLE_BAND };

  // 计算初始位置；若保存位置不在任何显示器内（如显示器已断开），回退到主屏右下角
  const maxY = wa.y + wa.height - lockedSize.height - 8;
  let x = Number.isFinite(saved.x) ? saved.x : wa.x + wa.width - s0 - 40;
  // 旧版存的是「正方形窗口」顶部 y；窗口加高后照旧复用会把角色脚底推到屏幕外，
  // 因此按新高度把 y 钳回工作区（只钳一次，不会逐次累积上移）。
  let y = Number.isFinite(saved.y) ? Math.min(saved.y, maxY) : maxY - 12;
  const onAnyScreen = screen.getAllDisplays().some((d) => {
    const a = d.workArea;
    return x >= a.x - s0 && x < a.x + a.width &&
           y >= a.y - lockedSize.height && y < a.y + a.height;
  });
  if (!onAnyScreen) { x = wa.x + wa.width - s0 - 40; y = maxY - 12; }

  win = new BrowserWindow({
    width: lockedSize.width,
    height: lockedSize.height,
    x,
    y,
    transparent: true,
    frame: false,
    hasShadow: false,
    resizable: false,
    skipTaskbar: true,
    alwaysOnTop: settings.alwaysOnTop,
    focusable: false, // 不抢焦点：绝不打断打字办公
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      backgroundThrottling: false
    }
  });

  win.setAlwaysOnTop(settings.alwaysOnTop, platform.alwaysOnTopLevel());
  win.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true });
  win.setIgnoreMouseEvents(false);
  win.loadFile(path.join(__dirname, "renderer", "index.html"));

  // G3 自检：普通启动 ready 后 2s；自检模式改由 selftest 结束后触发，避免读到移窗中间态
  if (process.env.PET_SELFTEST !== "1") setTimeout(runG3Check, 2000);

  win.on("moved", () => {
    const b = win.getBounds();
    saveState({ x: b.x, y: b.y }); // 只存位置，绝不把运行时尺寸写回持久化
  });
  // 兜底：任何非预期 resize（系统 DPI 换算误差等）立即钳回锁定尺寸，防止角色被拉大
  win.on("resize", () => {
    if (correctingSize || !lockedSize.width) return;
    const b = win.getBounds();
    if (b.width !== lockedSize.width || b.height !== lockedSize.height) {
      correctingSize = true;
      win.setBounds({ x: b.x, y: b.y, width: lockedSize.width, height: lockedSize.height });
      setTimeout(() => { correctingSize = false; }, 50);
    }
  });
}

// ---------- 托盘 ----------
function makeTrayIcon() {
  // 无图标资源时，用代码内嵌的橙色圆点作为托盘图标（避免 macOS 缺失图标崩溃）
  const { nativeImage } = require("electron");
  const S = 32, cx = (S - 1) / 2, cy = (S - 1) / 2, r = 13;
  const buf = Buffer.alloc(S * S * 4);
  for (let y = 0; y < S; y++) {
    for (let x = 0; x < S; x++) {
      const i = (y * S + x) * 4;
      const dx = x - cx, dy = y - cy;
      if (dx * dx + dy * dy <= r * r) {
        buf[i] = 255; buf[i + 1] = 150; buf[i + 2] = 40; buf[i + 3] = 255;
      }
    }
  }
  return nativeImage.createFromBuffer(buf, { width: S, height: S });
}

function createTray() {
  const icon = platform.trayIconPath() || makeTrayIcon();
  tray = new Tray(icon);
  const menu = Menu.buildFromTemplate([
    { label: "显示 / 隐藏", click: () => (win.isVisible() ? win.hide() : win.show()) },
    { type: "separator" },
    {
      label: "窗口置顶",
      type: "checkbox",
      checked: settings.alwaysOnTop,
      click: (item) => {
        settings.alwaysOnTop = item.checked;
        win.setAlwaysOnTop(item.checked, platform.alwaysOnTopLevel());
        saveState({ settings });
      }
    },
    {
      label: "鼠标穿透（空白处）",
      type: "checkbox",
      checked: settings.clickThrough,
      click: (item) => {
        settings.clickThrough = item.checked;
        win.webContents.send("click-through-changed", item.checked);
        saveState({ settings });
      }
    },
    {
      label: "安静模式（少冒泡）",
      type: "checkbox",
      checked: settings.quiet,
      click: (item) => {
        settings.quiet = item.checked;
        win.webContents.send("quiet-changed", item.checked);
        saveState({ settings });
      }
    },
    {
      label: "动效（呼吸 / 走路 / 打招呼）",
      type: "checkbox",
      checked: settings.animate,
      click: (item) => {
        settings.animate = item.checked;
        win.webContents.send("animate-changed", item.checked);
        saveState({ settings });
      }
    },
    {
      label: "开机自启",
      type: "checkbox",
      checked: platform.getLoginItem(),
      click: (item) => platform.setLoginItem(item.checked)
    },
    { type: "separator" },
    {
      label: "尺寸",
      submenu: [
        { label: "小", click: () => resizePet(96) },
        { label: "中", click: () => resizePet(128) },
        { label: "大", click: () => resizePet(160) }
      ]
    },
    { type: "separator" },
    { label: "退出", click: () => app.quit() }
  ]);
  tray.setToolTip("星瑶 · 桌宠");
  tray.setContextMenu(menu);
}

// 右键宠物本体弹出的快捷菜单（由渲染端坐标触发）
function showPetContextMenu(x, y) {
  if (!win) return;
  const menu = Menu.buildFromTemplate([
    { label: "隐藏 / 显示", click: () => (win.isVisible() ? win.hide() : win.show()) },
    { label: "睡觉 / 唤醒", click: () => win.webContents.send("pet:sleep") },
    {
      label: "动效（关掉后完全静止）",
      type: "checkbox",
      checked: settings.animate,
      click: (item) => {
        settings.animate = item.checked;
        win.webContents.send("animate-changed", item.checked);
        saveState({ settings });
      }
    },
    { type: "separator" },
    { label: "退出", click: () => app.quit() }
  ]);
  menu.popup({ x, y });
}

// ---------- IPC ----------
function registerIpc() {
  ipcMain.on("renderer-ready", (_e, payload) => {
    gateReady = true;
    gateLog(`[gate] G1 PASS frames=${JSON.stringify(payload && payload.frames)}`);
  });
  ipcMain.on("renderer-error", (_e, payload) => {
    gateLog(`[gate] G1 FAIL ${JSON.stringify(payload)}`);
  });
  ipcMain.on("window:moveBy", (_e, dx, dy) => {
    if (!win || !lockedSize.width) return;
    const b = win.getBounds();
    // 只把 x/y 读回来算新位置；尺寸必须用锁定常量显式传完整矩形。
    // setBounds({x,y}) / setPosition() / 传 b.width 都会在 150% DPI 下每次让宽度 +1px。
    win.setBounds({
      x: Math.round(b.x + dx),
      y: Math.round(b.y + dy),
      width: lockedSize.width,
      height: lockedSize.height
    });
  });
  ipcMain.handle("window:getMoveBounds", () => {
    const d = screen.getPrimaryDisplay();
    return { win: win.getBounds(), work: d.workArea };
  });
  ipcMain.on("window:setSize", (_e, pt) => resizePet(pt));
  ipcMain.on("click-through:apply", (_e, on) => {
    // on: 空白处穿透；鼠标悬停在宠物本体上时渲染端会恢复可点
    if (on) {
      win.setIgnoreMouseEvents(true, { forward: true });
    } else {
      win.setIgnoreMouseEvents(false);
    }
  });
  ipcMain.on("pet:contextmenu", (_e, x, y) => showPetContextMenu(x, y));
  ipcMain.handle("settings:get", () => settings);
  ipcMain.handle("app:onboarded", () => !!loadState().onboarded);
  ipcMain.on("app:onboarded", () => saveState({ onboarded: true }));
  // G4 取证：截取宠物窗口存到 userData，返回路径（双平台通用，替代外部截图命令）
  ipcMain.handle("app:capture", async () => {
    if (!win) return null;
    try {
      const img = await win.capturePage();
      const p = path.join(app.getPath("userData"), "pet-capture.png");
      fs.writeFileSync(p, img.toPNG());
      return p;
    } catch {
      return null;
    }
  });
}

// ---------- 外部气泡通道（父进程经 stdin 驱动桌宠冒泡） ----------
// 协议：父进程往桌宠 stdin 写「一行一个 JSON」，UTF-8，形如 {"text":"内容","ms":2600}（ms 可省略）。
// 任何一行解析失败都只跳过该行，绝不抛到顶层；父进程断开时桌宠继续独立常驻。
function setupStdinBridge() {
  let buf = "";

  function handleLine(line) {
    if (!line) return;
    let msg;
    try { msg = JSON.parse(line); } catch { return; }   // 非法 JSON：忽略该行
    if (!msg || typeof msg.text !== "string") return;
    const text = msg.text.trim().slice(0, 80);          // 气泡宽度有限，过长截断
    if (!text) return;
    let ms = Number(msg.ms);
    if (!Number.isFinite(ms)) ms = 2600;                 // 缺省显示时长
    ms = Math.min(Math.max(Math.round(ms), 800), 8000);  // 钳到合理区间
    if (win && win.webContents && !win.webContents.isDestroyed()) {
      win.webContents.send("pet:bubble", text, ms);
      gateLog(`[gate] BUBBLE ${text}`);
    }
  }

  function onData(chunk) {
    buf += chunk;
    let idx;
    while ((idx = buf.indexOf("\n")) >= 0) {
      const line = buf.slice(0, idx).trim();
      buf = buf.slice(idx + 1);
      if (!line) continue;                                // 空行跳过
      try { handleLine(line); } catch { /* 单行出错不影响后续 */ }
    }
  }

  function attach(stream) {
    stream.setEncoding("utf8");
    stream.on("data", onData);
    // 父进程断开后 EOF/报错只记日志，桌宠继续独立运行
    stream.on("error", (e) => {
      gateLog(`[gate] STDIN-ERROR ${e && e.message ? e.message : e}`);
    });
    if (typeof stream.resume === "function") stream.resume();
  }

  try {
    // Electron 主进程的 process.stdin 在 Windows 上一挂上就 EOF（Chromium 收掉了这个流），
    // 但 fd 0 仍连着父进程的管道：用 net.Socket 包一层才能真正收到数据。
    // 先试 fd 0，失败再退回 process.stdin（非 Electron 环境 / 有真实 TTY 时）。
    let attached = false;
    try {
      const net = require("net");
      attach(new net.Socket({ fd: 0, readable: true, writable: false }));
      attached = true;
    } catch { /* 继续尝试 process.stdin */ }
    if (!attached) {
      if (!process.stdin) return;
      attach(process.stdin);
    }
  } catch { /* 没有 stdin（用户从终端手动启动）时静默降级 */ }
}

// ---------- 应用生命周期 ----------
const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  app.quit();
} else {
  app.whenReady().then(() => {
    // 恢复上次的设置（置顶/穿透/安静），让 config.json 的 persist.settings 名副其实
    const savedSettings = loadState().settings;
    if (savedSettings && typeof savedSettings === "object") {
      settings = { ...settings, ...savedSettings };
    }
    gateLog(`[gate] app starting ${app.getVersion()}`);
    registerIpc();
    createWindow();
    setupStdinBridge();
    createTray();
    if (process.env.PET_SELFTEST === "1") setTimeout(runMoveSelfTest, 300);
  });

  app.on("window-all-closed", () => {
    // 桌宠常驻：关窗不退出，从托盘退出
  });
}

const { app, BrowserWindow, Tray, Menu, ipcMain, screen, protocol, net, shell } = require("electron");
const path = require("path");
const fs = require("fs");
const { pathToFileURL } = require("url");
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

// 素材自定义协议必须在 app ready 之前登记特权，晚于 ready 登记不生效。
// 皮肤素材在用户目录，用 file:// 加载时图片与 index.html 不同源，canvas getImageData
// （点击命中检测 / 空白穿透）会被判成跨源污染直接抛 SecurityError；统一成 pet-asset://
// 单一源后既躲开画布污染，也能把「素材根」钉死，顺带挡住 ../ 路径穿越。
protocol.registerSchemesAsPrivileged([
  { scheme: "pet-asset", privileges: { standard: true, secure: true, supportFetchAPI: true, corsEnabled: true, stream: true } }
]);

const SKIN_ID = (process.env.YCODE_PET_SKIN || "").trim() || "xingyao";
const SKIN_NAME = (process.env.YCODE_PET_NAME || "").trim() || "星瑶";
const BUILTIN_ASSETS = path.join(__dirname, "..", "assets");
const SKIN_ASSETS = (process.env.YCODE_PET_ASSETS || "").trim() || BUILTIN_ASSETS;
const BUILTIN_CONFIG = path.join(__dirname, "renderer", "config.json");
const SKIN_CONFIG = (process.env.YCODE_PET_CONFIG || "").trim() || BUILTIN_CONFIG;
// 渲染端拿到的两个素材根：自定义协议的统一前缀，由下面的 registerPetAssetProtocol 解析。
// skin 是当前宠物包，builtin 是随包内置素材——渲染层某个状态在宠物包里一帧都没有时，
// 才整组换用 builtin，绝不逐帧混用。
const ASSET_SCHEME_BASE = "pet-asset://skin";
const BUILTIN_SCHEME_BASE = "pet-asset://builtin";

function readJsonFile(file) {
  try {
    return JSON.parse(fs.readFileSync(file, "utf8"));
  } catch {
    return null;
  }
}

// 宠物包只需写自己和内置不一样的字段，缺省沿用内置行为。
// 否则每个皮肤包都得复制一整份 config.json，内置默认值以后改了也会被旧包覆盖回去。
function loadSkinConfig() {
  const base = readJsonFile(BUILTIN_CONFIG) || {};
  gateLog(`[gate] CONFIG builtin=${BUILTIN_CONFIG} baseKeys=${Object.keys(base).join(",")}`);
  if (!SKIN_CONFIG || path.resolve(SKIN_CONFIG) === path.resolve(BUILTIN_CONFIG)) return base;
  const skin = readJsonFile(SKIN_CONFIG);
  if (!skin || typeof skin !== "object") return base;

  const merged = { ...base, ...skin };
  for (const key of ["canvas", "motion", "bubble", "behaviors"]) {
    const v = skin[key];
    if (v && typeof v === "object") merged[key] = { ...(base[key] || {}), ...v };
  }
  if (skin.animations && typeof skin.animations === "object") {
    const anim = { ...(base.animations || {}) };
    for (const [st, spec] of Object.entries(skin.animations)) {
      if (spec && typeof spec === "object") anim[st] = spec;
    }
    merged.animations = anim;
  }
  return merged;
}

function registerPetAssetProtocol() {
  protocol.handle("pet-asset", async (request) => {
    let parsed = null;
    let rel = "";
    try {
      parsed = new URL(request.url);
      rel = decodeURIComponent(parsed.pathname).replace(/^\/+/, "");
    } catch {
      return new Response("", { status: 400 });
    }
    const root = parsed.host === "builtin" ? BUILTIN_ASSETS : parsed.host === "skin" ? SKIN_ASSETS : "";
    if (!root) return new Response("", { status: 404 });
    const rootAbs = path.resolve(root);
    const fileAbs = path.resolve(path.join(root, rel));
    // 前缀校验必须带上分隔符，否则根目录的兄弟目录（如 /x/pet-evil）会被误判成子路径
    if (fileAbs !== rootAbs && !fileAbs.startsWith(rootAbs + path.sep)) {
      return new Response("", { status: 404 });
    }
    let stat = null;
    try { stat = fs.statSync(fileAbs); } catch { stat = null; }
    if (!stat || !stat.isFile()) return new Response("", { status: 404 });
    try {
      const res = await net.fetch(pathToFileURL(fileAbs).toString());
      const headers = new Headers(res.headers);
      headers.set("Access-Control-Allow-Origin", "*");
      return new Response(res.body, { status: res.status, headers });
    } catch {
      return new Response("", { status: 500 });
    }
  });
}

const BASE_SIZE = 128;
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

function gateLog(line) {
  try {
    fs.appendFileSync(path.join(app.getPath("userData"), GATE_LOG), line + "\n", "utf8");
  } catch { /* 日志不可写也不影响运行 */ }
  console.log(line);
}

function resizePet(pt) {
  if (!win) return;
  const d = screen.getPrimaryDisplay();
  const s = clampSize(platform.windowSize(d, pt), BASE_SIZE);
  const b = win.getBounds();
  const bottom = b.y + b.height;
  lockedSize = { width: s, height: s + BUBBLE_BAND };
  win.setBounds({
    x: b.x, y: bottom - lockedSize.height,
    width: lockedSize.width, height: lockedSize.height
  });
  saveState({ width: s });        // 只存正方形边长，避免把 s+BUBBLE_BAND 当边长读回
}

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

function runMoveSelfTest() {
  if (!win) return;
  try { win.setOpacity(0); } catch { }
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

function createWindow() {
  const display = screen.getPrimaryDisplay();
  const size = platform.windowSize(display, BASE_SIZE);
  const saved = loadState();
  const wa = display.workArea;

  const s0 = clampSize(saved.width, size);
  lockedSize = { width: s0, height: s0 + BUBBLE_BAND };

  // 计算初始位置；若保存位置不在任何显示器内（如显示器已断开），回退到主屏右下角
  const maxY = wa.y + wa.height - lockedSize.height - 8;
  let x = Number.isFinite(saved.x) ? saved.x : wa.x + wa.width - s0 - 40;
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
    backgroundColor: "#00000000",
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
  win.setBackgroundColor("#00000000");
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
  // 显示名跟着当前宠物包走，避免多皮肤时托盘永远写着「星瑶」
  tray.setToolTip(`${SKIN_NAME} · 桌宠`);
  tray.setContextMenu(menu);
}

function sendEmote(kind) {
  if (!win || win.webContents.isDestroyed()) return;
  win.webContents.send("pet:emote", kind);
}

function dockToBottomRight() {
  if (!win || !lockedSize.width) return;
  const wa = screen.getPrimaryDisplay().workArea;
  const x = wa.x + wa.width - lockedSize.width - 40;
  const y = wa.y + wa.height - lockedSize.height - 8;
  win.setBounds({ x, y, width: lockedSize.width, height: lockedSize.height });
  saveState({ x, y });
}

// 打开用户宠物目录（~/.ycode/pets）：不存在就先建出来，否则 shell.openPath 会失败
function openPetsDir() {
  const dir = path.join(require("os").homedir(), ".ycode", "pets");
  try {
    fs.mkdirSync(dir, { recursive: true });
    shell.openPath(dir);
  } catch (err) {
    gateLog(`[gate] OPEN-PETS-DIR FAIL ${err && err.message ? err.message : err}`);
  }
}

function showPetContextMenu(x, y) {
  if (!win) return;
  const menu = Menu.buildFromTemplate([
    { label: `${SKIN_NAME} · 桌宠`, enabled: false },
    { type: "separator" },
    { label: "摸摸头", click: () => sendEmote("greet") },
    { label: "逗她一下", click: () => sendEmote("interact") },
    { label: "让她说句话", click: () => sendEmote("talk") },
    { label: "出来走走", click: () => sendEmote("walk") },
    {
      label: "更多动作",
      submenu: [
        { label: "伸个懒腰", click: () => sendEmote("stretch") },
        { label: "坐下休息", click: () => sendEmote("sit") },
        { label: "呵气暖手", click: () => sendEmote("warm_hands") },
        { label: "东张西望", click: () => sendEmote("look_around") },
        { label: "开心一下", click: () => sendEmote("happy") },
        { label: "害羞一下", click: () => sendEmote("shy") }
      ]
    },
    { type: "separator" },
    { label: "睡觉 / 唤醒", click: () => win.webContents.send("pet:sleep") },
    { label: "隐藏 / 显示", click: () => (win.isVisible() ? win.hide() : win.show()) },
    { type: "separator" },
    {
      label: "尺寸",
      submenu: [
        { label: "小", click: () => resizePet(96) },
        { label: "中", click: () => resizePet(128) },
        { label: "大", click: () => resizePet(160) }
      ]
    },
    { label: "回到右下角", click: () => dockToBottomRight() },
    { type: "separator" },
    {
      label: "更多设置",
      submenu: [
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
          label: "动效（关掉后完全静止）",
          type: "checkbox",
          checked: settings.animate,
          click: (item) => {
            settings.animate = item.checked;
            win.webContents.send("animate-changed", item.checked);
            saveState({ settings });
          }
        }
      ]
    },
    { type: "separator" },
    { label: "打开宠物目录", click: () => openPetsDir() },
    { label: "退出", click: () => app.quit() }
  ]);
  // 窗口很小（128×184），坐标钳在窗口内，避免渲染端给出异常值（NaN / 负值）时菜单飘走
  const size = lockedSize.width ? lockedSize : win.getBounds();
  const px = Number.isFinite(x) ? Math.min(Math.max(Math.round(x), 0), size.width) : undefined;
  const py = Number.isFinite(y) ? Math.min(Math.max(Math.round(y), 0), size.height) : undefined;
  menu.popup({ window: win, x: px, y: py });
}

function registerIpc() {
  ipcMain.on("renderer-ready", (_e, payload) => {
    gateReady = true;
    gateLog(`[gate] G1 PASS frames=${JSON.stringify(payload && payload.frames)}`);
  });
  ipcMain.on("renderer-error", (_e, payload) => {
    gateLog(`[gate] G1 FAIL ${JSON.stringify(payload)}`);
  });
  ipcMain.on("window:moveBy", (_e, dx, dy) => {
    gateLog(`[gate] MOVE-BY dx=${dx} dy=${dy} lockedSize=${lockedSize.width}x${lockedSize.height}`);
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
    if (on) {
      win.setIgnoreMouseEvents(true, { forward: true });
    } else {
      win.setIgnoreMouseEvents(false);
    }
  });
  ipcMain.on("pet:contextmenu", (_e, x, y) => showPetContextMenu(x, y));
  ipcMain.handle("pet:get-skin", () => ({
    id: SKIN_ID,
    name: SKIN_NAME,
    assetsBase: ASSET_SCHEME_BASE,
    builtinBase: BUILTIN_SCHEME_BASE,
    config: loadSkinConfig()
  }));
  ipcMain.handle("settings:get", () => settings);
  ipcMain.handle("app:onboarded", () => !!loadState().onboarded);
  ipcMain.on("app:onboarded", () => saveState({ onboarded: true }));
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
    try { msg = JSON.parse(line); } catch { return; }
    if (!msg || typeof msg !== "object") return;
    if (msg.clear === true) {
      const kinds = Array.isArray(msg.kinds)
        ? msg.kinds.map((k) => String(k || "").trim()).filter(Boolean)
        : null;
      if (win && win.webContents && !win.webContents.isDestroyed()) {
        win.webContents.send("pet:bubble-clear", kinds);
        gateLog(`[gate] BUBBLE-CLEAR ${kinds ? kinds.join(",") : "*"}`);
      }
      return;
    }
    if (typeof msg.text !== "string") return;
    const text = msg.text.trim().slice(0, 80);
    if (!text) return;
    let ms = Number(msg.ms);
    if (!Number.isFinite(ms)) ms = 2600;
    ms = Math.min(Math.max(Math.round(ms), 800), 15000);
    const meta = {};
    if (typeof msg.kind === "string" && msg.kind.trim()) meta.kind = msg.kind.trim();
    if (typeof msg.emote === "string" && msg.emote.trim()) meta.emote = msg.emote.trim();
    if (msg.sticky === true) meta.sticky = true;
    if (win && win.webContents && !win.webContents.isDestroyed()) {
      win.webContents.send("pet:bubble", text, ms, meta);
      gateLog(`[gate] BUBBLE ${meta.kind ? `[${meta.kind}] ` : ""}${text}`);
    }
  }

  function onData(chunk) {
    buf += chunk;
    let idx;
    while ((idx = buf.indexOf("\n")) >= 0) {
      const line = buf.slice(0, idx).trim();
      buf = buf.slice(idx + 1);
      if (!line) continue;
      try { handleLine(line); } catch { }
    }
  }

  function attach(stream) {
    stream.setEncoding("utf8");
    stream.on("data", onData);
    stream.on("error", (e) => {
      gateLog(`[gate] STDIN-ERROR ${e && e.message ? e.message : e}`);
    });
    if (typeof stream.resume === "function") stream.resume();
  }

  try {
    let attached = false;
    try {
      const net = require("net");
      attach(new net.Socket({ fd: 0, readable: true, writable: false }));
      attached = true;
    } catch { }
    if (!attached) {
      if (!process.stdin) return;
      attach(process.stdin);
    }
  } catch { /* 没有 stdin（用户从终端手动启动）时静默降级 */ }
}

const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  app.quit();
} else {
  app.whenReady().then(() => {
    const savedSettings = loadState().settings;
    if (savedSettings && typeof savedSettings === "object") {
      settings = { ...settings, ...savedSettings };
    }
    gateLog(`[gate] app starting ${app.getVersion()}`);
    gateLog(`[gate] SKIN id=${SKIN_ID} name=${SKIN_NAME} assets=${SKIN_ASSETS} config=${SKIN_CONFIG}`);
    registerPetAssetProtocol(); // 必须早于 createWindow（loadFile 之后渲染端就会请求素材）
    registerIpc();
    createWindow();
    setupStdinBridge();
    createTray();
    if (process.env.PET_SELFTEST === "1") setTimeout(runMoveSelfTest, 300);
  });

  app.on("window-all-closed", () => {
  });
}

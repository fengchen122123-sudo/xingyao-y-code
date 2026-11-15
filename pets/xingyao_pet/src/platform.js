// platform.js — 双平台差异适配层。主进程其他代码禁止出现 process.platform 判断。
const { app } = require("electron");
const path = require("path");
const fs = require("fs");

const IS_MAC = process.platform === "darwin";
const IS_WIN = process.platform === "win32";

function alwaysOnTopLevel() {
  // macOS: screen-saver 级盖过全屏应用；Windows 仅支持 normal/floating，用 floating
  return IS_MAC ? "screen-saver" : "floating";
}

function trayIconPath() {
  const name = IS_MAC ? "trayTemplate.png" : "tray.png";
  const p = path.join(__dirname, "..", "assets", "tray", name);
  return fs.existsSync(p) ? p : undefined; // Electron 无图标时用默认
}

function windowSize(display, basePt) {
  // Windows 高分屏按 DPI 换算，macOS Electron 用 DIP 无需处理
  if (IS_WIN) {
    const sf = display.scaleFactor || 1;
    return Math.round(basePt * Math.max(1, Math.min(sf, 3)));
  }
  return basePt;
}

function screenshotSupported() {
  // 双平台统一走主进程的 app:capture（win.capturePage），无需外部命令
  return true;
}

function screenshotCommand(outPath) {
  // 保留接口占位；实际取证由 app:capture IPC 完成（跨平台一致）
  return null;
}

function setLoginItem(enabled) {
  app.setLoginItemSettings({ openAtLogin: enabled });
}

function getLoginItem() {
  return app.getLoginItemSettings().openAtLogin;
}

module.exports = {
  IS_MAC,
  IS_WIN,
  alwaysOnTopLevel,
  trayIconPath,
  windowSize,
  screenshotSupported,
  screenshotCommand,
  setLoginItem,
  getLoginItem
};

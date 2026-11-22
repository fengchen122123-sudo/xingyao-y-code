const { app } = require("electron");
const path = require("path");
const fs = require("fs");

const IS_MAC = process.platform === "darwin";
const IS_WIN = process.platform === "win32";

function alwaysOnTopLevel() {
  return IS_MAC ? "screen-saver" : "floating";
}

function trayIconPath() {
  const name = IS_MAC ? "trayTemplate.png" : "tray.png";
  const p = path.join(__dirname, "..", "assets", "tray", name);
  return fs.existsSync(p) ? p : undefined;
}

function windowSize(display, basePt) {
  if (IS_WIN) {
    const sf = display.scaleFactor || 1;
    return Math.round(basePt * Math.max(1, Math.min(sf, 3)));
  }
  return basePt;
}

function screenshotSupported() {
  return true;
}

function screenshotCommand(outPath) {
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

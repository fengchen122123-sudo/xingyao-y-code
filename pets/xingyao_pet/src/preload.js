const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("petAPI", {
  rendererReady: (payload) => ipcRenderer.send("renderer-ready", payload),
  rendererError: (payload) => ipcRenderer.send("renderer-error", payload),
  moveBy: (dx, dy) => ipcRenderer.send("window:moveBy", dx, dy),
  getMoveBounds: () => ipcRenderer.invoke("window:getMoveBounds"),
  setSize: (pt) => ipcRenderer.send("window:setSize", pt),
  applyClickThrough: (on) => ipcRenderer.send("click-through:apply", on),
  getSettings: () => ipcRenderer.invoke("settings:get"),
  onClickThroughChanged: (cb) => ipcRenderer.on("click-through-changed", (_e, v) => cb(v)),
  getOnboarded: () => ipcRenderer.invoke("app:onboarded"),
  markOnboarded: () => ipcRenderer.send("app:onboarded"),
  onQuietChanged: (cb) => ipcRenderer.on("quiet-changed", (_e, v) => cb(v)),
  onAnimateChanged: (cb) => ipcRenderer.on("animate-changed", (_e, v) => cb(v)),
  contextMenu: (x, y) => ipcRenderer.send("pet:contextmenu", x, y),
  onPetSleep: (cb) => ipcRenderer.on("pet:sleep", () => cb()),
  getSkin: () => ipcRenderer.invoke("pet:get-skin"),
  onEmote: (cb) => ipcRenderer.on("pet:emote", (_e, kind) => cb(kind)),
  onBubble: (cb) => ipcRenderer.on("pet:bubble", (_e, text, ms, meta) => cb(text, ms, meta)),
  onBubbleClear: (cb) => ipcRenderer.on("pet:bubble-clear", (_e, kinds) => cb(kinds))
});

// dsh-desktop-shell — host-side plugin: opens the DeepSeek Harness Web GUI in
// a native WebView2 window once the web profile finishes booting, and skins
// the served page with the ycode theme (warm-neutral ZCode palette).
//
// Zero external imports on purpose: the module is installed into a profile as
// a hard-linked copy of this checkout, so any bare `@deepseek-ai/*` import
// would resolve from the checkout's own node_modules instead of the dsh
// installation. Node builtins only.
import { spawn } from "node:child_process";
import { appendFileSync, mkdirSync, readFileSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

/** Stable Cordis plugin name. */
export const name = "desktop-shell";

const SHELL_DIR = join(dirname(fileURLToPath(import.meta.url)), "..", "shell");
const WEBVIEW_SCRIPT = join(SHELL_DIR, "webview.ps1");
const THEME_CSS = join(SHELL_DIR, "theme-ycode.css");
const DSH_HOME = process.env.DSH_HOME ?? join(homedir(), ".dsh");
const LOG_DIR = join(DSH_HOME, "logs");
const LOG_FILE = join(LOG_DIR, "desktop-shell.log");

/** Append one line to the plugin log; never throws. */
function log(line) {
  const stamp = new Date().toISOString();
  try {
    mkdirSync(LOG_DIR, { recursive: true });
    appendFileSync(LOG_FILE, `${stamp} ${line}\n`);
  } catch {
    /* logging must never break the host */
  }
}

/**
 * Index.html transform: inline the ycode theme stylesheet right before
 * `</head>`. Pure html→html; no-op when the marker is missing.
 */
function makeThemeTap(css) {
  const tag = `<style id="dsh-ycode-theme">\n${css}\n</style>`;
  return (html) => {
    const at = html.indexOf("</head>");
    if (at === -1) return html;
    return html.slice(0, at) + tag + "\n" + html.slice(at);
  };
}

/**
 * Mount: wait for the whole loader tree to settle (the webserver port is only
 * final then, and a sibling boot failure must not be masked by a window
 * pointing at a dead app), register the theme tap, then open the native
 * window.
 * @param ctx - plugin context.
 * @param config - raw patch config; defaults applied here (no schema import).
 */
export function apply(ctx, config) {
  const {
    autoOpen = true,
    title = "DeepSeek Harness",
    width = 1320,
    height = 860,
    closeAction = "shutdown",
    theme = "ycode",
    themeMode = "dark",
  } = config ?? {};

  log(`apply: autoOpen=${autoOpen} closeAction=${closeAction} theme=${theme} themeMode=${themeMode} script=${WEBVIEW_SCRIPT}`);

  let spawned = false;
  let child;

  /** Apply the theme once the webserver exists. Never throws. */
  const applyTheme = () => {
    if (theme === "none") return;
    let server;
    try {
      server = ctx.get("webServer");
    } catch {
      server = undefined;
    }
    if (server === undefined || typeof server.tapIndex !== "function") {
      log("theme: webServer has no tapIndex; theme not applied");
      return;
    }
    try {
      const css = readFileSync(THEME_CSS, "utf8");
      server.tapIndex(makeThemeTap(css));
      log(`theme: ycode theme registered (${css.length} bytes)`);
    } catch (err) {
      log(`theme: failed to register: ${err.message}`);
    }
  };

  /**
   * Default the theme preference to the ycode look. ycode defaults to dark,
   * so unless the user already chose something, write `ui-theme.preference`
   * through the host settings API (atomic, comment-preserving). A later
   * in-app choice stays durable and is never overwritten.
   */
  const applyThemeMode = () => {
    if (themeMode === "keep") return;
    let settings;
    try {
      settings = ctx.get("settings");
    } catch {
      settings = undefined;
    }
    if (settings === undefined || typeof settings.describe !== "function" || typeof settings.update !== "function") {
      log("themeMode: settings service unavailable");
      return;
    }
    try {
      // `describe()` exposes the detached USER layer; `get()` would return the
      // schema default ("system") for an unstored section and look like a
      // user choice.
      const themeDesc = settings.describe().find((d) => d.ns === "ui-theme");
      const userPreference = themeDesc?.user?.preference;
      if (userPreference !== undefined) {
        log(`themeMode: user preference=${JSON.stringify(userPreference)}; leaving unchanged`);
        return;
      }
      settings.update("ui-theme", { preference: themeMode }).then(
        () => log(`themeMode: set ui-theme.preference=${themeMode}`),
        (err) => log(`themeMode: update failed: ${err?.message ?? String(err)}`),
      );
    } catch (err) {
      log(`themeMode: error: ${err?.message ?? String(err)}`);
    }
  };

  const open = () => {
    const server = ctx.get("webServer");
    if (server === undefined || server.port === undefined) {
      log("open: webServer missing or not listening; no window opened");
      ctx.logger.warn("desktop-shell: webServer service missing or not listening; no window opened");
      return;
    }
    const url = `http://127.0.0.1:${server.port}`;
    const args = [
      "-NoProfile",
      "-STA",
      "-ExecutionPolicy", "Bypass",
      "-File", WEBVIEW_SCRIPT,
      "-Url", url,
      "-Title", title,
      "-Width", String(width),
      "-Height", String(height),
      "-LogFile", LOG_FILE,
    ];
    log(`open: spawning powershell with url=${url}`);
    ctx.logger.info(`desktop-shell: opening ${url} in a native window`);
    child = spawn("powershell.exe", args, { windowsHide: true, stdio: "ignore" });
    child.on("spawn", () => {
      spawned = true;
      log("spawn: powershell process started");
    });
    child.on("error", (err) => {
      log(`spawn error: ${err.message}`);
      ctx.logger.warn(`desktop-shell: failed to launch the desktop window: ${err.message}`);
    });
    child.on("exit", (code, signal) => {
      log(`exit: code=${code} signal=${signal} spawned=${spawned}`);
      if (!spawned) return;
      ctx.logger.info("desktop-shell: window closed");
      if (closeAction === "shutdown") {
        // Ask the app to shut down through its own bounded SIGTERM path
        // (root-fiber dispose + 5s force-exit fallback, exit code 0).
        process.emit("SIGTERM");
      }
    });
  };

  // Make sure the window does not outlive the app (console Ctrl+C etc.).
  const killChild = () => {
    try {
      child?.kill();
    } catch {
      /* already gone */
    }
  };
  process.once("exit", killChild);

  const settled = ctx.get("loader")?.await();
  if (settled === undefined) {
    log("apply: loader service unavailable; applying theme and opening immediately");
    applyTheme();
    if (autoOpen) open();
  } else {
    log("apply: waiting for loader tree to settle");
    settled.then(
      () => {
        log("apply: loader settled; webServer present=" + (ctx.get("webServer") !== undefined));
        applyTheme();
        applyThemeMode();
        if (autoOpen && ctx.get("webServer") !== undefined) open();
      },
      () => {
        log("apply: loader tree failed to settle; no window opened");
      },
    );
  }
}

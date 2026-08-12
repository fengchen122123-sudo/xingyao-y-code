# Security Audit and GUI Tray Design

## Goal

Verify the 2026-08-12 external audit against the current `dev` branch, fix confirmed issues that can cross a trust boundary, and make the Windows close button hide Y-CODE to the notification area while the tray Exit action performs deterministic cleanup.

## Scope and trust boundaries

- LLM-originated MCP administration, shell commands, plan verification commands, memory text, and skill metadata are untrusted inputs.
- A plugin explicitly installed in a local plugin directory is trusted executable code by product design; the loader is not a sandbox.
- The embedded terminal and session export are explicit user actions. They still receive safe defaults, but are not treated as unauthenticated remote entry points.
- Command pattern checks are defense in depth. Permission confirmation remains the primary boundary; no regex list is described as a complete shell sandbox.

## Security changes

1. Require permission for all MCP configuration mutations; validate executable form and redact secrets in environment values, arguments, URLs, and surfaced errors.
2. Apply the existing catastrophic-command policy consistently to foreground/background/verification execution and cover documented bypass variants. Add `cmd /k` to inline-execution denial.
3. Reject archive traversal, safely quote PowerShell/Windows command-line arguments, reject PATH separator injection, atomically seed user files, and make uninstaller self-removal non-recursive.
4. Close MCP connections and its event loop without allowing a late reconnect. Synchronize shared token/vision caches.
5. Bound and clearly delimit memory/catalog data when inserted into system context. Reject skill files whose resolved path leaves the configured skill root.
6. Tighten the development-server identity check and local-path bridge APIs. Generalize UI error redaction.

## GUI lifecycle

- On Windows, `window.events.closing` returns `False` after calling `window.hide()`, so the native close is cancelled and the process remains represented by a tray icon.
- The tray menu contains `显示主窗口` and `退出`. Double-click/default activation shows the window.
- Exit is idempotent: mark exit requested, stop the tray loop, cancel active turns and permission timers, terminate the embedded terminal process tree, cancel owned subagents/plan verification, close MCP, checkpoint/close the GUI database, then destroy the pywebview window.
- If tray startup fails, the close button is allowed to close normally. The app must never become an invisible process without a usable tray icon.
- Non-Windows platforms retain normal close behavior.

## Verification

- Unit tests cover every new pure policy and lifecycle branch, including close-to-tray cancellation, tray-start failure, idempotent exit, archive traversal, quoting, redaction, permissions, command policy, and cleanup.
- Existing Python tests and frontend tests run after focused tests.
- Python compile checks and the Vue production build validate packaging-facing imports and types.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11+-blue.svg)](https://www.python.org/)
[![Version 1.4.0](https://img.shields.io/badge/Version-1.4.0-green.svg)](core/version.py)

# Xingyao (Y-code)

English | [中文](README.md)

> A desktop + terminal AI coding assistant built in China, codenamed "Xingyao". It ships as a GUI window and a TUI console, with multi-model switching, subagent orchestration, plan and workflow execution, long-term memory, MCP extension, pluggable skills and plugins, scheduled automation tasks, a desktop pet, and LAN remote access from your phone.

> **Before you start**: This project is an AI coding assistant that runs on your local machine. It does not provide model services itself — you configure your own models and the software talks to them directly. AI-generated content may be inaccurate and requires your own review and verification. The software can read and write files and execute commands, so please read the "Disclaimer" section below before use — both the installer and the first launch will prompt you to confirm.

## ✨ Highlights

### Conversation and interface

- **Terminal UI**: A Claude Code–style input box built on `prompt_toolkit`, with slash commands, Tab completion, `@file` quick attachment, and multi-line paste.
- **GUI window**: A graphical interface built on `pywebview` + Vue 3 + TypeScript, sharing the same core engine as the CLI.
- **GUI drag-and-drop attachments**: Drop a file and it appears as a placeholder on screen with its real path auto-filled. Binary files such as PDFs inject a path hint so the model can parse them with tools.
- **File tree panel**: Lazily loads subdirectories, supports manual refresh plus automatic refresh on tab activation and window focus, and restores expansion state. Clicking an image (png/jpg/gif/webp/bmp/ico/svg) opens a preview modal.
- **Customizable GUI panel widths**: Drag the divider between the sidebar and right panel to resize (double-click to reset). Widths persist across restarts.
- **9 terminal themes**: cyan / matrix / nord / cyberpunk / dracula / lava / sunset / amber / white, switchable with `/theme`.
- **Terminal Markdown rendering**: Syntax-highlighted code blocks, tables, lists, and other rich text.
- **Thinking stream display**: Shows the model's reasoning in real time (parses `<thinking>` tags with sentence-boundary buffering).
- **Minimal interface**: Frameless window with unified design tokens — breadcrumb in the session header, workspace chip, tool-execution rail rows, and single-column row lists on settings subpages. Window controls and all icons are vector strokes.
- **Turn metrics**: Live output speed while a turn runs; once it finishes, the whole-task wall-clock duration is shown (stamped onto that turn and kept for the next one), with a tooltip separating task duration from model time.
- **Document tab in the right panel**: Preview PDF / Word / Excel / PPT in place, annotate them (highlight, box, arrow, sticky note, etc.), cover and rewrite with white blocks, delete / rotate / insert pages, and save the result as a new PDF.
- **Novel workspace**: The right-panel "Novel" tab tracks each book — open a chapter to read or revise it, with foreshadowing / characters / volume outlines collected in setting tables, plus word-count trends, a read-only health check, and full-book export.
- **Desktop pet**: A small always-on-top pet window that comments on task progress; custom pet packs can be swapped from the settings page (takes effect after restarting the pet), and idle chatter thresholds and model are configurable.

### Models and tools

- **Multi-model management**: `/models` opens a full-screen model selector supporting OpenAI / Anthropic / DeepSeek / StepFun and other protocols, unified through a self-built HTTPX engine (`core/llm_native_transport.py`). Preset providers only carry provider info; models are picked from a live "fetch available models" list — endpoint auto-assembly, alternate spelling on 404, multi-key retry, and context window / max output / vision extracted from each vendor's fields (with a local spec table as fallback).
- **Secondary model support**: Configure a secondary model to handle vision recognition, memory compression, and subagent calls, splitting work between primary and secondary.
- **94 default-registered tools** (`computer_action` is added when computer control is enabled, plus 5 dedicated tools from the iMOM locator plugin): File read/write with atomic batch edits, cross-file code search plus symbol outlines and references, command execution and background tasks, web fetching and free news search, image and video generation, weather, plan orchestration, subagent dispatch and secretary cross-project search, session / model / skill / plugin / hook management, office document read/write, the novel writing toolchain, and computer control plus Windows system tools.
- **Free web search**: 360 Search first, Bing RSS as fallback; no third-party package dependency (timeouts slow it down but never disable it).
- **Tool result caching**: Identical searches and reads from read-only tools hit an in-process cache, saving tokens and time.

### Core harness

- **Fixed main-agent kernel**: Strict state machines (8 turn states / 10 step states) with explicit failure on illegal transitions. The kernel is not pluggable and cannot be swapped dynamically.
- **SQLite journal**: Owner leases plus heartbeats prevent multi-process conflicts. Event sequences are allocated monotonically, and messages and events commit atomically in the same transaction.
- **Crash recovery**: Incomplete turns are only marked `interrupted` and are never automatically replayed — no model requests, commands, or file writes are re-executed.
- **Request fingerprinting**: An immutable fingerprint is generated for the final provider request, paired with byte-golden regression tests to prevent cache drift.
- **Tool pipeline**: Same-signature repeat-call suppression (RepeatCallGuard) and batched concurrent execution of adjacent read-only tools.
- **Resume decisions**: Interrupted turns resume from a decided step according to a decision table; tool events are written to the journal as redacted facts (replay markers only, no new state transitions).
- **Context caching**: A stable prefix (everything invariant across turns folded into the system prompt) plus four-level watermarks (snip / fold / summarize). Insert-mode compression reuses the main request bytes, so cache hit rate does not collapse as the conversation grows.

### Advanced orchestration

- **Subagent system**: 5 built-in types (explore / implement / test / review / general) plus custom subagents, with dependency-aware parallel scheduling and nesting up to 3 levels deep.
- **Plan execution mode**: For multi-step tasks (more than 3 subtasks after decomposition), a `plan_write` plan is created first and subtasks are dispatched along the dependency graph, with pause / resume / retry / cancel support.
- **Background task management**: Long-running commands execute in the background. `/tasks` shows status, `/tasks tail` streams logs, `/tasks kill` terminates.
- **Permission system**: Five modes (confirm before changes / accept_edits for automatic edits / auto_decision for JEV-assisted approval / plan mode / full access), with a sandbox controlling working-directory access boundaries.
- **Scheduled automation tasks**: Bind a prompt to interval / daily / weekly / startup / one-off triggers; the task starts a full agent turn in the chosen session on schedule. Choose a fresh session each run or reuse a fixed one, missed runs catch up by default, and unattended permission is limited to confirm / accept-edits.
- **Workflow orchestration**: A `.dwf.ts` script declares phases with subagents, commands, and reports; saving it also generates a skill of the same name, and it runs via `/workflow` or `workflow_run` with live step events.
- **Secretary dispatch ledger**: Every dispatch is recorded (queued / running / done / failed / cancelled / dependency unmet), the result summary is written back to the originating session, and a batch can hold up to 8 tasks with intra-batch dependencies.
- **Task decision hooks**: Control points (loop stop-loss, subagent acceptance, etc.) can call the JEV decision model with per-scene toggles, configurable thresholds, and fail-open fallback.

### Memory and context

- **SQLite session memory**: Switch between multiple sessions; undo never rewrites history, so conversations stay intact.
- **Per-turn undo**: Controlled file modifications in each turn are snapshotted persistently. `/undo` runs a full pre-check before a one-click revert — zero writes on conflict, recoverable after crashes — and the model learns through context that the file results are invalid.
- **Long-term memory system**: Four memory types (user / feedback / project / reference) plus daily logs and a Dream consolidation thread. Supports "remember XXX" instructions.
- **Intelligent memory compression**: Four-level watermarks — at 60% of the window tool results are snipped / folded, at 80% elided, at 95% a model summary runs. After 240s idle a local watermark pass and pre-compression also run; trigger decisions use the previous turn's real API usage as the baseline.
- **Session export**: `/share` exports to Markdown / HTML / JSON in one command.
- **Project profile**: Records tech stack / architecture decisions / hard constraints / pitfalls / preferences per project directory and injects the relevant sections automatically each turn; writes only touch Xingyao's own memory directory.
- **Self-evolution**: Learns quietly after each turn to build a user profile and self model, viewable or switchable off in settings.
- **Summary recall**: Each compression archives a generation; `recall_history` can pull back the original messages behind a summary boundary, so details never have to be guessed.

### Extension ecosystem

- **Skill system (Skills)**: Scans `SKILL.md` files and injects them into the system prompt, supporting both project-level and user-level skills.
- **Plugin system (Plugins)**: In-process plugins extend tool capabilities with zero core intrusion; a failing plugin never blocks startup. Ships with 6 built-in plugins and supports hot-loading of user-level plugins.
- **MCP client**: A full-screen CLI configurator with 10+ preset templates (Feishu / Amap / Bilibili / SQLite / Postgres, etc.). The AI can configure MCP itself.
- **Git worktree**: `/worktree` manages parallel multi-branch workspaces.
- **Git workbench**: A GUI "Source Control" tab plus the CLI `/git` command (status / diff / stage / commit / branch / sync / stash / history / conflicts), with automatic refresh on external changes, repository trust confirmation, and double confirmation for high-risk operations. AI-initiated Git operations in conversations still follow the normal permission flow.
- **Deletable skills and plugins**: A trash button at the end of each row deletes the directory after double confirmation. Bundled sources and built-in plugins are refused, and symlinks or out-of-root paths are rejected (the phone remote API is blocked from these actions too).
- **Bundled skills and plugins**: 15 skill packs ship in the repo (Baoyu-series WeChat / Xiaohongshu illustration, prompt expansion and optimization, WeChat layout, explainer video, office documents, plugin authoring) along with 6 plugins (Hunyuan image, TokenRhythm image / video, office documents, novel workspace, Windows system tools).
- **GUI theme system**: Built-in skins plus custom themes — copy / rename / import-export ZIP / read image assets inside a theme.
- **Phone remote access**: A relay service connects desktop sessions over LAN or the internet; a phone browser opens the full desktop interface (device registration with its own token), and event broadcasting is asynchronous so a silent phone never blocks the desktop.

## 🚀 Quick Start

### 1. Clone the repository

```bash
git clone https://github.com/fengchen122123-sudo/xingyao-y-code.git
cd xingyao-y-code
```

### 2. Create a virtual environment and install dependencies

```bash
python -m venv venv

# Windows
venv\Scripts\activate
# macOS / Linux
source venv/bin/activate

pip install -e .
```

> This installs all runtime dependencies: `rich`, `prompt_toolkit`, `pyfiglet`, `ddgs`, `mcp`, `psutil`, `pillow`.

### 3. Configure your API key

**⚠️ Security note: real keys are never committed to the repository.**

Recommended approach: after launching, use the GUI model page's "Add provider" (or CLI `/models`) to pick a provider from the preset catalog and enter your API key — no manual file editing required.

You can also copy the example config and edit it manually:

```bash
cp config.example.json config.json
```

Edit `config.json` with your real API key:

```json
{
  "active_model_id": "model_1",
  "providers": [
    {
      "id": "provider_1",
      "name": "Agnes AI",
      "base_url": "https://apihub.agnes-ai.cn/v1",
      "api_key": "sk-your-real-api-key-here",
      "protocol": "openai",
      "models": [
        {
          "id": "model_1",
          "name": "agnes-3.0-flash",
          "litellm_model": "openai/agnes-3.0-flash",
          "context_window": 512000,
          "supports_vision": true,
          "supports_function_calling": true,
          "max_tokens": 65535
        },
        {
          "id": "model_2",
          "name": "agnes-2.5-flash",
          "litellm_model": "openai/agnes-2.5-flash",
          "context_window": 128000,
          "supports_vision": true
        }
      ]
    }
  ]
}
```

### 4. (Optional) Integrate Feishu: talk to Xingyao inside Feishu

Once configured, a single message to the bot in Feishu drives a full task turn. Completion cards, permission approval cards, question cards, a pending-decision inbox, stuck-task reminders, and the command set all work end-to-end inside Feishu.

**Feishu side (one-time, three steps)**

1. Create an **enterprise self-built app** on the [Feishu Open Platform](https://open.feishu.cn/) and note the App ID and App Secret.
2. Grant the app permissions: `im:message` (read messages) and `im:message:send_as_bot` (send messages). If you need to @ the bot in group chats, also add `im:message.group_at_msg`.
3. Under "Events and Callbacks", switch the subscription mode to **receive events via long connection**, then **add both blocks**:
   - **Event subscription**: `im.message.receive_v1` (receive messages)
   - **Callback / card callback**: `card.action.trigger` (card button callbacks)

   > The second block is the one people most often miss. With only event subscriptions configured, messages arrive and the bot replies, but buttons on cards (approve / reject / panel / pagination) do nothing when clicked. Remember to **republish the app version** after changing subscriptions.

> The long connection is initiated by Xingyao connecting out to Feishu. **No public IP, no tunneling, and no callback URL or verification token required.**

**Xingyao side**: Open the GUI → **Settings → Feishu integration**, enter the App ID / App Secret, and optionally fill in "Approval allowlist" (a list of open_ids) and "Notify chat ID". Click "Test connection" to validate, then save.

- **The approval allowlist** determines who can click "Approve / Reject" in Feishu. When left empty, **approvals are disabled on the Feishu side** (cards become reminders only and still require handling on the desktop) — this is an intentional fail-closed design.
  **Don't know your open_id?** Send `/whoami` to the bot in Feishu and it replies with your open_id; copy that into the allowlist (open_ids are scoped per app, so uses the value Xingyao sees).
- The settings page includes a **six-step Feishu setup guide** and a **configuration checklist**. If the connection fails, follow the troubleshooting order in the guide first (whether the event subscription uses long connection, whether the app is published, whether permissions are granted).
- You can also edit `config.json` manually: `feishu_enabled` / `feishu_app_id` / `feishu_app_secret` / `feishu_approvers` / `feishu_notify_chat_id`. Filling secrets in the UI is recommended (they are encrypted at rest).
- Disabled by default; when off or unconfigured, Xingyao behaves exactly as before integration.
- Send `/help` in Feishu to see all commands (`/panel` `/status` `/new` `/list` `/switch` `/model` `/inbox` `/stop`).

### 5. Launch Xingyao

```bash
# CLI console (recommended)
python main.py

# Or the installed command
ycode
```

### 6. (Optional) Launch the GUI

GUI mode is based on `pywebview` + a Vue 3 frontend. The built frontend output (`frontend/dist/`) is not committed to the repository, so build it first:

```bash
pip install pywebview

cd frontend
npm install
npm run build
cd ..

python gui_main.py
```

> For frontend development, you can instead run `npm run dev` inside `frontend/` (Vite dev server on port 5173); `gui_main.py` prefers it when available. If neither is available the GUI cannot start; CLI mode is unaffected.

### 7. (Optional) Build the Windows installer

```bash
# Full build (GUI main installer + optional CLI components)
venv\Scripts\python build_release.py

# CLI artifacts only
venv\Scripts\python build_release.py --cli-only

# Skip the frontend build (reuse existing frontend/dist)
venv\Scripts\python build_release.py --skip-frontend

# Force a frontend rebuild
venv\Scripts\python build_release.py --rebuild-frontend

# Rebuild only the two install wizards (reuses existing payloads and uninstaller, ~2 min)
venv\Scripts\python build_release.py --setup-only
```

Artifacts land in `dist/`: `Y-code.exe` (GUI), `ycode.exe` (CLI), `ycode_setup_v{version}.exe` (main installer, version number included automatically), and `ycode_uninstall.exe` (uninstall wizard). See [docs/BUILD.md](docs/BUILD.md) for details.

## 📦 Install and Uninstall (Windows)

The release artifact is a graphical wizard in classic Windows installer style (NSIS MUI2), with a white header banner and Back / Next / Cancel buttons at the bottom.

### Installer (`ycode_setup_v{version}.exe`)

Page flow: **Welcome → Install user → Path and options → Installing → Finished**

- **Install-user selection**:

| Option | Install location | Privileges | Uninstall registry | PATH | Shortcuts |
|------|---------|------|-----------|------|---------|
| Install for me only (recommended) | `%LOCALAPPDATA%\Programs\Y-code` | No admin required | HKCU | User PATH | Personal desktop |
| Install for all users | `%ProgramFiles%\Y-code` | Admin required (auto UAC elevation restart) | HKLM | System PATH | Public desktop |

- **Optional components**: Install the CLI alongside (checked by default) and create desktop shortcuts (checked by default).
- **Reinstall detection**: Detects an existing installation, reuses its scope and path, locks the relevant options, and offers a overwrite install.
- **Instant launch**: The core program package is embedded as a single-file zip payload, so the window appears in seconds after a double-click (the old scattered-file approach took 20–95 seconds).

### Uninstaller (`ycode_uninstall.exe`, released into the install directory with the installer)

A graphical wizard in the same classic style: **Welcome → Uninstall options → Uninstalling → Finished**

- Cleans desktop shortcuts (trying GUI/CLI and legacy TUI names), PATH, and registry uninstall entries.
- Adapts to the install scope: a machine-wide install cleans HKLM entries, the system PATH, and the public desktop (auto UAC elevation when not running as admin).
- Optionally deletes the `~/.ycode` user configuration and session history (kept by default so a reinstall can resume).
- The wizard self-destructs on exit, clearing leftovers in the install directory.

You can also uninstall from "Windows Settings → Apps → Installed apps → Y-code".

## ⌨️ Slash Commands (CLI)

Type these in the CLI input box (Tab completion supported). The desktop GUI only exposes the 13 commands that buttons cannot replace; see "Desktop Slash Commands" below.

### Sessions and history

| Command | Description |
|------|------|
| `/help` | Show help for all commands |
| `/session` | Open the full-screen session manager (Enter to switch, Del to delete, R/F2 to rename) |
| `/session all` | List historical sessions across all directories |
| `/session rename <new_name>` | Rename the current session |
| `/session delete <name>` | Permanently delete the specified session |
| `/new` | Create and switch to a brand-new session |
| `/undo` | Per-turn undo of file modifications (`list` / `preview` / `status` / `recover <operation_id>`; defaults to confirm N) |
| `/checkpoint` | Checkpoint management (`list` to view, `restore <turn_id>` for a full-turn rollback) |
| `/share [md\|html\|json]` | Export the current session (md by default) |

### Models and appearance

| Command | Description |
|------|------|
| `/models` | Open the full-screen model manager |
| `/effort` | Switch the current model's reasoning effort (low / high / max / default) |
| `/usage [date [end_date]]` | Token usage statistics (per model: today / last 7 days / last 30 days; accepts a date or range) |
| `/theme [theme_name]` | List or switch terminal color themes (9 available) |

### Tools and extensions

| Command | Description |
|------|------|
| `/skills` | Open the skill selector (Space to toggle) |
| `/skills list` | List all skills and their enabled state in the terminal |
| `/skills enable <short_id>` | Enable the specified skill |
| `/skills disable <short_id>` | Disable the specified skill |
| `/<short_id> [prompt]` | Explicitly force a specific skill to run a task |
| `/plugins` | List installed in-process plugins |
| `/plugins enable\|disable <name>` / `reload` | Enable / disable a plugin, or rescan the plugin directory |
| `/mcp` | MCP service management (open / status / tools / enable / disable / reconnect / edit) |

### Tasks and plans

| Command | Description |
|------|------|
| `/tasks` | List background tasks |
| `/tasks tail <ID>` | Stream a task's log output |
| `/tasks kill <ID>` | Terminate the specified background task |
| `/plan` | Show the current task plan and per-item status |
| `/plan pause` / `resume` / `cancel` | Pause / resume / cancel the plan |
| `/plan retry <ID>` | Retry the specified plan item |
| `/worktree [list\|create\|remove\|prune]` | Git worktree management |

### Memory and diagrams

| Command | Description |
|------|------|
| `/remember` | Manually append a rule / preference / fact to the memory log |
| `/dream` | Trigger background dream consolidation to distill the memory log |
| `/memory` | View the long-term memory index (MEMORY.md) |
| `/compress [force]` | Manually trigger session memory compression (force keeps fewer entries) |
| `/diagram open <index>\|list` | Open or list exported Mermaid diagram pages in the browser |

### System control

| Command | Description |
|------|------|
| `/permission <confirm\|accept_edits\|auto_decision\|plan\|full>` | Switch permission level |
| `/sandbox <on\|off>` | Toggle the sandbox (on = allow access outside the working directory) |
| `/turns <10-1000>` | Set the maximum model turns per task (default 100) |
| `/clear` | Clear the screen |
| `/exit` | Quit |

> Tool execution feedback defaults to a compact progress mode: the CLI and GUI merge consecutive reads and searches; commands, file modifications, subagents, and failed operations leave their own traces; ordinary tool failures are returned to the model for further analysis and never silently hidden. The GUI can expand full tool results and the corresponding file diffs.

> Enabled skills are automatically added to the completion menu as `/<skill_short_id>` commands.

### Keyboard shortcuts

| Shortcut | Function |
|--------|------|
| `Enter` | Send message |
| `Alt + Enter` | New line in the input box |
| `Ctrl + C` | Clear the input box |
| `Ctrl + D` | Quit |
| `Tab` | Command / filename completion |

## 🖥️ Desktop Slash Commands

The desktop app does not provide commands for actions that already have GUI buttons (model switching, themes, permissions, session management all go through the UI). It keeps only the 13 commands that need free-form text arguments and cannot be expressed as buttons. Typing `/` in the input box shows candidates; text starting with `/` that does not match the command table is sent as a normal message — for example, `/usr/bin explained` is not treated as a command.

| Command | Description |
|------|------|
| `/goal <goal> [--check command] [--rounds N]` | Goal mode: iterate autonomously until achieved. `/goal status` to inspect, `/goal stop` to stop, `/goal resume` to resume |
| `/agent <type> <task>` | Dispatch a subagent to run a task |
| `/skill <name> [requirements]` | Look up an enabled skill and inject its body into the current turn |
| `/remember <content>` | Append a rule or preference to long-term memory |
| `/forget <keyword>` | Count matching lines in today's log and MEMORY.md and show samples; after confirmation run `/forget --yes <keyword>` to actually delete, scoped to those two places only |
| `/fix <error>` | Locate the root cause and fix it, then run verification |
| `/review [scope]` | Code review |
| `/init` | Scan the project and generate AGENTS.md |
| `/test [target]` | Run tests, fix failures, rerun |
| `/explain <file:line>` | Explain the specified code |
| `/compact [retain instructions]` | Trigger background session compression; progress is echoed as a notice card |
| `/commit [message]` | Stage all changes and show pending files; after confirmation run `/commit --yes [message]` to commit. Without a message it auto-generates "update N files" |
| `/workflow <name> [JSON args]` | Run a workflow |

`/goal` determines completion in three prioritized tiers: with `--check`, the command's exit code decides (trusted); without it, the model self-assesses by emitting `<goal-status>achieved</goal-status>` at the end of its reply, which requires human confirmation; if the `--rounds` limit is reached without success, the goal becomes "limit reached" and per-round summaries are printed. Goal state is persisted and can be resumed across sessions; `/goal stop` also interrupts the current turn.

`/agent` and `/workflow` currently hand off to the model via prompt rather than calling tools directly: the command itself neither starts a subagent nor executes a workflow — it only writes the type, name, and arguments into the current turn's prompt so the model can invoke the corresponding tool.

## 🛠️ Built-in Tools
Y-code registers 94 tools by default (`computer_action` is added when computer control is enabled, excluding the 5 dedicated tools from the iMOM project-locator plugin stack), grouped by purpose:

### Files and code (read-only)

| Tool | Function |
|------|----------|
| `read_file` | Read a file (offset/limit/line numbers/compression; large files are auto-truncated) |
| `list_directory` | List directory contents (supports depth=2..4 trees) |
| `glob_files` | Find files by glob pattern |
| `search_code` | Cross-file code search (ripgrep-based, bad regex falls back to literal) |
| `get_outline` | Extract a source file's structure outline (classes / function signatures with line ranges) |
| `find_definition` | Locate a symbol definition (Python/JS/TS/Go/Rust/Java/C/C++/C#) |
| `find_references` | Find all references to a symbol across files (word-boundary, for impact analysis) |
| `tool_result_read` | Retrieve the original text behind a folded (snip/elide) tool result |

### File changes and commands

| Tool | Function |
|------|----------|
| `write_file` | Create a new file or fully rewrite a small one (refuses to overwrite existing files over 500 lines) |
| `edit_file` | Patch an existing file locally (string-match / line-range paths, with similarity fallback) |
| `multi_edit` | Atomic batch edits within one file (single lock, single write; any failure aborts the whole batch) |
| `execute_command` | Run local commands (30s default timeout, background mode, child-process env protection) |
| `undo_turn` | Revert all controlled file changes in a turn (pre-checks first; refuses if files changed afterwards) |

### Network and media

| Tool | Function |
|------|----------|
| `fetch_url` | Fetch a web page or JSON (with SSRF protection) |
| `fetch_image` | Safely download a web image (http/https only; png/jpg/gif/webp whitelist, 10MB cap) |
| `search_news` | Web / news search (360 Search first, Bing RSS fallback) |
| `get_weather` | Current weather and short-term forecast for a city |
| `hunyuan_generate_image` | Tencent Hunyuan text-to-image (multiple sizes, image-to-image, custom watermark) |
| `tokenrhythm_generate_image` | TokenRhythm text-to-image with qwen-image-2.0 / wan2.7-image, 1–4 images per call |
| `tokenrhythm_edit_image` | TokenRhythm image-to-image: one source image plus an instruction |
| `tokenrhythm_generate_video` | TokenRhythm text-to-video / image-to-video: 2–30s, 480P / 720P / 1080P, billed per second |

> The three TokenRhythm tools require enabling the `tokenrhythm_image` / `tokenrhythm_video` plugins and configuring the matching provider key (off by default).

### Sessions and memory

| Tool | Function |
|------|----------|
| `session_manage` | Session maintenance (list / create / rename / delete / archive / move workdir / clear / compress / checkpoints / undo records) |
| `session_export` | Export a session to md / html / json |
| `search_sessions` | Full-text search across sessions, including compressed-away early messages |
| `search_conversations` | Search sessions by name or content |
| `recall_history` | Pull back earlier messages that were compressed out of the current session |
| `read_project_profile` | Read a project profile (stack / decisions / constraints / pitfalls / preferences / task log) |
| `remember_decision` | Record a project-level conclusion into a specific profile section |

### Planning and dispatch

| Tool | Function |
|------|----------|
| `ask_user` | Ask the user a clarifying question (1–3 options) |
| `plan_write` / `plan_read` / `plan_update` | Create, read, and update a task plan |
| `plan_retry` | Reset a failed item to pending and dispatch it again |
| `plan_finish` | Finish a plan item taken over by the main agent (actually runs its verification command) |
| `plan_resume_takeover` | Resume a plan item that timed out and moved to the main agent |
| `run_subagent` | Start an auxiliary agent (5 built-in types plus custom subagents) |
| `manage_task` | Inspect / wait on / kill background tasks and background subagents |
| `dispatch_task` / `dispatch_batch` | Secretary dispatch (single / batch, with intra-batch dependencies) |
| `list_dispatches` / `get_dispatch_result` | Review the dispatch ledger and results |
| `list_projects` / `list_sessions` / `read_session` | Secretary cross-project session listing and paginated history reading |
| `manage_memory` | Secretary personal long-term memory management (`~/.ycode/memory/` only) |
| `open_session` | Open (switch to) a session |
| `create_project` | Create a project in a directory (including AGENTS.md scaffolding) |

### Workflows and automation

| Tool | Function |
|------|----------|
| `workflow_list` / `workflow_save` / `workflow_run` / `workflow_finish` | Inspect, save, run, and close out workflows |
| `automation_list` / `automation_create` / `automation_update` / `automation_delete` / `automation_run_now` | Manage scheduled automation tasks (interval / daily / weekly / startup / one-off triggers) |

### Administration and operations

| Tool | Function |
|------|----------|
| `model_manage` | Add/remove providers and models, switch the active model, set reasoning effort, test connectivity, fetch a vendor's model list |
| `skill_manage` | List, enable/disable, rescan skills, or restore a built-in version |
| `plugin_manage` | List plugins, enable/disable, run plugin settings actions |
| `hook_manage` | List, add/edit, delete, and disable hooks (no trust granting) |
| `usage_stats` | Token usage over 1 / 7 / 30 / 365 days, by model and cache hit rate |
| `update_manage` | Self-update check, download, install, and cache cleanup |
| `system_settings` | Read/write non-security settings (default shell, step limit, search excludes, AI output dir) |
| `doctor_run` | Aggregated self-check (environment / config / credentials / database / MCP / model endpoints) |
| `diagram_manage` | Export Mermaid diagrams to local HTML, list and open them |
| `theme_manage` | View, activate, copy, import/export themes |
| `persona_manage` | Read/write persona (agent name, persona body, how it addresses you) |
| `decision_manage` | Configure the JEV decision model, probe keys, set the four business scenes |
| `feishu_manage` | Configure the Feishu channel, validate credentials, check connection status |
| `lan_manage` | LAN phone-remote service status, start/stop |
| `mcp_add_server` / `mcp_remove_server` / `mcp_set_enabled` / `mcp_list_servers` | Add/remove MCP servers, enable/disable, list (keys masked) |

### Computer control and system (Windows-only parts noted)

| Tool | Function |
|------|----------|
| `computer_control` | Computer-control run control (status / pause / resume / emergency stop / browser options) |
| `computer_action` | Single-step action and observation on a browser or the desktop (element refs first, `sequence` batching and conditional waits; registered only when computer control is enabled) |
| `browser_pane` | Open / navigate / back-forward / reload the embedded browser pane |
| `browser_set_viewport` | Set the project browser resolution (375×667 for mobile pages, 1440×900 for desktop) |
| `screenshot_capture` | Capture the screen (full or region) to a local file |
| `clipboard_image` | Read an image from the clipboard and save it |
| `pty_manage` | Persistent shell pseudo-terminal (keeps cwd / REPL state) |
| `windows_process_list` / `windows_process_kill` | List / kill processes (protected system processes refused) |
| `windows_clipboard_get` / `windows_clipboard_set` | Read / write clipboard text |
| `windows_registry` | Read-only registry access (keys and values; no writes) |
| `windows_notify` | Show a Windows message box |

### Office and novels

| Tool | Function |
|------|----------|
| `office_read` | Read xlsx / xlsm / docx / pptx / pdf and convert to structured Markdown |
| `office_write` | Generate a standard Excel file (header styling, zebra rows, frozen header, autofilter, print layout) |
| `novel_init` | Initialize a novel project (directory tree, project marker, 7 writing subagents, reference material) |
| `novel_status` | Novel project status (chapters written, total words, today's words, next chapter, foreshadow count) |
| `novel_outline` | Read volume-outline sections by unit / scope (outline stage / writing stage / table of contents) |
| `novel_track` | Commit a per-chapter tracking transaction (foreshadowing, character state, timeline, context rendering) |
| `novel_wordcount` | Count visible body words with the shared measurement |
| `novel_audit` | Deterministic novel health check (tracking gaps, overdue foreshadowing, short chapters, missing outlines, AI patterns) |

> The built-in "iMOM manufacturing system locator" plugin separately provides five tools: `imom_register_project` / `imom_list_projects` / `imom_use_project` / `imom_build_index` / `imom_locate`. They perform four-layer page → Controller → SqlMapping localization for iMOM/MES project code and are useful in iMOM-related projects.

## 🧠 Subagents and the Plan System

### Subagents

Start auxiliary agents through the `run_subagent` tool. Five built-in types:

| Type | Purpose | Default timeout |
|------|------|---------|
| `explore` | Read-only exploration (prefer it for explorations expected to exceed 3 tool calls) | 240s |
| `implement` | Modify code | 600s |
| `test` | Run tests and locate failures | 480s |
| `review` | Read-only review | 300s |
| `general` | General-purpose tasks | 600s |

**Custom subagents**: Create a Markdown file under `~/.ycode/agents/*.md` (user level) or `<cwd>/.ycode/agents/*.md` (project level) with frontmatter fields: `name` / `description` / `when_to_use` / `base_type` / `model` / `allowed_tools`.

**Characteristics**:
- Nesting depth is hard-limited to 3 levels to prevent infinite recursion.
- Independent subtasks are dispatched in parallel at once.
- On failure / timeout / cancellation, the main agent falls back to its own tools.
- A subtask is retried at most once.

### Plan mode

For multi-step tasks (more than 3 subtasks after decomposition), create an executable plan with `plan_write` first:

1. `plan_write` creates the plan (with dependency graph, stages, and verification methods).
2. The main agent's general tools become restricted; it may only use `run_subagent` / `ask_user` / `plan_read` / `plan_retry` / `plan_update`.
3. Dispatch each execution item via `run_subagent` in dependency order.
4. On failure, `plan_retry` resets the item before re-dispatch; after 2 failures on the same item, the root cause must be analyzed first.
5. An item may only be marked `completed` when the subagent truly reports success with no failed tools.

**Plan item fields**: `id` / `subject` / `status` / `depends_on` / `owner` / `files` / `verification` / `stage` (plan/build/review) / `kind` (execution/input_preparation)

## 🔒 Permissions and Security

### Five permission modes

| Mode | Description |
|------|------|
| `confirm` (default) | Queries pass automatically; any modification, deletion, or execution change is asked first |
| `accept_edits` | Queries plus ordinary edits and builds pass automatically; deletions, dependency installs, process termination, Git commit/push, and other sensitive operations are still asked |
| `auto_decision` | Permission requests and clarifying questions are evaluated automatically by the decision model (JEV); falls back to user confirmation when unconfigured or high-risk |
| `plan` | Only reads, read-only queries, plan metadata, and user clarifications are allowed; modifications and deletions are rejected outright with no authorization bypass |
| `full` | All non-catastrophic operations inside the sandbox pass automatically; access outside the sandbox depends on the sandbox toggle |

### Sandbox mode

`/sandbox off` (default): accessing outside the current working directory requires confirmation. `/sandbox on`: out-of-sandbox access to non-credential paths passes automatically. Sandbox confirmations offer "Allow this time", "Reject", and "Allow out-of-sandbox access for this task". Task authorization is bound to the task ID, survives interface switches, and is not shared across parallel tasks; it clears when the task is deleted or the app restarts.

### Project allowlist

The CLI and GUI share the project allowlist in `permissions.json`. The settings page allows adding entries manually, deleting them individually, or clearing the list. In the CLI:

```text
/whitelist list
/whitelist add command git status
/whitelist remove command git status
/whitelist add write path/to/file.txt
/whitelist remove write path/to/file.txt
/whitelist clear confirm
```

Command rules match by token prefix; file rules match only the exact normalized path and never grant recursive directory access. Ordinary permission prompts offer "Allow this time", "Reject", "Allow similar operations for this task", or "Add to project allowlist". The project allowlist cannot override sandbox boundaries, plan mode, or catastrophic-command rejections.

### Security measures
- Catastrophic commands are hard-rejected (`rm -rf /`, fork bombs, disk overwrites, `mkfs`, `format`, etc.) — no mode, authorization, or allowlist can let them through.
- `fetch_url` has built-in SSRF protection (blocks cloud metadata endpoints by default; strict mode enables full IP validation).
- Path traversal protection (task ID format constraints, memory filename validation).
- Atomic config writes with `.bak` backups and self-healing.

## 📝 Memory System

### Short-term memory
- **SQLite persistence**: Chat history is stored per session in isolation.
- **Per-turn undo**: Original bytes of writes/edits/downloads during a turn are snapshotted into a content-addressed store before the write. `/undo` pre-checks (zero writes on fingerprint conflict) → one-time token → per-file atomic restore. Interrupted transactions freeze the affected paths and support continuing or cancelling recovery.
- **Session-level checkpoints**: A persistent checkpoint is created before each turn; `/checkpoint restore <turn_id>` rolls back a whole turn.

### Long-term memory (`~/.ycode/memory/`)

Four memory types, modeled on Claude's memory pattern:

| Type | Purpose |
|------|------|
| `user` | User role, goals, preferences |
| `feedback` | Guidance / corrections given by the user |
| `project` | Ongoing work, decisions, incidents |
| `reference` | Pointers to external system resources |

**Two ways to save**:
- Option A: `<memory>...</memory>` tags (quick notes, auto-extracted into the daily log)
- Option B: Write a `.md` file directly (with frontmatter)

**Dream consolidation**: A background thread collects recent logs plus the MEMORY.md index, uses a large model to generate a consolidation strategy, and updates the categorized memory files.

### Memory compression
- Four-level watermarks: at 60% of the window tool results are snipped / folded → at 80% elided → at 95% a model summary (LLM compression) runs
- Insert-mode compression reuses the bytes of the session's last main request and appends the compression instruction; if the snapshot is missing or the route does not support it, it silently falls back to the segmented path
- Idle maintenance: after 240s of user idleness (`memory_compression.idle_maintenance_seconds`, 0 disables) a local watermark pass and pre-compression run
- Tool results are pre-truncated (long results keep the head, the tail, and a placeholder); each message's bytes can be rewritten at most twice and freeze permanently afterwards, keeping the cached prefix stable
- Token estimation prefers tiktoken (cl100k_base), falling back to character-count approximation

## 🔌 MCP Integration

### MCP client
- Config file: `~/.ycode/mcp.json`
- Lazy connection: started on first use, with an independent asyncio event loop on a daemon thread
- Timeouts: 10s to connect, 60s per tool call

### Full-screen CLI configurator
`/mcp` opens the configurator with 10+ preset templates. Pick one and fill in the fields as prompted — no manual JSON editing:

- **Official Chinese services**: Amap, Feishu, WeChat Official Accounts
- **Community**: Bilibili search
- **Databases**: SQLite, PostgreSQL
- **Web scraping / search**: Firecrawl, Wigolo
- **General utilities**: Sequential Thinking, Filesystem

### AI self-configuration
Through the `mcp_add_server` / `mcp_remove_server` / `mcp_set_enabled` / `mcp_list_servers` tools, the AI can configure MCP on your behalf; API keys in `env` are masked.

## 🎨 Skills and Plugins

### Skill system
Built on `SKILL.md` files that are injected into the system prompt and influence LLM behavior.

**Scanned directories** (by priority):
1. User level: `~/.ycode/skills/`
2. Global compatibility: `~/.agents/skills/` (scanned only if it already exists)
3. Project level: explicitly configured via `skill_search_paths` in `config.json`

**Built-in skills ship with the installer**: The bundled `skills/` are released to `~/.ycode/skills/` on first launch and enabled by default. **Built-in skills that have been released and never modified by the user upgrade automatically with new versions**; anything the user modified is preserved, and anything the user deleted stays deleted.

**Deletion**: the trash button at the end of each row deletes the skill directory after double confirmation, removes it from the active list, and clears its fingerprint cache. Sources inside the installer are refused, as are symlinked or out-of-root paths.

**Skill file format**:
```markdown
---
name: my-skill
description: An example skill
---

Detailed instructions for the skill, injected into the system prompt.
```

### Plugin system
Built on `plugin.py` code that registers new tools and extends tool capabilities.

**Directory structure**:
```
plugins/<plugin_name>/          # Built-in plugins (shipped with the repo)
  plugin.json                    # {"name","version","description","entry","enabled"}
  plugin.py                      # Must expose a register(registry) function
~/.ycode/plugins/<plugin_name>/  # User-level plugins (same name overrides built-in)
```

**Characteristics**:
- The loader is invoked automatically at the end of `core/tool_registry.py:build_default_registry()`.
- Plugins installed during runtime become visible immediately (incremental hot rescan, no restart needed).
- A failing plugin only logs and is skipped — it never blocks ycode from starting.
- Plugin tools automatically inherit permission interception, schema argument filtering, and result truncation through `ToolSpec` metadata.
- The `/plugins` command lists installed plugins.

**Bundled plugins**:

| Plugin | Location | Function |
|------|------|------|
| Hunyuan text-to-image | Repo `plugins/hunyuan_image/` | Tencent Hunyuan text-to-image; data is compiled into the PYZ. The key is read only from the `HUNYUAN_API_KEY` environment variable and never stored in code; the tool reports a clear error when unconfigured |
| TokenRhythm image / video | Repo `plugins/tokenrhythm_image/`, `plugins/tokenrhythm_video/` | Text-to-image (qwen-image-2.0 / wan2.7-image), image editing, and text/image-to-video (wan3.0-video, billed per second); enable in the plugin page and configure the provider key — off by default |
| Office documents | Repo `plugins/office/` | `office_read` / `office_write`: read xlsx / docx / pptx / pdf and generate standard-format Excel |
| Novel workspace | Repo `plugins/novel_workspace/` | Six tools: `novel_init` / `status` / `outline` / `track` / `wordcount` / `audit` |
| Windows system tools | Repo `plugins/windows_tools/` | List / kill processes, read / write clipboard text, read-only registry, system notification (Windows only) |
| iMOM locator | `~/.ycode/plugins/imom_locator/` (user level) | Multi-project code localization for iMOM/MES: dynamic publish discovery, system identification, page index building, and millisecond-level lookup |

> User-level plugins live in `~/.ycode/plugins/<plugin_name>/`; a plugin with the same name overrides the built-in/repository version.
> The trash button on the plugins page physically deletes user-level plugins, or soft-removes built-in plugins (recorded in the removed list without deleting bundled files; restorable from the "Removed" view or via `/plugins restore`). The phone remote API blocks deletion, toggling, and restoration.

## 📁 Project Structure

```
xingyao-y-code/
├── main.py                  # CLI entry point
├── gui_main.py              # GUI entry point (pywebview)
├── gui_bridge.py            # GUI JS-API bridge (split into browser / git / pet / snip mixins)
├── config.py                # Config loading (atomic writes + self-healing)
├── config.example.json      # Config template
├── system_prompt.md         # System prompt (CLI)
├── system_prompt_gui.md     # System prompt (GUI)
├── system_prompt_secretary_gui.md  # System prompt (secretary sessions)
├── core/                    # Core modules (144 Python modules)
│   ├── version.py           # Version number (single source of truth)
│   ├── agent_service.py     # Conversation service (shared by CLI/GUI)
│   ├── harness/             # Main-agent kernel (state machine / journal / tool pipeline / resume policy)
│   ├── computer/            # Computer control (browser channel / desktop UIA / element locating / settings)
│   ├── cloud_relay/         # Cloud relay client and portal
│   ├── serve/               # LAN services and metrics
│   ├── workflows/           # Workflow parsing, execution, and ledger
│   ├── automation/          # Scheduled automation tasks and triggers
│   ├── hooks/               # Lifecycle hooks
│   ├── git/                 # Git workbench bridge
│   ├── feishu/              # Feishu channel
│   ├── slash_commands/      # Desktop slash commands
│   ├── turn_undo/           # Per-turn undo snapshots
│   ├── llm.py               # Model request entry and tool orchestration
│   ├── llm_native_transport.py  # Custom HTTPX transport (HTTP/2, in-flight abort)
│   ├── model_discovery.py   # Vendor model-list fetching (shared by CLI / GUI)
│   ├── model_selector.py    # Full-screen model selector (with model-list picking)
│   ├── model_manager.py     # Multi-model configuration management
│   ├── tools.py             # Local tool implementations
│   ├── tool_registry.py     # Tool registry
│   ├── tool_protocol.py     # Tool protocol (ToolSpec metadata)
│   ├── tool_result_maintenance.py  # Tool-result snip / elide maintenance
│   ├── memory.py            # SQLite session memory + checkpoints
│   ├── memory_compressor.py # Memory compression (segmented / insert mode)
│   ├── memory_kairos.py     # Long-term memory + dream consolidation
│   ├── memory_compression.py# Compression policy config
│   ├── cache_keepalive.py   # Prompt-cache keepalive
│   ├── idle_maintenance.py  # Idle watermark maintenance and pre-compression
│   ├── project_profile.py   # Project profile
│   ├── novel_service.py     # Novel project service (word count / tracking / audit)
│   ├── pet_skins.py         # Desktop pet packs (frame sequences)
│   ├── decision_service.py  # JEV decision-model integration
│   ├── plan.py              # Plan management
│   ├── task_manager.py      # Background task management
│   ├── task_scheduler.py    # Dependency-aware execution scheduler
│   ├── subagent_registry.py # Subagent registry
│   ├── permission_broker.py # Permission broker
│   ├── command_policy.py    # Command risk tiers and child env policy
│   ├── mcp_client.py        # MCP client
│   ├── mcp_selector.py      # MCP full-screen configurator
│   ├── skills.py            # SKILL.md scanning, injection, and deletion
│   ├── skill_selector.py    # Skill selector
│   ├── plugin_loader.py     # Plugin loader (hot-load / soft-remove & restore / deletion)
│   ├── lsp.py               # LSP-lite symbol lookup
│   ├── worktree.py          # Git worktree
│   ├── session_selector.py  # Full-screen session selector
│   ├── thinking_stream.py   # Thinking stream buffering
│   ├── reasoning_stream.py  # Reasoning stream parsing
│   ├── terminal_markdown.py # Terminal Markdown rendering
│   ├── diff_viewer.py       # Diff rendering
│   ├── turn_renderer.py     # Turn rendering
│   ├── history_renderer.py  # History rendering
│   ├── tool_presenter.py    # Tool presentation
│   ├── tool_cache.py        # In-process read-only tool result cache
│   ├── context_builder.py   # Context building
│   ├── turn_controller.py   # Turn control
│   ├── input_parser.py      # User input parsing
│   ├── paste_placeholders.py# Paste placeholders
│   ├── process_io.py        # Process output decoding
│   ├── share.py             # Session export
│   ├── themes.py            # Terminal color themes
│   ├── ask_user.py          # User clarification
│   ├── runtime_warnings.py  # Runtime warnings
│   ├── ui_markers.py        # UI markers
│   └── app_context.py       # Application context
├── frontend/               # GUI frontend (Vue 3 + TypeScript + Vite, 75 Vue components)
│   ├── src/
│   │   ├── components/      # Vue components
│   │   ├── composables/     # Composables (panel drag, viewport detection, etc.)
│   │   ├── stores/          # Pinia state management
│   │   └── utils/
│   ├── package.json
│   └── vite.config.ts
├── frontend-mobile/        # Phone pages (served by the relay service)
├── relay_server/           # Relay server (device registration / own tokens / static assets)
├── pets/                   # Bundled desktop pet packs (frame sequences)
├── site_admin/             # Website admin (site info and download package management)
├── website/                # Static website
├── skills/                 # Built-in skills (15 skill packs)
├── plugins/                # Built-in plugins (Hunyuan image / TokenRhythm image & video / office / novel workspace / Windows tools)
├── vendor/ripgrep/         # Bundled ripgrep
├── vendor/mermaid/         # Bundled Mermaid rendering assets
├── assets/                 # CHANGELOG, icons, and other packaging assets
├── packaging/              # Packaging helpers
├── scripts/                # Maintenance scripts
├── tests/                  # pytest test cases (394 files)
├── docs/                   # Documentation
│   ├── BUILD.md            # Build documentation
│   ├── all_features.md     # Full feature documentation
│   ├── module_map.md       # Module map
│   └── superpowers/        # Design and planning documents
├── build_release.py        # Windows release build pipeline
├── publish_release.py      # Release upload script
├── ycode.spec              # PyInstaller spec for both entry points
├── ycode_setup.py          # Windows install wizard (classic style, user/machine scopes)
├── ycode_setup.spec        # Install wizard build spec (reference for manual builds)
├── ycode_uninstall.py      # Windows uninstall wizard (classic-style GUI)
├── ycode_uninstall.spec    # Uninstall wizard build spec
└── README.md               # Chinese version (this is README.en.md)
```

## ⚙️ Configuration Reference

Config file location: `~/.ycode/config.json`. A fresh install starts with a clean, empty config; users add providers through the GUI model page's "Add provider" or the CLI `/models` preset catalog. Source developers can also run `cp config.example.json config.json` and edit it.

### Full configuration

```json
{
  "active_model_id": "model_1",
  "secondary_model_id": null,
  "secondary_model_roles": {
    "vision": true,
    "compression": true,
    "subagent": true
  },
  "image_compression": {
    "max_edge": 1536,
    "quality": 90,
    "max_output_bytes": 1048576
  },
  "search_exclude_dirs": ["target", "out", "bin", "obj"],
  "subagent_enabled": true,
  "memory_compression": {
    "retain_count": 8,
    "retain_count_force": 4,
    "tool_result_truncate_threshold_chars": 2000,
    "tool_result_preview_limit_chars": 800,
    "tool_result_token_limit": 600,
    "summary_length_first": 250,
    "summary_length_merge": 350,
    "insert_mode": true,
    "precompress_ratio": 0.85,
    "idle_maintenance_seconds": 240
  },
  "providers": [...],
  "theme": "cyan",
  "permission_mode": "confirm",
  "allow_outside_workspace": false,
  "command_shell": "auto",
  "skill_search_paths": [],
  "prompt_cache_keepalive_seconds": 120
}
```

### Configuration fields

| Field | Description |
|--------|------|
| `active_model_id` | ID of the currently active primary model |
| `secondary_model_id` | Secondary model ID (for vision / compression / subagents) |
| `secondary_model_roles` | Secondary model role switches (vision / compression / subagent) |
| `image_compression` | Image compression parameters (default: long edge 1536, quality 90, target 1MiB) |
| `search_exclude_dirs` | Directories excluded from code search |
| `subagent_enabled` | Global subagent switch: `false` disables subagent dispatch and plan orchestration tools (plan_write/plan_update); all subtasks are done directly by the main agent (default `true`) |
| `memory_compression` | Memory compression parameters (`insert_mode` insert-mode compression, `idle_maintenance_seconds` idle-maintenance trigger seconds (0 disables), `precompress_ratio` pre-compression watermark) |
| `providers` | List of model providers |
| `theme` | Terminal color theme (9 available) |
| `permission_mode` | Permission mode (confirm / accept_edits / auto_decision / plan / full) |
| `allow_outside_workspace` | Sandbox toggle |
| `command_shell` | Default command interpreter (auto / pwsh / powershell / cmd; the GUI "System settings" page restricts choices to what is installed) |
| `skill_search_paths` | Skill search paths (project level) |
| `prompt_cache_keepalive_seconds` | DeepSeek / Kimi / GLM / Qwen prefix cache keep-alive interval (default 120; 0 disables; minimum 60) |
| `shell_env_policy` | Child-process environment policy: `exclude_sensitive` strips `*KEY*` / `*SECRET*` / `*TOKEN*` / `*PASSWORD*` by default (set `false` to pass everything through); also supports `inherit` / `exclude` / `include_only` / `set` |
| `expose_mcp_admin_tools` | Whether to send MCP admin tool schemas to the model (default `false`, saves ~500 tokens per turn) |
| `resume` | Resume-after-interruption switch (default `{"enabled": false}`) |
| `pet_skin_id` | Active desktop pet pack ID (restart the pet process to apply) |
| `route_request_overrides` | Rewrite request headers / body per host (for gateways that require specific headers or thinking fields) |

### config.toml read-only overlay (optional)

Besides `config.json`, Xingyao can read `~/.ycode/config.toml` as a **read-only overlay**, making it easy to follow the TOML conventions of tools like Codex / Kimi CLI or migrate config fragments:

- **Read effects**: `config.toml` is deep-merged over `config.json`, with TOML keys taking priority (final precedence: command-line arguments > config.toml > config.json > built-in defaults).
- **Writes always go to JSON**: Save paths such as the GUI and `/config` only write `config.json` and never generate or rewrite TOML. If a saved key is also overridden by TOML, you are warned that "the save succeeded but the effective value still comes from TOML".
- **Fault tolerance**: TOML syntax errors or read failures only warn and fall back to plain JSON — they never block startup; automatically ignored on Python < 3.11.
- **Trade-off to note**: TOML values solidify into `config.json` after one write operation, so deleting the TOML afterward will not roll them back (edit the JSON back manually or delete the keys and reconfigure).

Example `config.toml`:

```toml
theme = "nord"
permission_mode = "accept_edits"

[memory_compression]
retain_count = 12
```

Image attachments allow up to 20MiB and at most 40 million pixels after decoding. PNG (including alpha) stays PNG; JPEG/WebP progressively lowers quality when necessary; GIF keeps its original animation. When sent to a vision model, a Base64 data URL matching the actual encoding is used. Processing results in the GUI are pinned below the corresponding user message.

### User data directory

Files under `~/.ycode/`:

| Path | Purpose |
|------|------|
| `config.json` | User-level configuration |
| `config.toml` | Optional read-only overlay (higher priority than config.json; see "Configuration Reference") |
| `ycode.db` | CLI session database |
| `ycode_gui.db` | GUI session database (invisible to the CLI) |
| `active_skills.json` | List of enabled skills |
| `mcp.json` | MCP service configuration |
| `system_prompt.md` | User-level system prompt override |
| `memory/` | Long-term memory (logs / user / feedback / project / reference) |
| `self/` | Self model built by self-evolution |
| `skills/` | User-level skills |
| `plugins/` | User-level plugins |
| `agents/` | Custom subagent definitions |
| `pets/` | User-built desktop pet packs |
| `hooks/` and `hooks.json` | Lifecycle hook definitions and trust records |
| `gui-themes/` | Custom interface themes |
| `persona.json` | Persona (agent name / body / how it addresses you) |
| `decision.json` and `decision_audit/` | Decision-model config and decision audit records |
| `exports/` / `media/` / `tmp/` | Session exports, media attachments, AI-generated content tmp |
| `undo_blobs/` / `undo_transactions/` | Per-turn undo file snapshots and transaction logs |

## 🧪 Running Tests

```bash
pytest
```

Tests cover 394 module files, including agent_turn_e2e, harness (models/journal/runtime/request fingerprinting/tool pipeline), cancellation, config, memory_kairos, permission_broker, plan, task_scheduler, subagent_registry, tools, setup_engine, and uninstall_engine.

### Manual smoke tests for computer control (never part of pytest)

`scripts/smoke_computer_control.py` is run manually and explicitly only (it opens visible system browser windows or produces real keyboard/mouse/screen-capture actions); automated tests only validate its code paths with fake injection:

```powershell
# Browser channel probe (system Edge / Chrome, about:blank, no browser download)
venv\Scripts\python.exe scripts\smoke_computer_control.py --probe-browser edge
venv\Scripts\python.exe scripts\smoke_computer_control.py --browser-observe chrome

# Desktop observation (pre-check + one screenshot, no input)
venv\Scripts\python.exe scripts\smoke_computer_control.py --desktop-observe

# Real keyboard and mouse (one fixed move/click/type, prints a redacted action per step
# and requires a single Enter confirmation; open a test window with no sensitive data first)
venv\Scripts\python.exe scripts\smoke_computer_control.py --desktop-input --confirm-real-input I_UNDERSTAND
```

Manual acceptance matrix: first login and restart persistence with a dedicated Edge/Chrome profile, clear failure when the browser is closed, multi-monitor/scaling, no new actions after pause, first action after resume is an observe, emergency stop via GUI stop / CLI `/computer stop` / Ctrl+C / Ctrl+Alt+Esc, fail-closed on lock screen and UAC, and no leftover Playwright child processes or temporary PNGs after exit.

## 🔒 Security and Privacy

- `config.json`, `ycode.db`, `active_skills.json`, and `mcp.json` are listed in `.gitignore` and will not be committed.
- Never write real API keys into source code or the README.
- If you accidentally commit a key, reset it on the corresponding platform immediately.
- The uninstall wizard (GUI) cleans desktop shortcuts, user/system PATH entries, and HKCU/HKLM uninstall registry entries according to the install scope (supporting both new and legacy names), and can optionally keep or delete the `~/.ycode` user configuration.

## 🤝 Contributing

Issues and pull requests are welcome:

- Submit an issue: <https://github.com/fengchen122123-sudo/xingyao-y-code/issues>
- Before opening a PR, make sure `pytest` passes locally and follow the development conventions in `AGENTS.md`.
- If you find sensitive information leaked in the code, please notify the maintainers via an issue right away.

## ⚖️ Disclaimer

By using this software you confirm that you have read and agree to the following points (the full terms consist of 8 chapters, viewable in the app under "Settings → Legal → Disclaimer"; both the installer and the first launch prompt for confirmation):

- **Nature of the software**: This software is an AI coding assistant that runs on your local device. It does not provide or operate any generative AI model service; you choose, configure, and communicate directly with model services yourself.
- **AI-generated content**: Models generate content probabilistically and it may be inaccurate, incomplete, or outdated. This software makes no guarantee regarding its truthfulness, accuracy, completeness, legality, or fitness, and generated content does not constitute professional advice of any kind.
- **Your responsibility**: Before adopting, executing, or distributing generated content, you should review, verify, and test it yourself and bear full responsibility for the outcome. The software can read and write files, execute commands, and control your computer — confirm the scope of impact yourself and back up your data.
- **Data and privacy**: Configuration, session records, and memory data are stored on your local device by default. During use, session content is sent directly to the model service provider you configured.
- **Compliant use**: You may not use this software for activities that violate laws or regulations, nor to generate or distribute illegal or harmful information.
- **Limitation of liability**: **To the maximum extent permitted by law**, the copyright holders and contributors of this software are not liable for any damages arising from the use of or inability to use this software (including but not limited to direct or indirect losses such as data loss, device damage, business interruption, lost profits, and third-party claims). The software is provided "as is", without warranty of any kind, express or implied.

## 📄 License

This project is open source under the [MIT License](LICENSE) and may be freely used, modified, and distributed.
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10+-blue.svg)](https://www.python.org/)
[![Version 1.3.9](https://img.shields.io/badge/Version-1.3.9-green.svg)](core/version.py)

# Xingyao (Y-code)

English | [中文](README.md)

> A terminal-based AI coding assistant built in China, codenamed "Xingyao". It ships both as a TUI console and a GUI window, with multi-model switching, subagent orchestration, plan execution, long-term memory, MCP extension, and a pluggable skill system.

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

### Models and tools

- **Multi-model management**: `/models` opens a full-screen model selector supporting OpenAI / Anthropic / DeepSeek / Agnes and other protocols, unified through a self-built HTTPX engine (`core/llm_native_transport.py`).
- **Secondary model support**: Configure a secondary model to handle vision recognition, memory compression, and subagent calls, splitting work between primary and secondary.
- **29 built-in tools**: File read/write, code search, command execution, web fetching, image download and generation, weather, LSP symbol lookup, plan orchestration, intelligent routing, and more.
- **Tool result caching**: Identical searches and reads hit a local cache automatically, saving tokens and time.

### Core harness

- **Fixed main-agent kernel**: Strict state machines (8 turn states / 10 step states) with explicit failure on illegal transitions. The kernel is not pluggable and cannot be swapped dynamically.
- **SQLite journal**: Owner leases plus heartbeats prevent multi-process conflicts. Event sequences are allocated monotonically, and messages and events commit atomically in the same transaction.
- **Crash recovery**: Incomplete turns are only marked `interrupted` and are never automatically replayed — no model requests, commands, or file writes are re-executed.
- **Request fingerprinting**: An immutable fingerprint is generated for the final provider request, paired with byte-golden regression tests to prevent cache drift.
- **Tool pipeline**: Same-signature repeat-call suppression (RepeatCallGuard) and batched concurrent execution of adjacent read-only tools.

### Advanced orchestration

- **Subagent system**: 5 built-in types (explore / implement / test / review / general) plus custom subagents, with dependency-aware parallel scheduling and nesting up to 3 levels deep.
- **Plan execution mode**: For multi-step tasks (more than 3 subtasks after decomposition), a `plan_write` plan is created first and subtasks are dispatched along the dependency graph, with pause / resume / retry / cancel support.
- **Background task management**: Long-running commands execute in the background. `/tasks` shows status, `/tasks tail` streams logs, `/tasks kill` terminates.
- **Permission system**: Four modes (confirm before changes / accept_edits for automatic edits / plan mode / full access), with a sandbox controlling working-directory access boundaries.

### Memory and context

- **SQLite session memory**: Switch between multiple sessions; undo never rewrites history, so conversations stay intact.
- **Per-turn undo**: Controlled file modifications in each turn are snapshotted persistently. `/undo` runs a full pre-check before a one-click revert — zero writes on conflict, recoverable after crashes — and the model learns through context that the file results are invalid.
- **Long-term memory system**: Four memory types (user / feedback / project / reference) plus daily logs and a Dream consolidation thread. Supports "remember XXX" instructions.
- **Intelligent memory compression**: Automatically compresses when history reaches 70% of the context window, pre-truncates tool results, and estimates tokens precisely with tiktoken.
- **Session export**: `/share` exports to Markdown / HTML / JSON in one command.

### Extension ecosystem

- **Skill system (Skills)**: Scans `SKILL.md` files and injects them into the system prompt, supporting both project-level and user-level skills.
- **Plugin system (Plugins)**: In-process plugins extend tool capabilities with zero core intrusion; a failing plugin never blocks startup. Ships with a Hunyuan text-to-image plugin and supports hot-loading of user-level plugins.
- **MCP client**: A full-screen CLI configurator with 10+ preset templates (Feishu / Amap / Bilibili / SQLite / Postgres, etc.). The AI can configure MCP itself.
- **Git worktree**: `/worktree` manages parallel multi-branch workspaces.
- **Git workbench**: A GUI "Source Control" tab plus the CLI `/git` command (status / diff / stage / commit / branch / sync / stash / history / conflicts), with automatic refresh on external changes, repository trust confirmation, and double confirmation for high-risk operations. AI-initiated Git operations in conversations still follow the normal permission flow.

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
| `/permission <confirm\|accept_edits\|plan\|full>` | Switch permission level |
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

Y-code ships 29 built-in tools (excluding the 5 locator tools from the iMOM project-locator plugin stack), split into read-only and mutating groups:

### Read-only tools

| Tool | Function |
|------|------|
| `read_file` | Read a file (supports offset/limit/line numbers/compression; large files are auto-truncated) |
| `list_directory` | List directory contents |
| `search_code` | Cross-file code search (based on ripgrep) |
| `glob_files` | Find files by glob pattern |
| `find_definition` | Locate symbol definitions (Python/JS/TS/Go/Rust/Java/C/C++/C#) |
| `search_news` | Online news search |
| `fetch_url` | Fetch web pages or JSON (with SSRF protection) |
| `get_weather` | Query real-time weather for a city |

### Mutating tools

| Tool | Function |
|------|------|
| `write_file` | Create a new file or fully rewrite a small file (refuses to overwrite existing files over 500 lines) |
| `edit_file` | Edit an existing file locally (string-match or line-number paths, with intelligent similarity fallback) |
| `execute_command` | Execute local commands (30s default timeout, background execution supported) |
| `fetch_image` | Safely download a web image locally (http/https only, png/jpg/gif/webp allowlist, 10MB limit) |
| `hunyuan_generate_image` | Tencent Hunyuan text-to-image: generate an image from a text description and save it locally (multiple sizes, custom watermark, 10–60 seconds) |
| `tokenrhythm_generate_image` | TokenRhythm text-to-image: qwen-image-2.0 / wan2.7-image models, downloaded immediately after generation (requires the plugin toggle and provider key; off by default) |
| `tokenrhythm_edit_image` | TokenRhythm image-to-image: upload 1 source image (≤10MB) plus a text instruction (same prerequisites) |
| `ask_user` | Ask the user a clarifying question (1–3 options) |
| `manage_task` | Inspect or terminate background tasks |
| `run_subagent` | Start an auxiliary agent (5 built-in types plus custom) |
| `plan_write` | Create or replace the task plan |
| `plan_read` | Read plan status |
| `plan_update` | Update plan item metadata |
| `plan_retry` | Reset a failed item to pending |
| `plan_finish` | Complete a plan item taken over by the main agent (and run its verification command) |
| `plan_resume_takeover` | Resume a plan item handed to the main agent after a timeout |
| `mcp_add_server` | Add an MCP server configuration |
| `mcp_remove_server` | Remove an MCP server configuration |
| `mcp_set_enabled` | Enable / disable an MCP server |
| `mcp_list_servers` | List all MCP servers (API keys masked) |

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

### Four permission modes

| Mode | Description |
|------|------|
| `confirm` (default) | Queries pass automatically; any modification, deletion, or execution change is asked first |
| `accept_edits` | Queries plus ordinary edits and builds pass automatically; deletions, dependency installs, process termination, Git commit/push, and other sensitive operations are still asked |
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
- Trigger threshold: history reaches 70% of the context window
- Tool results are pre-truncated (long results keep the head, the tail, and a placeholder)
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
| iMOM locator | `~/.ycode/plugins/imom_locator/` | Multi-project code localization for iMOM/MES: dynamic publish discovery, system identification, page index building, and millisecond-level lookup |

> User-level plugins live in `~/.ycode/plugins/<plugin_name>/`; a plugin with the same name overrides the built-in/repository version.

## 📁 Project Structure

```
xingyao-y-code/
├── main.py                  # CLI entry point
├── gui_main.py              # GUI entry point (pywebview)
├── gui_bridge.py            # GUI JS-API bridge layer
├── config.py                # Config loading (atomic writes + self-healing)
├── config.example.json      # Config template
├── system_prompt.md         # System prompt
├── core/                    # Core modules
│   ├── version.py           # Version number (single source of truth)
│   ├── agent_service.py     # Conversation service (shared by CLI/GUI)
│   ├── harness/             # Fixed main-agent kernel (strict state machine / SQLite journal / tool pipeline)
│   ├── llm.py               # Model request entry and tool orchestration (custom HTTPX engine)
│   ├── tools.py             # Local tool implementations
│   ├── tool_registry.py     # Tool registry
│   ├── tool_protocol.py     # Tool protocol (ToolSpec metadata)
│   ├── memory.py            # SQLite session memory + checkpoints
│   ├── memory_compressor.py # Memory compression
│   ├── memory_kairos.py     # Long-term memory + dream consolidation
│   ├── plan.py              # Plan management
│   ├── task_manager.py      # Background task management
│   ├── task_scheduler.py    # Dependency-aware execution scheduler
│   ├── subagent_registry.py # Subagent registry
│   ├── permission_broker.py # Permission broker
│   ├── mcp_client.py        # MCP client
│   ├── mcp_selector.py      # MCP full-screen configurator
│   ├── skills.py            # SKILL.md scanning and injection
│   ├── skill_selector.py    # Skill selector
│   ├── plugin_loader.py     # Plugin loader
│   ├── lsp.py               # LSP-lite symbol lookup
│   ├── worktree.py          # Git worktree
│   ├── model_manager.py     # Multi-model configuration management
│   ├── model_selector.py    # Full-screen model selector
│   ├── session_selector.py  # Full-screen session selector
│   ├── thinking_stream.py   # Thinking stream buffering
│   ├── reasoning_stream.py  # Reasoning stream parsing
│   ├── terminal_markdown.py # Terminal Markdown rendering
│   ├── diff_viewer.py       # Diff rendering
│   ├── turn_renderer.py     # Turn rendering
│   ├── history_renderer.py  # History rendering
│   ├── tool_presenter.py    # Tool presentation
│   ├── tool_cache.py        # Tool result cache
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
├── frontend/               # GUI frontend (Vue 3 + TypeScript + Vite)
│   ├── src/
│   │   ├── components/      # 29 Vue components
│   │   ├── composables/     # Composables (panel drag, viewport detection, etc.)
│   │   ├── stores/          # Pinia state management
│   │   └── utils/
│   ├── package.json
│   └── vite.config.ts
├── skills/                 # Built-in skill examples
├── plugins/                # Built-in plugin directory
├── vendor/ripgrep/         # Bundled ripgrep
├── packaging/              # Packaging helpers
├── tests/                  # pytest test cases (117 files)
├── docs/                   # Documentation
│   ├── BUILD.md            # Build documentation
│   ├── all_features.md     # Full feature documentation
│   └── superpowers/        # Design and planning documents
├── build_release.py        # Windows release build pipeline
├── ycode.spec              # PyInstaller spec for both entry points
├── ycode_setup.py          # Windows install wizard (classic style, user/machine scopes)
├── ycode_setup.spec        # Install wizard build spec (reference for manual builds)
├── ycode_uninstall.py      # Windows uninstall wizard (classic-style GUI)
├── ycode_uninstall.spec    # Uninstall wizard build spec
└── README.md               # This file
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
  "memory_compression": {
    "retain_count": 8,
    "retain_count_force": 4,
    "tool_result_truncate_threshold_chars": 2000,
    "tool_result_preview_limit_chars": 800,
    "tool_result_token_limit": 600,
    "summary_length_first": 250,
    "summary_length_merge": 350
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
| `memory_compression` | Memory compression parameters |
| `providers` | List of model providers |
| `theme` | Terminal color theme (9 available) |
| `permission_mode` | Permission mode (confirm / accept_edits / plan / full) |
| `allow_outside_workspace` | Sandbox toggle |
| `command_shell` | Default command interpreter (auto / pwsh / powershell / cmd; the GUI "System settings" page restricts choices to what is installed) |
| `skill_search_paths` | Skill search paths (project level) |
| `prompt_cache_keepalive_seconds` | DeepSeek/Kimi prefix cache keep-alive interval (default 120; 0 disables; minimum 60) |

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
| `skills/` | User-level skills |
| `plugins/` | User-level plugins |
| `agents/` | Custom subagent definitions |

## 🧪 Running Tests

```bash
pytest
```

Tests cover 117 modules, including agent_turn_e2e, harness (models/journal/runtime/request fingerprinting/tool pipeline), cancellation, config, memory_kairos, permission_broker, plan, task_scheduler, subagent_registry, tools, setup_engine, and uninstall_engine.

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
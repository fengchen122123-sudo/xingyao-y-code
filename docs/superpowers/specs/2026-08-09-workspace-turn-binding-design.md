# GUI Workspace Turn Binding Design

## Problem

A pending project conversation can display a project name in the sidebar while the backend process still runs in another directory. The first message persists the intended `workdir` on the session but does not activate it before runtime context creation, input parsing, or tool execution. The pending composer also does not request a file-tree snapshot, so the right panel stays empty.

DeepSeek Responses can additionally expose tool-call chunks whose `id` is an empty string. The stream accumulator currently treats only `None` as missing, persists the empty IDs, and sends ambiguous lifecycle events. Existing GUI history then contains terminal turns with unresolved calls, while the trace presentation treats every call without a result as permanently running.

## Evidence

- SQLite stores the affected session under `E:\fch\cc-mini_reference`.
- The same turn's persisted runtime context says `E:\fch\Y-code_tui`.
- The affected assistant message contains five tool calls with empty IDs, followed by five tool messages with empty IDs.
- `createSessionInGroup()` changes the frontend label and `workdir` but never loads `fileTree`.
- Existing-session switching already changes to the stored directory and returns a file tree, establishing the intended behavior.

## Design

### Turn workspace binding

Before a GUI worker parses input or calls `prepare_turn_messages`, it resolves the effective directory from `pending_workdir` or the persisted session. A non-global directory must exist and is activated with `os.chdir`; invalid directories fail the turn explicitly. Global sessions do not trigger project activation and remain outside this project's binding change.

The binding occurs on every turn, not only the first one, so a session remains authoritative even if another GUI action changed the process directory between turns.

### Pending workspace snapshot

Add a read-only bridge method that validates a project directory and returns its normalized path plus `_get_file_tree()` output. `createSessionInGroup()` calls this method while retaining lazy session persistence. Success replaces `workdir`, label, and `fileTree`; failure surfaces a warning and does not pretend the project was entered.

### Stable tool-call IDs

The core stream accumulator treats `None` and `""` as missing IDs. It assigns deterministic fallback IDs using the tool-call index (`call_<index>`) and still replaces a fallback if a later chunk supplies a real non-empty provider ID. Requested, started, finished, persisted assistant, and tool-result records therefore share one stable ID.

### Terminal trace repair

`buildToolTraceRows()` receives the enclosing execution state. A call without a result remains `running` only while the enclosing turn is running. In completed, cancelled, or error turns it renders as completed, cancelled, or failed respectively. This repairs already-persisted empty-ID histories without inventing tool output or rewriting user data.

## Error Handling

- Missing or non-directory pending workspaces fail before any model request or tool call.
- File-tree snapshot failures return `{ok: false, msg}` and leave the UI out of the project state.
- Empty provider IDs are normalized at the stream boundary; no downstream fallback is required for new turns.
- Historical unresolved calls retain their metadata and details but cannot display a live spinner after the turn is terminal.

## Testing

- Backend worker test proves runtime context construction observes the session directory rather than the process launch directory.
- Bridge test proves a workspace snapshot returns files for an explicit directory without relying on the current directory.
- Store test proves a pending project conversation loads and displays its file tree.
- LLM event test proves multiple empty provider IDs become distinct stable IDs across start and finish events.
- Tool-trace test proves unresolved calls stop running for every terminal enclosing state.
- Run Python tests, all frontend tests, production build, and a local GUI flow covering a pending project conversation.

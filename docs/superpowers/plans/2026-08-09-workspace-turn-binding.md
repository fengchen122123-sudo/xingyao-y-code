# GUI Workspace Turn Binding Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every GUI project conversation execute in its persisted directory, populate the right-side file tree, and prevent terminal tool calls from spinning forever.

**Architecture:** Normalize missing tool-call IDs at the core stream boundary, bind each GUI turn to the session workdir before context construction, expose an explicit read-only workspace snapshot for pending conversations, and make trace presentation respect the enclosing turn's terminal state. Preserve lazy session creation and existing SQLite records.

**Tech Stack:** Python, pytest, Vue 3, TypeScript, Pinia, Vitest, PyWebView

## Global Constraints

- Project sessions use their persisted or pending `workdir` as the authoritative Agent directory.
- Pending project conversations remain lazy and do not create SQLite sessions until the first message.
- Empty tool IDs become stable per-index IDs before persistence and event emission.
- Historical unresolved calls cannot remain visually running after their enclosing turn is terminal.
- No broad session architecture refactor and no history rewrite.

---

### Task 1: Normalize Empty Tool-Call IDs

**Files:**
- Modify: `tests/test_llm_events.py`
- Modify: `core/llm.py`

**Interfaces:**
- Consumes: streamed tool chunks with `index`, `id`, `function.name`, and `function.arguments`.
- Produces: non-empty `tool_calls[*].id` values shared by persistence and all tool lifecycle events.

- [ ] **Step 1: Write the failing test**

Add a test that streams two parallel-safe calls with IDs `""`, executes both, and asserts requested and finished IDs are `call_0` and `call_1` in order.

```python
def test_empty_tool_call_ids_get_unique_fallbacks(mocker):
    first = iter([_chunk(tool_calls=[
        _tool_call(0, "", "glob_files", '{"pattern":"*.py"}'),
        _tool_call(1, "", "glob_files", '{"pattern":"*.md"}'),
    ])])
    final = iter([_chunk("done")])
    mocker.patch("core.llm.completion", side_effect=[first, final])
    mocker.patch("core.llm._execute_single_tool_call", side_effect=lambda tc, **_: {
        "role": "tool", "tool_call_id": tc["id"],
        "name": tc["function"]["name"], "content": "ok",
    })
    events = list(chat_with_model_events(
        "fake-model", "key", [{"role": "user", "content": "inspect"}],
        persist=False, turn_id="turn-empty-ids",
    ))
    assert [e.payload["id"] for e in events if e.kind == "tool_requested"] == ["call_0", "call_1"]
    assert [e.payload["id"] for e in events if e.kind == "tool_finished"] == ["call_0", "call_1"]
```

- [ ] **Step 2: Run the focused test and verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_llm_events.py -k empty_tool_call_ids -q`

Expected: FAIL because both events currently carry an empty ID.

- [ ] **Step 3: Implement the minimal normalization**

Treat a falsey chunk ID as missing. Keep `call_<index>` until a later non-empty provider ID replaces it.

```python
tc_id = getattr(tc, "id", None)
existing_id = tool_calls_dict[idx].get("id")
if tc_id:
    if not existing_id or str(existing_id).startswith("call_"):
        tool_calls_dict[idx]["id"] = str(tc_id)
elif not existing_id:
    tool_calls_dict[idx]["id"] = f"call_{idx}"
```

- [ ] **Step 4: Run the focused test and verify GREEN**

Run the command from Step 2 and expect PASS.

---

### Task 2: Bind GUI Turns and Expose Workspace Snapshots

**Files:**
- Modify: `tests/test_gui_tool_events.py`
- Modify: `tests/test_gui_workspace_groups.py`
- Modify: `gui_bridge.py`

**Interfaces:**
- Consumes: `pending_workdir` or `ApiBridge._session_workdir(session_name)`.
- Produces: `ApiBridge.get_workspace_state(workdir) -> {ok, workdir, file_tree}` and a worker whose runtime context is built after workdir activation.

- [ ] **Step 1: Write failing backend tests**

Add one worker test that starts outside a stored project and captures `os.getcwd()` inside `prepare_turn_messages`. Add one workspace-state test that creates a file under an explicit directory and expects that file in the returned tree.

```python
def test_workspace_state_reads_explicit_project(_bare_gui_bridge, tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "README.md").write_text("project", encoding="utf-8")
    result = _bare_gui_bridge.get_workspace_state(str(project))
    assert result["ok"] is True
    assert [node["n"] for node in result["file_tree"]] == ["README.md"]

def test_worker_activates_session_workdir_before_context(monkeypatch, tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    launch = tmp_path / "launch"
    launch.mkdir()
    monkeypatch.chdir(launch)
    bridge = configured_worker_bridge(str(project))
    seen = {}
    monkeypatch.setattr(
        "core.agent_service.prepare_turn_messages",
        lambda *args, **kwargs: seen.setdefault("cwd", os.getcwd()) or [],
    )
    bridge._run_turn_worker("inspect", "project-session")
    assert pathlib.Path(seen["cwd"]).resolve() == project.resolve()
```

- [ ] **Step 2: Run the backend tests and verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_gui_tool_events.py tests/test_gui_workspace_groups.py -q`

Expected: the worker observes the launch directory and `get_workspace_state` does not exist.

- [ ] **Step 3: Implement workspace binding and snapshot**

Resolve the effective turn workdir before input parsing. Reject missing non-global directories, change into a valid project, update the manager's default workdir, and then build runtime context. Add the read-only snapshot method using `_resolve_real_path()` and `_get_file_tree()`.

```python
def get_workspace_state(self, workdir):
    path = _resolve_real_path(str(workdir or ""))
    if not os.path.isdir(path):
        return {"ok": False, "msg": "项目文件夹不存在"}
    return {"ok": True, "workdir": path, "file_tree": self._get_file_tree(path)}

turn_workdir = pending_workdir or self._session_workdir(session_name)
if turn_workdir != "GLOBAL":
    turn_workdir = _resolve_real_path(turn_workdir)
    if not os.path.isdir(turn_workdir):
        raise FileNotFoundError(f"会话项目文件夹不存在: {turn_workdir}")
    os.chdir(turn_workdir)
    self._memory.set_workdir(turn_workdir)
```

- [ ] **Step 4: Re-run backend tests and verify GREEN**

Run the command from Step 2 and expect PASS.

---

### Task 3: Load Pending Project File Trees

**Files:**
- Create: `frontend/src/stores/appStore.workspace.test.ts`
- Modify: `frontend/src/stores/appStore.ts`
- Modify: `frontend/src/types/pywebview.d.ts`
- Modify: `frontend/src/components/RightPanel.vue`

**Interfaces:**
- Consumes: `PyWebViewAPI.get_workspace_state(workdir)`.
- Produces: a pending project state with normalized `workdir`, label, and populated `fileTree`.

- [ ] **Step 1: Write the failing store test**

Mock `get_workspace_state()` to return a project file and assert `createSessionInGroup()` requests the target directory and stores the returned tree while retaining `pendingWorkdir`.

```ts
it('loads files when entering a pending project conversation', async () => {
  const getWorkspaceState = vi.fn().mockResolvedValue({
    ok: true,
    workdir: 'E:\\projects\\demo',
    file_tree: [{ n: 'README.md', d: 0 }],
  });
  (window as any).pywebview = { api: { get_workspace_state: getWorkspaceState } };
  const store = useAppStore();
  await store.createSessionInGroup('E:\\projects\\demo');
  expect(getWorkspaceState).toHaveBeenCalledWith('E:\\projects\\demo');
  expect(store.pendingWorkdir).toBe('E:\\projects\\demo');
  expect(store.fileTree).toEqual([{ n: 'README.md', d: 0 }]);
});
```

- [ ] **Step 2: Run the focused test and verify RED**

Run: `npm.cmd test -- --run src/stores/appStore.workspace.test.ts`

Expected: FAIL because `createSessionInGroup()` does not call the backend or set `fileTree`.

- [ ] **Step 3: Implement pending workspace loading**

Call the new bridge method before committing the frontend project state. On failure, show a warning and leave the previous workspace intact. Add the API type. Make the empty-tree message distinguish an empty project from a global conversation.

```ts
const res = await api.get_workspace_state(targetWorkdir);
if (!res?.ok) {
  this.showToast('进入项目失败: ' + (res?.msg || '未知错误'), 'warn');
  return;
}
this.pendingWorkdir = res.workdir;
this.workdir = res.workdir;
this.currentWorkdirLabel = res.workdir.split('\\').pop() || res.workdir;
this.fileTree = res.file_tree || [];
```

- [ ] **Step 4: Re-run the focused test and verify GREEN**

Run the command from Step 2 and expect PASS.

---

### Task 4: Terminalize Historical Unresolved Tool Calls

**Files:**
- Modify: `frontend/src/utils/toolTrace.test.ts`
- Modify: `frontend/src/components/ToolExecutionTrace.test.ts`
- Modify: `frontend/src/utils/toolTrace.ts`
- Modify: `frontend/src/components/ToolExecutionTrace.vue`

**Interfaces:**
- Consumes: `ExecutionState` from the enclosing assistant message.
- Produces: trace rows whose missing-result status is running only for an active turn.

- [ ] **Step 1: Write failing trace tests**

Assert one unresolved call maps to `running`, `completed`, `cancelled`, and `failed` for enclosing states `running`, `completed`, `cancelled`, and `error`. Mount the trace with a completed state and assert no `.spin` icon exists.

```ts
it('terminalizes unresolved calls with the enclosing turn', () => {
  const call = displayedCall('legacy-empty-id', 'list_directory', 'read');
  expect(buildToolTraceRows([call], [], 'running')[0].status).toBe('running');
  expect(buildToolTraceRows([call], [], 'completed')[0].status).toBe('completed');
  expect(buildToolTraceRows([call], [], 'cancelled')[0].status).toBe('cancelled');
  expect(buildToolTraceRows([call], [], 'error')[0].status).toBe('failed');
});
```

- [ ] **Step 2: Run trace tests and verify RED**

Run: `npm.cmd test -- --run src/utils/toolTrace.test.ts src/components/ToolExecutionTrace.test.ts`

Expected: terminal states still produce a running row and spinner.

- [ ] **Step 3: Implement terminal-state-aware row building**

Pass the enclosing state into `buildToolTraceRows()` and `statusFor()`. Keep completed tool results authoritative; only map calls without results from the outer state.

```ts
function statusFor(call: ToolExecutionCall, state: ExecutionState): ToolCallStatus {
  if (call.result) return call.result.ok ? 'completed' : 'failed';
  if (state === 'cancelled') return 'cancelled';
  if (state === 'error') return 'failed';
  return state === 'completed' ? 'completed' : 'running';
}
```

- [ ] **Step 4: Re-run trace tests and verify GREEN**

Run the command from Step 2 and expect PASS.

---

### Task 5: Full Verification and Delivery

**Files:**
- Modify: `docs/superpowers/plans/2026-08-09-workspace-turn-binding.md`

- [ ] **Step 1: Run Python regression suites**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_llm_events.py tests/test_gui_tool_events.py tests/test_gui_workspace_groups.py -q`

- [ ] **Step 2: Run frontend tests and build**

Run: `npm.cmd test -- --run` and `npm.cmd run build` from `frontend/`.

- [ ] **Step 3: Verify the real GUI flow**

Use a temporary local workspace and mocked bridge to verify a pending project displays files, sends its workdir, and renders a completed unresolved historical call without animation.

- [ ] **Step 4: Check the diff and commit**

Run `git diff --check`, stage only the planned files, and commit with `fix: bind GUI turns to project workspaces`.

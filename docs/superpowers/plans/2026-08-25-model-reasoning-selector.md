# Model Reasoning Selector Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a model-aware reasoning selector beside the model button, persist each model's selection, and guarantee that every explicit reasoning effort is sent with thinking enabled according to that model's official request contract.

**Architecture:** A new backend capability registry is the single source of truth, keyed by normalized model name rather than provider. The model manager persists a generic reasoning choice, the GUI bridge returns a complete capability snapshot, the frontend renders that snapshot, and the request builder encodes the choice through a model-specific strategy with a fail-fast consistency check.

**Tech Stack:** Python 3, pytest, LiteLLM request construction, Vue 3 Composition API, Pinia, TypeScript, Vitest, Vue Test Utils.

**Spec:** `docs/superpowers/specs/2026-08-25-model-reasoning-selector-design.md`

## Global Constraints

- Work only on the `dev` branch; do not merge to another branch.
- Capability selection is keyed by normalized model name, never by provider name.
- Keep “默认”; it means no user override, not a forced effort.
- Fixed-thinking models render “开启” as a disabled button.
- Unknown, unverified, and dynamic-router models do not render the selector.
- A concrete effort must never be sent unless thinking is enabled by an explicit switch, an always-thinking contract, or an effort-as-switch contract.
- Unsupported choices and inconsistent request states fail explicitly; no generic `low/high/max` fallback and no silent mapping.
- Do not add dependencies or change prompt, tools-schema, metadata, cache-affinity, or unrelated request bytes.
- Before request-construction edits, establish a passing baseline for `tests/test_provider_payload_golden.py` and `tests/test_prompt_byte_stability.py`.
- Follow RED → GREEN → REFACTOR for every production behavior.

## File Structure

- Create `core/reasoning_capabilities.py`: immutable capability registry, model-name normalization, snapshot creation, and request-strategy metadata.
- Create `tests/test_reasoning_capabilities.py`: exhaustive capability and normalization tests.
- Modify `core/model_manager.py`: generic reasoning-choice persistence and legacy-state interpretation.
- Modify `gui_bridge.py`: return reasoning snapshots and expose `set_reasoning_choice`.
- Modify `tests/test_config.py` and `tests/test_gui_tool_events.py`: persistence and bridge contract tests.
- Modify `core/llm.py`: model-specific request encoding and consistency guard.
- Modify `tests/test_llm.py` and `tests/test_provider_payload_golden.py`: request-shape and byte-golden tests.
- Modify `frontend/src/types/pywebview.d.ts`: generic reasoning types and bridge method.
- Modify `frontend/src/stores/appStore.ts`: reasoning snapshot state, synchronization, optimistic update, and rollback.
- Modify `frontend/src/stores/appStore.modelSession.test.ts`: store synchronization and race tests.
- Create `frontend/src/components/ReasoningSelector.vue`: reusable button and anchored single-column menu.
- Create `frontend/src/components/ReasoningSelector.test.ts`: component behavior tests.
- Modify `frontend/src/components/InputArea.vue` and `frontend/src/components/KimiEmptyState.vue`: place selector between model and send controls.
- Modify `frontend/src/components/ModelCascader.vue` and `frontend/src/components/ModelCascader.test.ts`: remove the old generic effort row.
- Modify `frontend/src/components/iconPaths.ts` and `frontend/src/components/ComposerControls.test.ts`: add and verify the brain icon and both composer integrations.

---

### Task 1: Model reasoning capability registry

**Files:**
- Create: `core/reasoning_capabilities.py`
- Create: `tests/test_reasoning_capabilities.py`

**Interfaces:**
- Produces: `ReasoningCapability`, `normalize_reasoning_model_name(model_name: str) -> str`, `get_reasoning_capability(model_name: str) -> ReasoningCapability`, `build_reasoning_snapshot(model: dict | None, choice: str = "default") -> dict`.
- Snapshot shape: `{kind, visible, disabled, disabled_reason, choice, options}` where each option is `{value, label}`.

- [ ] **Step 1: Write failing normalization and matrix tests**

```python
from core.reasoning_capabilities import (
    build_reasoning_snapshot,
    get_reasoning_capability,
    normalize_reasoning_model_name,
)


def test_normalizes_transport_prefix_case_and_glm_alias():
    assert normalize_reasoning_model_name("openai/K3-256K") == "k3-256k"
    assert normalize_reasoning_model_name("anthropic/kimi-for-coding") == "kimi-for-coding"
    assert normalize_reasoning_model_name("zai-org/GLM-5.2") == "glm-5.2"


def test_model_specific_effort_options_are_exact():
    expected = {
        "k3": ("default", "low", "high", "max"),
        "deepseek-v4-flash": ("default", "off", "low", "high", "max"),
        "deepseek-v4-pro": ("default", "off", "high", "max"),
        "qwen3.8-max": ("default", "off", "low", "medium", "xhigh"),
        "glm-5.2": ("default", "off", "high", "max"),
        "mimo-v2.5-pro": ("default", "off", "high"),
    }
    for model_name, options in expected.items():
        assert get_reasoning_capability(model_name).options == options


def test_fixed_and_hidden_models_have_distinct_snapshots():
    fixed = build_reasoning_snapshot({"name": "kimi-k2.7-code"})
    assert fixed == {
        "kind": "fixed",
        "visible": True,
        "disabled": True,
        "disabled_reason": "该模型始终开启思考",
        "choice": "on",
        "options": [],
    }
    hidden = build_reasoning_snapshot({"name": "step-router-v1"})
    assert hidden["visible"] is False
    assert hidden["options"] == []
```

- [ ] **Step 2: Run the focused test and verify RED**

Run: `venv\Scripts\python -m pytest tests/test_reasoning_capabilities.py -q`

Expected: FAIL because `core.reasoning_capabilities` does not exist.

- [ ] **Step 3: Implement the immutable registry and snapshot serializer**

```python
from dataclasses import dataclass

LABELS = {
    "default": "默认", "off": "关闭", "on": "开启", "low": "低",
    "medium": "中", "high": "高", "xhigh": "超高", "max": "最高",
}


@dataclass(frozen=True)
class ReasoningCapability:
    kind: str
    options: tuple[str, ...]
    request_strategy: str
    disabled_reason: str = ""


HIDDEN = ReasoningCapability("hidden", (), "hidden")


def normalize_reasoning_model_name(model_name: str) -> str:
    normalized = str(model_name or "").strip().lower()
    prefix, separator, suffix = normalized.partition("/")
    if separator and prefix in {"openai", "anthropic"}:
        normalized = suffix
    if normalized == "zai-org/glm-5.2":
        return "glm-5.2"
    return normalized


def get_reasoning_capability(model_name: str) -> ReasoningCapability:
    return CAPABILITIES.get(normalize_reasoning_model_name(model_name), HIDDEN)
```

Register the complete matrix with this exact data:

```python
CAPABILITIES: dict[str, ReasoningCapability] = {}


def _register(names: tuple[str, ...], capability: ReasoningCapability) -> None:
    for name in names:
        CAPABILITIES[name.lower()] = capability


_register(
    ("k3", "k3-256k", "kimi-k3"),
    ReasoningCapability("effort", ("default", "low", "high", "max"), "always_on_effort"),
)
_register(
    ("deepseek-v4-flash", "deepseek-v4-flash-0731"),
    ReasoningCapability("effort", ("default", "off", "low", "high", "max"), "thinking_object_effort"),
)
_register(
    ("deepseek-v4-pro",),
    ReasoningCapability("effort", ("default", "off", "high", "max"), "thinking_object_effort"),
)
_register(
    ("deepseek-v4-pro-0813",),
    ReasoningCapability("effort", ("default", "off", "low", "high", "max"), "thinking_object_effort"),
)
_register(
    ("qwen3.8-max",),
    ReasoningCapability("effort", ("default", "off", "low", "medium", "xhigh"), "enable_thinking_effort"),
)
_register(
    ("glm-5.2",),
    ReasoningCapability("effort", ("default", "off", "high", "max"), "thinking_object_effort"),
)
_register(
    ("seed-2.1-turbo", "seed-2.1-pro", "doubao-seed-2.0-code"),
    ReasoningCapability("effort", ("default", "off", "low", "medium", "high"), "thinking_object_effort"),
)
_register(
    ("mimo-v2.5-pro",),
    ReasoningCapability("effort", ("default", "off", "high"), "effort_switch"),
)
_register(
    ("kimi-k2.6", "kimi-k2.5", "glm-5", "glm-5.1", "minimax-m3", "longcat-2.0"),
    ReasoningCapability("toggle", ("default", "off", "on"), "thinking_object_toggle"),
)
_register(
    ("qwen3.7-max",),
    ReasoningCapability("toggle", ("default", "off", "on"), "enable_thinking_toggle"),
)
_register(
    ("kimi-for-coding", "kimi-k2.7-code", "minimax-m2.5", "minimax-m2.7"),
    ReasoningCapability("fixed", (), "always_on", "该模型始终开启思考"),
)
_register(
    (
        "deepseek-v4-flash-vision-exp", "agnes-2.0-flash", "agnes-2.5-flash",
        "sensenova-6.7-flash-lite", "step-3.7-flash", "step-explore",
        "step-router-v1", "doubao-seed-evolving", "ark-code-latest",
    ),
    HIDDEN,
)


def build_reasoning_snapshot(model: dict | None, choice: str = "default") -> dict:
    model_name = str((model or {}).get("litellm_model") or (model or {}).get("name") or "")
    capability = get_reasoning_capability(model_name)
    fixed = capability.kind == "fixed"
    return {
        "kind": capability.kind,
        "visible": capability.kind != "hidden",
        "disabled": fixed,
        "disabled_reason": capability.disabled_reason,
        "choice": "on" if fixed else choice,
        "options": [
            {"value": value, "label": LABELS[value]}
            for value in capability.options
        ],
    }
```

- [ ] **Step 4: Run focused tests and verify GREEN**

Run: `venv\Scripts\python -m pytest tests/test_reasoning_capabilities.py -q`

Expected: PASS.

- [ ] **Step 5: Commit the capability registry**

```bash
git add core/reasoning_capabilities.py tests/test_reasoning_capabilities.py
git commit -m "feat: add model reasoning capability registry"
```

---

### Task 2: Persist generic choices and expose bridge snapshots

**Files:**
- Modify: `core/model_manager.py:171-212`
- Modify: `gui_bridge.py:1898-1910,2366-2378,2480-2539`
- Modify: `tests/test_config.py:217-246`
- Modify: `tests/test_gui_tool_events.py:469-510`

**Interfaces:**
- Consumes: `get_reasoning_capability`, `build_reasoning_snapshot` from Task 1.
- Produces: `get_model_reasoning_choice(model_id: str) -> str`, `set_model_reasoning_choice(model_id: str, choice: str) -> tuple[bool, str]`, `ApiBridge.set_reasoning_choice(choice: str) -> dict`.
- Bridge responses add `reasoning: ReasoningSnapshot`; the old `reasoning_effort` response field is removed from frontend-facing responses.

- [ ] **Step 1: Replace old persistence tests with failing generic-choice tests**

```python
def test_set_model_reasoning_choice_persists_effort_with_thinking_enabled(tmp_path, monkeypatch):
    data = _provider_config()
    data["providers"][0]["models"][0]["name"] = "deepseek-v4-flash"
    data["providers"][0]["models"][0]["litellm_model"] = "openai/deepseek-v4-flash"
    cfg = _use_tmp_config(tmp_path, monkeypatch, data)
    ok, _ = model_manager.set_model_reasoning_choice("m1", "high")
    assert ok
    model = json.loads(cfg.read_text(encoding="utf-8"))["providers"][0]["models"][0]
    assert model["reasoning_choice"] == "high"
    assert model["enable_thinking"] is True
    assert model["reasoning_effort"] == "high"


def test_off_clears_stale_effort_and_default_clears_all_overrides(tmp_path, monkeypatch):
    data = _provider_config()
    data["providers"][0]["models"][0]["name"] = "deepseek-v4-flash"
    data["providers"][0]["models"][0]["litellm_model"] = "openai/deepseek-v4-flash"
    cfg = _use_tmp_config(tmp_path, monkeypatch, data)
    assert model_manager.set_model_reasoning_choice("m1", "max")[0]
    assert model_manager.set_model_reasoning_choice("m1", "off")[0]
    model = json.loads(cfg.read_text(encoding="utf-8"))["providers"][0]["models"][0]
    assert model["reasoning_choice"] == "off"
    assert model["enable_thinking"] is False
    assert "reasoning_effort" not in model
    assert model_manager.set_model_reasoning_choice("m1", "default")[0]
    model = json.loads(cfg.read_text(encoding="utf-8"))["providers"][0]["models"][0]
    assert "reasoning_choice" not in model
    assert "enable_thinking" not in model
    assert "reasoning_effort" not in model


def test_fixed_model_rejects_changes_without_writing(tmp_path, monkeypatch):
    data = _provider_config()
    data["providers"][0]["models"][0]["name"] = "kimi-k2.7-code"
    data["providers"][0]["models"][0]["litellm_model"] = "openai/kimi-k2.7-code"
    cfg = _use_tmp_config(tmp_path, monkeypatch, data)
    before = cfg.read_bytes()
    ok, message = model_manager.set_model_reasoning_choice("m1", "off")
    assert not ok
    assert "始终开启" in message
    assert cfg.read_bytes() == before
```

- [ ] **Step 2: Run persistence tests and verify RED**

Run: `venv\Scripts\python -m pytest tests/test_config.py -k reasoning -q`

Expected: FAIL because the generic-choice functions do not exist.

- [ ] **Step 3: Implement atomic choice persistence and legacy interpretation**

Use one `config.update_config` mutation. The exact state transitions are:

```python
if normalized == "default":
    model.pop("reasoning_choice", None)
    model.pop("enable_thinking", None)
    model.pop("reasoning_effort", None)
elif normalized == "off":
    model["reasoning_choice"] = "off"
    model["enable_thinking"] = False
    model.pop("reasoning_effort", None)
elif normalized == "on":
    model["reasoning_choice"] = "on"
    model["enable_thinking"] = True
    model.pop("reasoning_effort", None)
else:
    model["reasoning_choice"] = normalized
    model["enable_thinking"] = True
    model["reasoning_effort"] = normalized
```

`get_model_reasoning_choice` resolves old configurations in this order: valid `reasoning_choice`; `enable_thinking is False` → `off`; valid `reasoning_effort`; `enable_thinking is True` with an `on` option → `on`; otherwise `default`. An invalid stored value raises `ValueError` with the model name and value.

- [ ] **Step 4: Run persistence tests and verify GREEN**

Run: `venv\Scripts\python -m pytest tests/test_config.py -k reasoning -q`

Expected: PASS.

- [ ] **Step 5: Write failing bridge snapshot tests**

```python
def test_bridge_set_reasoning_choice_returns_snapshot(monkeypatch):
    bridge = ApiBridge.__new__(ApiBridge)
    monkeypatch.setattr("gui_bridge.load_config", lambda: {"active_model_id": "m1"})
    monkeypatch.setattr("gui_bridge.set_model_reasoning_choice", lambda _id, _choice: (True, "ok"))
    monkeypatch.setattr("gui_bridge.get_active_model", lambda: {"id": "m1", "name": "qwen3.8-max"})
    result = bridge.set_reasoning_choice("medium")
    assert result["ok"] is True
    assert result["reasoning"]["choice"] == "medium"
    assert [item["value"] for item in result["reasoning"]["options"]] == [
        "default", "off", "low", "medium", "xhigh",
    ]
```

Also assert that `get_initial_state`, `set_model`, and `switch_session` return a `reasoning` snapshot for their effective model.

- [ ] **Step 6: Run bridge tests and verify RED**

Run: `venv\Scripts\python -m pytest tests/test_gui_tool_events.py -k reasoning -q`

Expected: FAIL because `set_reasoning_choice` and snapshot responses are missing.

- [ ] **Step 7: Implement bridge snapshot responses**

Add a private helper with one contract:

```python
def _reasoning_snapshot_for_model(model: dict | None) -> dict:
    snapshot = build_reasoning_snapshot(model)
    if model and snapshot["kind"] not in {"fixed", "hidden"}:
        snapshot["choice"] = get_model_reasoning_choice(str(model.get("id") or ""))
    return snapshot
```

Use it in initialization, model switching, session switching, and `set_reasoning_choice`. Do not catch capability validation errors inside the capability layer; the bridge converts them to `{ok: False, msg}` at its existing API boundary.

- [ ] **Step 8: Run bridge and persistence tests**

Run: `venv\Scripts\python -m pytest tests/test_config.py tests/test_gui_tool_events.py -q`

Expected: PASS.

- [ ] **Step 9: Commit persistence and bridge changes**

```bash
git add core/model_manager.py gui_bridge.py tests/test_config.py tests/test_gui_tool_events.py
git commit -m "feat: persist model reasoning choices"
```

---

### Task 3: Encode model-specific requests with an enablement invariant

**Files:**
- Modify: `core/llm.py:2709-2888`
- Modify: `tests/test_llm.py:103-325`
- Modify: `tests/test_provider_payload_golden.py:58-166`

**Interfaces:**
- Consumes: `ReasoningCapability.request_strategy`, `get_reasoning_capability`, and the persisted `reasoning_choice`.
- Produces: `_apply_reasoning_choice(result: dict, model_name: str, model_cfg: dict) -> None` and `_assert_reasoning_request_consistent(result: dict, capability: ReasoningCapability) -> None`.

- [ ] **Step 1: Establish the mandatory byte-stability baseline before editing request code**

Run: `venv\Scripts\python -m pytest tests/test_provider_payload_golden.py tests/test_prompt_byte_stability.py -q`

Expected: PASS before any `core/llm.py` modification.

- [ ] **Step 2: Write failing request-strategy tests**

```python
@pytest.mark.parametrize(
    ("model", "choice", "expected"),
    [
        ("openai/kimi-k3", "high", {"extra_body": {"reasoning_effort": "high"}}),
        ("openai/deepseek-v4-flash", "high", {
            "extra_body": {"thinking": {"type": "enabled"}},
            "reasoning_effort": "high",
            "allowed_openai_params": ["reasoning_effort"],
        }),
        ("openai/qwen3.8-max", "medium", {
            "extra_body": {"enable_thinking": True, "reasoning_effort": "medium"},
        }),
        ("openai/glm-5.2", "max", {
            "extra_body": {"thinking": {"type": "enabled"}},
            "reasoning_effort": "max",
            "allowed_openai_params": ["reasoning_effort"],
        }),
        ("openai/kimi-k2.6", "off", {
            "extra_body": {"thinking": {"type": "disabled"}},
        }),
        ("openai/LongCat-2.0", "on", {
            "extra_body": {"thinking": {"type": "enabled"}},
        }),
        ("openai/mimo-v2.5-pro", "off", {
            "reasoning_effort": "none",
            "allowed_openai_params": ["reasoning_effort"],
        }),
    ],
)
def test_model_reasoning_choice_uses_exact_request_contract(monkeypatch, model, choice, expected):
    monkeypatch.setattr(
        "core.model_manager.get_model_config_by_litellm_model",
        lambda *_args, **_kwargs: {"reasoning_choice": choice},
    )
    result = _provider_request_options(model, "https://gateway.example/v1")
    for key, value in expected.items():
        assert result[key] == value


def test_effort_without_enabled_thinking_fails_before_transport():
    capability = get_reasoning_capability("deepseek-v4-flash")
    with pytest.raises(ValueError, match="思考强度.*未开启思考"):
        _assert_reasoning_request_consistent({"reasoning_effort": "high"}, capability)
```

Add off/on tests for Qwen 3.7, GLM 5/5.1, MiniMax M3, Seed 2.1, and Doubao Seed 2.0 Code. Add a default test proving no user override field is added. Add fixed-model tests proving the existing Kimi coding request contract remains enabled.

- [ ] **Step 3: Run focused request tests and verify RED**

Run: `venv\Scripts\python -m pytest tests/test_llm.py -k "reasoning_choice or request_contract or enabled_thinking" -q`

Expected: FAIL because the strategy encoder and invariant do not exist.

- [ ] **Step 4: Implement the strategy encoder**

Encode exact strategies as follows:

```python
if strategy == "always_on_effort":
    extra_body["reasoning_effort"] = choice
elif strategy == "thinking_object_effort":
    extra_body["thinking"] = {"type": "disabled" if choice == "off" else "enabled"}
    if choice not in {"off", "default"}:
        result["reasoning_effort"] = choice
        result["allowed_openai_params"] = ["reasoning_effort"]
elif strategy == "enable_thinking_effort":
    extra_body["enable_thinking"] = choice != "off"
    if choice not in {"off", "default"}:
        extra_body["reasoning_effort"] = choice
elif strategy == "effort_switch":
    result["reasoning_effort"] = "none" if choice == "off" else choice
    result["allowed_openai_params"] = ["reasoning_effort"]
elif strategy == "thinking_object_toggle":
    extra_body["thinking"] = {"type": "disabled" if choice == "off" else "enabled"}
elif strategy == "enable_thinking_toggle":
    extra_body["enable_thinking"] = choice != "off"
```

For `default`, return before adding user override fields and retain the existing model defaults, cache-affinity fields, token budgets, and output limits. For `always_on` and `hidden`, reject an explicit persisted choice instead of ignoring it.

Run `_assert_reasoning_request_consistent` after all provider options are assembled. Treat `always_on_effort`, `always_on`, and `effort_switch` as enabled-by-contract; all other effort strategies must contain their expected enabled flag.

- [ ] **Step 5: Run focused request tests and verify GREEN**

Run: `venv\Scripts\python -m pytest tests/test_llm.py -k "reasoning or kimi or deepseek" -q`

Expected: PASS.

- [ ] **Step 6: Add byte-golden cases for changed provider payloads**

Use a helper that serializes with `ensure_ascii=False`, `sort_keys=True`, and `separators=(",", ":")`. Assert these exact payload bytes after monkeypatching the model config to the named choice:

```python
assert encode(_provider_request_options(
    "openai/deepseek-v4-flash", "https://gateway.example/v1"
)) == b'{"allowed_openai_params":["reasoning_effort"],"extra_body":{"thinking":{"type":"enabled"}},"reasoning_effort":"high"}'

assert encode(_provider_request_options(
    "openai/qwen3.8-max", "https://gateway.example/v1"
)) == b'{"extra_body":{"enable_thinking":true,"reasoning_effort":"medium"}}'

assert encode(_provider_request_options(
    "openai/glm-5.2", "https://gateway.example/v1"
)) == b'{"allowed_openai_params":["reasoning_effort"],"extra_body":{"thinking":{"type":"enabled"}},"reasoning_effort":"max"}'

assert encode(_provider_request_options(
    "openai/LongCat-2.0", "https://gateway.example/v1"
)) == b'{"extra_body":{"thinking":{"type":"disabled"}}}'
```

The four tests set choices to `high`, `medium`, `max`, and `off`, respectively. Do not alter unrelated existing golden strings.

- [ ] **Step 7: Run request golden and prompt stability suites**

Run: `venv\Scripts\python -m pytest tests/test_provider_payload_golden.py tests/test_prompt_byte_stability.py tests/test_llm.py -q`

Expected: PASS with unchanged prompt/tool byte tests.

- [ ] **Step 8: Commit request encoding**

```bash
git add core/llm.py tests/test_llm.py tests/test_provider_payload_golden.py
git commit -m "feat: encode model reasoning request contracts"
```

---

### Task 4: Frontend reasoning contract and store synchronization

**Files:**
- Modify: `frontend/src/types/pywebview.d.ts:14-25,220-245,336-346`
- Modify: `frontend/src/stores/appStore.ts:280-330,1128-1142,1217-1265,1465-1475`
- Modify: `frontend/src/stores/appStore.modelSession.test.ts`

**Interfaces:**
- Consumes: bridge `reasoning` snapshot from Task 2.
- Produces: `ReasoningChoice`, `ReasoningOption`, `ReasoningSnapshot`, store state `reasoning`, and action `setReasoningChoice(choice: ReasoningChoice)`.

- [ ] **Step 1: Add failing store synchronization and rollback tests**

```typescript
it('setReasoningChoice applies the normalized backend snapshot', async () => {
  const setChoice = vi.fn(async () => ({
    ok: true,
    reasoning: {
      kind: 'effort', visible: true, disabled: false, disabled_reason: '',
      choice: 'medium', options: [
        { value: 'default', label: '默认' },
        { value: 'medium', label: '中' },
      ],
    },
  }));
  mockBridgeApi({ set_reasoning_choice: setChoice });
  const store = useAppStore();
  await store.setReasoningChoice('medium');
  expect(setChoice).toHaveBeenCalledWith('medium');
  expect(store.reasoning.choice).toBe('medium');
});

it('setReasoningChoice restores the previous snapshot on failure', async () => {
  mockBridgeApi({ set_reasoning_choice: vi.fn(async () => ({ ok: false, msg: '不支持' })) });
  const store = useAppStore();
  store.reasoning = reasoningSnapshot('high');
  await store.setReasoningChoice('off');
  expect(store.reasoning.choice).toBe('high');
});
```

Update existing model/session mocks to return `reasoning` snapshots and assert initialization, `setModel`, and `switchSession` replace the complete snapshot.

- [ ] **Step 2: Run store tests and verify RED**

Run: `cd frontend && npm test -- src/stores/appStore.modelSession.test.ts`

Expected: FAIL because generic reasoning types and store action do not exist.

- [ ] **Step 3: Implement frontend types and store action**

```typescript
export type ReasoningChoice = 'default' | 'off' | 'on' | 'low' | 'medium' | 'high' | 'xhigh' | 'max';
export interface ReasoningOption { value: ReasoningChoice; label: string }
export interface ReasoningSnapshot {
  kind: 'effort' | 'toggle' | 'fixed' | 'hidden';
  visible: boolean;
  disabled: boolean;
  disabled_reason: string;
  choice: ReasoningChoice;
  options: ReasoningOption[];
}
```

Initialize store state with a hidden snapshot. Replace `reasoningEffort` with `reasoning`. `setReasoningChoice` clones the old snapshot, updates only `choice` optimistically, calls `api.set_reasoning_choice(choice)`, replaces the full snapshot on success, and restores the clone on failure. Initialization and model/session switching replace the full snapshot returned by the backend.

- [ ] **Step 4: Run store tests and verify GREEN**

Run: `cd frontend && npm test -- src/stores/appStore.modelSession.test.ts`

Expected: PASS.

- [ ] **Step 5: Run TypeScript checking through the production build**

Run: `cd frontend && npm run build`

Expected: PASS with no references to `ReasoningEffort` or `reasoningEffort`.

- [ ] **Step 6: Commit frontend contract changes**

```bash
git add frontend/src/types/pywebview.d.ts frontend/src/stores/appStore.ts frontend/src/stores/appStore.modelSession.test.ts
git commit -m "feat: synchronize model reasoning state"
```

---

### Task 5: Reasoning selector component and composer integration

**Files:**
- Create: `frontend/src/components/ReasoningSelector.vue`
- Create: `frontend/src/components/ReasoningSelector.test.ts`
- Modify: `frontend/src/components/InputArea.vue`
- Modify: `frontend/src/components/KimiEmptyState.vue`
- Modify: `frontend/src/components/ModelCascader.vue`
- Modify: `frontend/src/components/ModelCascader.test.ts`
- Modify: `frontend/src/components/iconPaths.ts`
- Modify: `frontend/src/components/ComposerControls.test.ts`

**Interfaces:**
- Consumes: `store.reasoning`, `store.setReasoningChoice` from Task 4.
- Produces: `<ReasoningSelector variant="compact | hero" />` with internal anchored menu state.

- [ ] **Step 1: Write failing selector behavior tests**

```typescript
it('renders fixed thinking as a disabled enabled button', () => {
  const store = useAppStore();
  store.reasoning = {
    kind: 'fixed', visible: true, disabled: true,
    disabled_reason: '该模型始终开启思考', choice: 'on', options: [],
  };
  const wrapper = mount(ReasoningSelector);
  const button = wrapper.get('[data-test="reasoning-button"]');
  expect(button.text()).toContain('开启');
  expect(button.attributes('disabled')).toBeDefined();
  expect(button.attributes('title')).toBe('该模型始终开启思考');
});

it('renders only the current model options and selects one', async () => {
  const store = useAppStore();
  store.reasoning = {
    kind: 'effort', visible: true, disabled: false, disabled_reason: '', choice: 'high',
    options: [
      { value: 'default', label: '默认' },
      { value: 'off', label: '关闭' },
      { value: 'high', label: '高' },
      { value: 'max', label: '最高' },
    ],
  };
  const spy = vi.spyOn(store, 'setReasoningChoice').mockResolvedValue(undefined);
  const wrapper = mount(ReasoningSelector, { global: { stubs: { teleport: true } } });
  await wrapper.get('[data-test="reasoning-button"]').trigger('click');
  expect(wrapper.findAll('[data-test="reasoning-option"]').map(node => node.text())).toEqual([
    '默认', '关闭', '高', '最高',
  ]);
  await wrapper.findAll('[data-test="reasoning-option"]')[3].trigger('click');
  expect(spy).toHaveBeenCalledWith('max');
});

it('does not render hidden capability', () => {
  useAppStore().reasoning = hiddenReasoningSnapshot();
  expect(mount(ReasoningSelector).find('[data-test="reasoning-button"]').exists()).toBe(false);
});
```

- [ ] **Step 2: Run selector tests and verify RED**

Run: `cd frontend && npm test -- src/components/ReasoningSelector.test.ts`

Expected: FAIL because the component does not exist.

- [ ] **Step 3: Implement `ReasoningSelector.vue`**

The template contract is:

```vue
<button
  v-if="store.reasoning.visible"
  ref="buttonRef"
  data-test="reasoning-button"
  class="reasoning-button"
  :class="`reasoning-${variant}`"
  :disabled="store.reasoning.disabled"
  :title="buttonTitle"
  @click.stop="toggleMenu"
>
  <AppIcon name="brain" :size="14" />
  <span>{{ currentLabel }}</span>
  <AppIcon v-if="!store.reasoning.disabled" name="chevron-down" :size="10" />
</button>
<Teleport to="body">
  <div v-if="open" class="reasoning-menu" :style="menuStyle" @click.stop>
    <button
      v-for="option in store.reasoning.options"
      :key="option.value"
      data-test="reasoning-option"
      class="reasoning-option"
      :class="{ active: option.value === store.reasoning.choice }"
      @click="select(option.value)"
    >
      <span>{{ option.label }}</span>
      <AppIcon v-if="option.value === store.reasoning.choice" name="check" :size="14" />
    </button>
  </div>
</Teleport>
```

Position above the anchor when the button is in the lower half of the viewport, otherwise below it. Clamp the menu inside 10 px viewport padding. Close on outside click, model ID change, component unmount, or selection. Use theme variables and the compact single-column visual from the approved screenshot.

Add a Lucide-style `brain` path to `iconPaths.ts`:

```typescript
brain: '<path d="M9.5 4A2.5 2.5 0 0 0 7 6.5v.5a3 3 0 0 0-2 5.83V13a3 3 0 0 0 2 5.83v.67A2.5 2.5 0 0 0 9.5 22H12V2H9.5A2.5 2.5 0 0 0 7 4.5"/><path d="M14.5 4A2.5 2.5 0 0 1 17 6.5v.5a3 3 0 0 1 2 5.83V13a3 3 0 0 1-2 5.83v.67a2.5 2.5 0 0 1-2.5 2.5H12V2h2.5A2.5 2.5 0 0 1 17 4.5"/><path d="M8 10h4"/><path d="M12 14h4"/>',
```

- [ ] **Step 4: Run selector tests and verify GREEN**

Run: `cd frontend && npm test -- src/components/ReasoningSelector.test.ts`

Expected: PASS.

- [ ] **Step 5: Write failing integration tests for both composers and old-row removal**

In `ComposerControls.test.ts`, set a visible capability and assert DOM order:

```typescript
const model = wrapper.get('[data-test="model-button"]').element;
const reasoning = wrapper.get('[data-test="reasoning-button"]').element;
const send = wrapper.get('[data-test="send-button"]').element;
expect(model.compareDocumentPosition(reasoning) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
expect(reasoning.compareDocumentPosition(send) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
```

In `ModelCascader.test.ts`, assert `.effort-row` does not exist even when models are present.

- [ ] **Step 6: Run integration tests and verify RED**

Run: `cd frontend && npm test -- src/components/ComposerControls.test.ts src/components/ModelCascader.test.ts`

Expected: FAIL because the selector is not mounted, button test IDs are absent, and the old effort row remains.

- [ ] **Step 7: Integrate the selector and remove the old row**

- Import and mount `<ReasoningSelector variant="compact" />` after the model button in `InputArea.vue`.
- Import and mount `<ReasoningSelector variant="hero" />` after the model button in `KimiEmptyState.vue`.
- Add `data-test="model-button"` and `data-test="send-button"` to the respective existing controls.
- Ensure opening model, permission, or reasoning menus closes the other local menu state; model changes also close the selector internally.
- Delete the effort template, `effortOptions`, `selectEffort`, type import, and `.effort-*` CSS from `ModelCascader.vue`.

- [ ] **Step 8: Run component tests and build**

Run: `cd frontend && npm test -- src/components/ReasoningSelector.test.ts src/components/ComposerControls.test.ts src/components/ModelCascader.test.ts src/components/InputArea.test.ts src/components/KimiEmptyState.test.ts`

Expected: PASS.

Run: `cd frontend && npm run build`

Expected: PASS.

- [ ] **Step 9: Commit selector UI**

```bash
git add frontend/src/components/ReasoningSelector.vue frontend/src/components/ReasoningSelector.test.ts frontend/src/components/InputArea.vue frontend/src/components/KimiEmptyState.vue frontend/src/components/ModelCascader.vue frontend/src/components/ModelCascader.test.ts frontend/src/components/iconPaths.ts frontend/src/components/ComposerControls.test.ts
git commit -m "feat: add model-aware reasoning selector"
```

---

### Task 6: Full regression verification

**Files:**
- Modify only files needed to fix failures directly caused by Tasks 1-5.

**Interfaces:**
- Consumes: all backend and frontend behavior from Tasks 1-5.
- Produces: verified feature with no unrelated regressions.

- [ ] **Step 1: Run the complete Python test suite**

Run: `venv\Scripts\python -m pytest -q`

Expected: PASS.

- [ ] **Step 2: Run the complete frontend test suite**

Run: `cd frontend && npm test`

Expected: PASS.

- [ ] **Step 3: Run production frontend build and syntax compilation**

Run: `cd frontend && npm run build`

Expected: PASS.

Run: `venv\Scripts\python -m py_compile main.py core/llm.py core/model_manager.py core/reasoning_capabilities.py gui_bridge.py`

Expected: exit code 0.

- [ ] **Step 4: Re-run strict request and prompt guards**

Run: `venv\Scripts\python -m pytest tests/test_provider_payload_golden.py tests/test_prompt_byte_stability.py -q`

Expected: PASS.

- [ ] **Step 5: Inspect the final diff and repository state**

Run: `git diff --check`

Expected: no whitespace errors.

Run: `git status --short`

Expected: feature files are clean after their task commits. Preserve the pre-existing unrelated changes in `tests/test_setup_engine.py`, `ycode_setup.py`, and `tmp_gui_smoke.py`, plus any pre-existing `tmp/` content; do not stage, edit, or delete them.

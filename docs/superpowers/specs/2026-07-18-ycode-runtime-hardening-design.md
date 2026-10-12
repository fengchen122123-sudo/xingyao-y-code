# Y-CODE Agent Runtime Hardening Design

## Goal

Reduce unproductive K3 reasoning at its source, preserve provider-native reasoning semantics, make subagent timeouts exclude human approval waits, expose stage diagnostics, and execute Windows commands without parser or encoding corruption. The startup layout stays unchanged except for the version string.

## Confirmed Causes

1. `core.skills.get_active_skills_markdown()` injects the full contents of 17 enabled skills, roughly 142 KB, into every root prompt. The model repeatedly evaluates unrelated workflow rules before acting.
2. K3 returns Anthropic `thinking_blocks` with a signature, but `core.llm` stores the reasoning inside ordinary `<think>` assistant text and drops the signed blocks before the next tool round.
3. Foreground subagent timeout uses one wall-clock deadline, so time waiting for a user approval consumes the execution budget.
4. Skill dependency inspection parses every Windows shell command with `shlex`, even when no skill script exists in the command.
5. Foreground command output is forced through UTF-8 although Windows internal commands may write the active OEM code page.

## Architecture

### Skill Catalog

The runtime prompt contains only enabled skill name, description, and absolute `SKILL.md` path. The full body is never injected automatically. The decision policy tells the model to read exactly the directly relevant skill before using it and to ignore unrelated skills.

### Decision Policy

`core.context_builder` owns a compact, stable policy appended to both default and user-customized prompts. Explicit simple tasks receive one decision pass followed by action. The policy forbids revisiting rejected options and irrelevant tools without new evidence.

### Reasoning Envelope

`core.llm` keeps three values separate during a streamed assistant step:

- public content for the assistant message;
- `reasoning_content` for display and structured persistence;
- provider `thinking_blocks` for same-turn API round-tripping.

Kimi Coding K3 requests use Anthropic-native `thinking.type="enabled"` with LiteLLM's high budget (4096 tokens), a 32000-token response ceiling, and a K3-only `allowed_openai_params=["thinking"]` declaration because LiteLLM's bundled model catalog does not recognize the K3 identifier. This is the Anthropic-protocol equivalent of Kimi CLI's high effort; sending the OpenAI-only `reasoning_effort` field is invalid for this endpoint. The configured endpoint remains unchanged because it already returns signed thinking blocks. No model/provider migration is performed.

### Permission-Aware Subagent Budget

`PermissionBroker` exposes whether an owner has an active or queued request. `SubagentManager` accumulates execution time and approval-wait time separately. Only execution time can trigger the foreground timeout. Task snapshots contain a bounded stage history and both elapsed counters.

### Windows Process I/O

Dependency inspection only tokenizes commands that actually contain a `.py` candidate. Shell syntax errors remain the shell's responsibility. Process output is captured as bytes and decoded deterministically: BOM, strict UTF-8, then the Windows OEM code page (or the platform preferred encoding elsewhere).

## UI Contract

- Native reasoning remains grey and streams by stable sentence blocks.
- Tool activity remains concise and does not print file contents.
- Public answers remain the normal bright text with terminal Markdown rendering.
- `thinking` is shown only between native reasoning boundary events; other active work is `executing`.
- No logo, welcome card, input box, theme, or status layout change is allowed.

## Failure Semantics

- Missing or invalid skill metadata is surfaced by the skill scanner; unrelated shell commands are not rejected by the optional dependency preflight.
- A subagent timeout reports its latest stage, execution seconds, and approval-wait seconds.
- A late worker cannot overwrite a timed-out or cancelled terminal state.
- Provider thinking without a signature is still displayed, but only complete signed blocks are round-tripped as Anthropic history.

## Verification

Automated acceptance requires focused red-green tests, the complete pytest suite, `py_compile`, source/version assertions, prompt-size assertions, and a real K3 smoke request that proves signed thinking blocks survive a tool round. The release version is `v1.0.12`.

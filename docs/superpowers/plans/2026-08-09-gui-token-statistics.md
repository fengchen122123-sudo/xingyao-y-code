# GUI Token Statistics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 GUI 设置页增加与 CLI 同源的 Token 统计页面，用每日多模型圆环图、汇总卡片和模型明细展示今日/近 7 天/近 30 天真实用量。

**Architecture:** `MemoryManager` 增加按日期与模型的聚合查询，`ApiBridge` 负责补齐日期、计算模型/总体汇总并提供 JSON 契约；Pinia store 管理加载状态，独立 Vue 组件负责统计页面和原生 CSS 圆环渲染。CLI 原有 `/usage` 与路由级查询不改动。

**Tech Stack:** Python 3、SQLite、PyWebView bridge、Vue 3、Pinia、TypeScript、Vitest、CSS `conic-gradient`。

## Global Constraints

- 统计只使用 `token_usage` 表中的服务端真实 usage，不估算缺失数据。
- 时间范围仅允许 `1`、`7`、`30` 天，日期使用本机时区并补齐无记录日期。
- 缓存命中率分母仅包含 `cached_tokens IS NOT NULL` 的输入 Token。
- 不引入第三方图表依赖，不增加费用估算、自定义日期或导出。
- GUI 与 CLI 共用同一数据库和输入/输出/缓存/合计字段定义。

---

### Task 1: Daily Model Aggregation

**Files:**
- Modify: `core/memory.py`
- Modify: `tests/test_token_usage.py`

**Interfaces:**
- Consumes: `token_usage(date, provider, model, prompt_tokens, completion_tokens, cached_tokens, total_tokens)`。
- Produces: `MemoryManager.query_token_usage_by_day_and_model(start_date, end_date) -> list[dict]`。

- [ ] **Step 1: Write the failing aggregation test**

```python
def test_query_token_usage_by_day_and_model_keeps_days_and_models_separate():
    manager = MemoryManager(":memory:")
    manager.add_token_usage("openai", "m1", 100, 10, 90, 110, ts=_ts_of("2026-08-08"))
    manager.add_token_usage("openai", "m1", 200, 20, None, 220, ts=_ts_of("2026-08-08"))
    manager.add_token_usage("moonshot", "m2", 300, 30, 250, 330, ts=_ts_of("2026-08-09"))

    rows = manager.query_token_usage_by_day_and_model("2026-08-08", "2026-08-09")

    assert rows[0] == {
        "date": "2026-08-08", "provider": "openai", "model": "m1",
        "prompt": 300, "completion": 30, "cached": 90,
        "cache_prompt": 100, "total": 330, "requests": 2,
    }
    assert rows[1]["date"] == "2026-08-09"
    assert rows[1]["requests"] == 1
```

- [ ] **Step 2: Run the test and verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_token_usage.py -k day_and_model -q -p no:cacheprovider`

Expected: FAIL with `AttributeError` because the query does not exist.

- [ ] **Step 3: Implement the query**

Add one locked SQL query grouped by `date, provider, model`, ordered by date ascending and daily total descending. Return exact integers and `COUNT(*) AS requests`; compute `cache_prompt` with the same conditional sum used by `query_token_usage_by_model`.

- [ ] **Step 4: Run the focused test and verify GREEN**

Run the Step 2 command and expect PASS.

---

### Task 2: GUI Statistics Bridge Contract

**Files:**
- Modify: `gui_bridge.py`
- Create: `tests/test_gui_token_statistics.py`

**Interfaces:**
- Consumes: `query_token_usage_by_day_and_model(start_date, end_date)`。
- Produces: `ApiBridge.get_token_usage_statistics(days: int) -> {ok, range, summary, models, days}`。

- [ ] **Step 1: Write failing bridge tests**

Create a bridge with an in-memory manager and records for today and yesterday. Assert:

```python
result = bridge.get_token_usage_statistics(7)
assert result["ok"] is True
assert len(result["days"]) == 7
assert result["days"][-1]["date"] == date.today().isoformat()
assert result["summary"]["total"] == 660
assert result["summary"]["requests"] == 2
assert result["models"][0]["key"] == "openai/m1"
assert result["summary"]["cache_hit_rate"] == 85.0
```

Also assert `get_token_usage_statistics(14)` returns `ok=False` and does not query the manager.

- [ ] **Step 2: Run tests and verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_gui_token_statistics.py -q -p no:cacheprovider`

Expected: FAIL because the bridge method does not exist.

- [ ] **Step 3: Implement the bridge method**

Validate `days in (1, 7, 30)`. Calculate inclusive local start/end dates. Aggregate rows into:

```python
{
    "ok": True,
    "range": {"days": 7, "start_date": "...", "end_date": "..."},
    "summary": {"prompt": 0, "completion": 0, "cached": 0,
                "cache_prompt": 0, "total": 0, "requests": 0,
                "cache_hit_rate": None},
    "models": [{"key": "provider/model", ...}],
    "days": [{"date": "...", "prompt": 0, "completion": 0,
              "cached": 0, "cache_prompt": 0, "total": 0,
              "requests": 0, "models": []}],
}
```

Sort models by total descending. Compute cache hit rate only when `cache_prompt > 0`, rounded to one decimal. Log and return `{ok: False, msg}` on query errors.

- [ ] **Step 4: Run bridge tests and verify GREEN**

Run the Step 2 command and expect PASS.

---

### Task 3: Store State and Donut View Model

**Files:**
- Modify: `frontend/src/types/pywebview.d.ts`
- Modify: `frontend/src/stores/appStore.ts`
- Create: `frontend/src/stores/appStore.tokenStatistics.test.ts`
- Create: `frontend/src/utils/tokenStatistics.ts`
- Create: `frontend/src/utils/tokenStatistics.test.ts`

**Interfaces:**
- Consumes: `PyWebViewAPI.get_token_usage_statistics(days)`。
- Produces: store fields `tokenStatistics`, `tokenStatisticsLoading`, `tokenStatisticsDays`; action `loadTokenStatistics(days, force)`; helpers `modelColor(key)`, `donutGradient(models)` and `formatTokenCount(value)`。

- [ ] **Step 1: Write failing store tests**

Mock the bridge response, call `loadTokenStatistics(7)`, and assert the API receives `7`, the selected range changes only after success, and loading resets. Add a failure test proving the previous snapshot and range remain unchanged when `{ok: false}` is returned.

- [ ] **Step 2: Write failing utility tests**

Assert one model produces a full-color gradient, two models produce proportional ranges totaling 100%, zero total produces the empty ring color, and `modelColor("openai/m1")` is stable across calls.

- [ ] **Step 3: Run focused frontend tests and verify RED**

Run: `npm.cmd test -- --run src/stores/appStore.tokenStatistics.test.ts src/utils/tokenStatistics.test.ts`

Expected: FAIL because state, action and helpers do not exist.

- [ ] **Step 4: Implement types, store action and helpers**

Define explicit `TokenStatistics*` interfaces. In the store, preserve the previous snapshot on failure, show the existing warning toast, and always clear loading in `finally`. Implement deterministic FNV-style string hashing over a fixed accessible palette. Build `conic-gradient` stops from cumulative totals without mutating input.

- [ ] **Step 5: Re-run focused frontend tests and verify GREEN**

Run the Step 3 command and expect PASS.

---

### Task 4: Token Statistics Settings Page

**Files:**
- Create: `frontend/src/components/TokenUsageSettings.vue`
- Create: `frontend/src/components/TokenUsageSettings.test.ts`
- Modify: `frontend/src/components/SettingsPage.vue`
- Modify: `frontend/src/components/SettingsPage.test.ts`

**Interfaces:**
- Consumes: token statistics store state/actions and `tokenStatistics.ts` helpers。
- Produces: settings navigation entry `data-test="token-statistics-nav"` and page `data-test="token-statistics-page"`。

- [ ] **Step 1: Write failing navigation and page tests**

Mount `SettingsPage`, click the Token statistics nav item, and assert the model card is hidden and statistics component exists. Mount `TokenUsageSettings` with a two-model snapshot and assert:

- range buttons for 1/7/30 days;
- four summary cards;
- one ring per returned day with `role="img"`;
- both model names in the legend and ranking table;
- exact total in the relevant `title` attribute;
- clicking 7 days calls `loadTokenStatistics(7, true)`.

Add an empty snapshot test asserting the empty-state copy and empty rings render without errors.

- [ ] **Step 2: Run component tests and verify RED**

Run: `npm.cmd test -- --run src/components/TokenUsageSettings.test.ts src/components/SettingsPage.test.ts`

Expected: FAIL because the component and navigation entry do not exist.

- [ ] **Step 3: Implement the component and navigation**

Use semantic buttons and sections. Render circular charts as styled elements with `background: donutGradient(day.models)` and an inner center. Add responsive CSS: 7-day view uses wider cards, 30-day view uses compact cards. Keep model configuration mounted only while its navigation section is active. On mount, load 30 days unless a successful snapshot already exists; refresh forces the request.

- [ ] **Step 4: Re-run component tests and verify GREEN**

Run the Step 2 command and expect PASS.

- [ ] **Step 5: Run all frontend tests and production build**

Run from `frontend/`:

```powershell
npm.cmd test -- --run
npm.cmd run build
```

Expected: all tests pass and Vite build exits 0.

---

### Task 5: Full Verification and Delivery

**Files:**
- Modify: `docs/superpowers/plans/2026-08-09-gui-token-statistics.md`

**Interfaces:**
- Consumes: completed backend and frontend implementation。
- Produces: verified dev-branch commit。

- [ ] **Step 1: Run Python regression suites**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_token_usage.py tests/test_gui_token_statistics.py tests/test_gui_token_usage.py -q -p no:cacheprovider
```

- [ ] **Step 2: Run full frontend verification**

Run `npm.cmd test -- --run` and `npm.cmd run build` from `frontend/`.

- [ ] **Step 3: Verify real GUI rendering**

Start Vite with a temporary mocked PyWebView bridge containing three models across seven days. Use Playwright to open Settings → Token statistics and verify range controls, multi-color rings, model rows, exact accessible labels and loading completion. Capture a screenshot under `output/playwright/` for inspection, then remove temporary mock artifacts.

- [ ] **Step 4: Review and commit**

Run `git diff --check`, stage only planned files, and commit with `feat: add GUI token statistics` on `dev`.

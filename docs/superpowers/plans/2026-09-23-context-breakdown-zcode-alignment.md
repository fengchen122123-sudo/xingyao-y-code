# 上下文用量面板分类对齐 ZCode Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把输入框上下文用量面板的来源分类对齐 ZCode 最新版（工具拆分为「系统工具 / MCP 工具」两行，中文标签同步为 消息 / 系统提示词 / 技能 / 其他 / 系统工具 / MCP 工具）；数字格式保持星瑶完整千分位。

**Architecture:** 后端 `core/context_breakdown.py` 白名单把 `tools` 拆为 `system_tools` + `mcp_tools`；`core/llm.py` 统计点抽为模块级 `_snapshot_tool_breakdown()`，按 MCP 工具名集合（`{server}__{tool}`，来自 `mcp_manager.get_schemas()`）把 `request_tools` 分两组分别计数。前端类型、白名单、标签三处同步。快照为会话级内存态、不持久化，无数据迁移负担。

**Tech Stack:** Python 3（pytest）；Vue 3 + TypeScript（vitest / vue-tsc）。

**背景（2026-09-23）：** 用户对照 ZCode 截图（`github.com/zai-org/ZCode`，源码已核实）要求对齐分类；数字格式经用户确认保持星瑶千分位，不采用 ZCode 的「万/亿」紧凑格式。

## Global Constraints

- **纯旁路铁律**：统计失败绝不影响对话主流程；不改变任何发送给 provider 的字节——`tests/test_prompt_byte_stability.py` 与 `tests/test_provider_payload_golden.py` 回归必须保持通过。
- 数字格式保持星瑶约定（完整千分位；百分比 1 位小数），不引入「万/亿」紧凑格式。
- 分类中文标签对齐 ZCode：消息 / 系统提示词 / 技能 / 其他 / 系统工具 / MCP 工具。
- 旧来源 `tools` 从白名单移除（快照不持久化，无兼容负担；旧值静默过滤）。
- 只在 `dev` 分支操作；不混杂无关重构；改动留在工作区待用户 GUI 验收（不主动 commit）。

---

### Task 1: 后端拆分工具来源统计（system_tools / mcp_tools）

**Files:**
- Modify: `core/context_breakdown.py`（L9-11 docstring、L21-28 `VALID_SOURCES`）
- Modify: `core/llm.py`（L1411-1419 统计点抽函数；新函数放在 `_get_mcp_manager` 定义附近）
- Test: `tests/test_context_breakdown.py`

**Interfaces:**
- Produces: 白名单 `("messages", "system_prompt", "skills", "runtime_context", "system_tools", "mcp_tools")`；`llm._snapshot_tool_breakdown(request_tools) -> None`（纯旁路、绝不抛出）。
- Consumes: `core.llm._get_mcp_manager().get_schemas()` 的 `function.name` 集合用于归类；空组写 `0`（而非 `'[]'` 的 2），前端过滤 `chars<=0` 不会渲染 0 占比行。

- [x] **Step 1: 更新后端测试（先 RED）**

  `tests/test_context_breakdown.py` 全部 `"tools"` 来源引用改为新源：
  - `test_set_and_get_roundtrip_in_stable_source_order`：改用 `system_tools`/`mcp_tools`，断言顺序按新白名单序。
  - `test_get_breakdown_returns_independent_copy` / `test_update_source_appends_then_overwrites` / `test_invalid_chars_are_filtered_without_raising` / `test_concurrent_updates_smoke`：`"tools"` → `"system_tools"`。
  - `test_invalid_sources_are_filtered_without_raising`：补「旧来源 tools 被过滤」断言（`cb.update_source("tools", 10)` 后快照不变）。

  新增 3 个拆分函数测试：

  ```python
  def test_tool_breakdown_splits_system_and_mcp(monkeypatch):
      """工具字符量按 MCP 名集拆为 system_tools / mcp_tools 两组。"""
      class _FakeMCP:
          def get_schemas(self):
              return [{"function": {"name": "firecrawl__scrape_url"}}]
      monkeypatch.setattr(llm, "_get_mcp_manager", lambda: _FakeMCP())
      cb.clear_breakdown()
      try:
          llm._snapshot_tool_breakdown([
              {"function": {"name": "read_file"}},
              {"function": {"name": "firecrawl__scrape_url"}},
          ])
          snapshot = {item["source"]: item["chars"] for item in cb.get_breakdown()}
          assert snapshot["system_tools"] == len(
              json.dumps([{"function": {"name": "read_file"}}], ensure_ascii=False)
          )
          assert snapshot["mcp_tools"] == len(
              json.dumps([{"function": {"name": "firecrawl__scrape_url"}}], ensure_ascii=False)
          )
      finally:
          cb.clear_breakdown()

  def test_tool_breakdown_empty_group_writes_zero(monkeypatch):
      """无 MCP 工具时空组写 0（而非 '[]' 的 2），前端不会渲染 0 行。"""
      class _FakeMCP:
          def get_schemas(self):
              return []
      monkeypatch.setattr(llm, "_get_mcp_manager", lambda: _FakeMCP())
      cb.clear_breakdown()
      try:
          llm._snapshot_tool_breakdown([{"function": {"name": "read_file"}}])
          snapshot = {item["source"]: item["chars"] for item in cb.get_breakdown()}
          assert snapshot["mcp_tools"] == 0
          assert snapshot["system_tools"] > 0
      finally:
          cb.clear_breakdown()

  def test_tool_breakdown_failure_is_silent(monkeypatch):
      """统计异常被吞掉绝不抛出（旁路铁律）。"""
      def _boom():
          raise RuntimeError("mcp manager exploded")
      monkeypatch.setattr(llm, "_get_mcp_manager", _boom)
      cb.clear_breakdown()
      try:
          llm._snapshot_tool_breakdown([{"function": {"name": "read_file"}}])  # 不得抛异常
      finally:
          cb.clear_breakdown()
  ```

  Run: `venv\Scripts\python -m pytest tests/test_context_breakdown.py -q`
  Expected: FAIL（`_snapshot_tool_breakdown` 不存在、白名单未更新）。

- [x] **Step 2: 实现后端改动**

  `core/context_breakdown.py`：

  ```python
  #: 允许的来源；元组顺序即 get_breakdown 的稳定输出顺序（前端按此顺序渲染）
  VALID_SOURCES: tuple[str, ...] = (
      "messages",
      "system_prompt",
      "skills",
      "runtime_context",
      "system_tools",
      "mcp_tools",
  )
  ```

  模块 docstring 来源清单同步改为：messages（消息）/ system_prompt（系统提示词）/ skills（技能目录）/ runtime_context（其他）/ system_tools（系统工具）/ mcp_tools（MCP 工具），并注明 2026-09-23 对齐 ZCode 拆分。

  `core/llm.py` 新增模块级函数（放在 `_get_mcp_manager` 之后）：

  ```python
  def _snapshot_tool_breakdown(request_tools):
      """【纯旁路】按 MCP 工具名集合拆分统计工具定义字符量（设计规格 §3.3；
      2026-09-23 拆分系统/MCP）。失败仅记 debug，绝不改变 request_tools 与发送字节。"""
      try:
          from core.context_breakdown import update_source as _update_ctx_source
          mcp_tool_names = {
              s.get("function", {}).get("name")
              for s in _get_mcp_manager().get_schemas()
          }
          system_tools = [
              s for s in request_tools
              if s.get("function", {}).get("name") not in mcp_tool_names
          ]
          mcp_tools = [
              s for s in request_tools
              if s.get("function", {}).get("name") in mcp_tool_names
          ]
          # 空组写 0（而非 '[]' 的 2），前端过滤 chars<=0 不会渲染 0 占比行
          _update_ctx_source(
              "system_tools",
              len(json.dumps(system_tools, ensure_ascii=False)) if system_tools else 0,
          )
          _update_ctx_source(
              "mcp_tools",
              len(json.dumps(mcp_tools, ensure_ascii=False)) if mcp_tools else 0,
          )
      except Exception:
          logger.debug("工具定义字符量统计失败（旁路，已忽略）", exc_info=True)
  ```

  原 L1411-1419 内联统计块替换为：

  ```python
  # 【纯旁路】工具定义字符量快照（设计规格 §3.3；2026-09-23 拆分系统/MCP）：
  # 每步请求都会走到，重复更新无害；失败仅记 debug，绝不改变发送字节。
  _snapshot_tool_breakdown(request_tools)
  ```

- [x] **Step 3: 运行后端验证**

  Run: `venv\Scripts\python -m pytest tests/test_context_breakdown.py -q`
  Expected: PASS（全部）。

### Task 2: 前端分类对齐（类型 / 白名单 / 标签）

**Files:**
- Modify: `frontend/src/types/pywebview.d.ts`（L433-440 `ContextBreakdownSource`）
- Modify: `frontend/src/stores/appStore.ts`（L143-150 `CONTEXT_BREAKDOWN_SOURCES`）
- Modify: `frontend/src/components/ContextUsagePanel.vue`（L70-78 `SOURCE_LABELS`）
- Test: `frontend/src/components/ContextUsagePanel.test.ts`
- Test: `frontend/src/stores/appStore.tokenUsage.test.ts`

**Interfaces:**
- Consumes: 后端白名单 `system_tools` / `mcp_tools`（Task 1 产出）。
- Produces: 面板中文标签 `消息 / 系统提示词 / 技能 / 其他 / 系统工具 / MCP 工具`；数字格式不变。

- [x] **Step 1: 更新前端测试（先 RED）**

  `ContextUsagePanel.test.ts`：
  - 「按 chars 降序渲染来源占比行」断言 `'对话历史'` → `'消息'`。
  - 新增「渲染系统工具与 MCP 工具两类来源行」测试：

    ```ts
    it('渲染系统工具与 MCP 工具两类来源行', () => {
      setUsage({
        used: 1000,
        window: 128000,
        context_breakdown: [
          { source: 'system_tools', chars: 800 },
          { source: 'mcp_tools', chars: 100 },
        ],
      });
      const wrapper = mountPanel();
      const rows = wrapper.findAll('[data-test="ctx-usage-row"]');
      expect(rows).toHaveLength(2);
      expect(rows[0].find('.cxp-row-name').text()).toBe('系统工具');
      expect(rows[1].find('.cxp-row-name').text()).toBe('MCP 工具');
      wrapper.unmount();
    });
    ```

  - 「过滤非法来源与非正 chars」测试：`{ source: 'tools', chars: -3 }` 改为 `{ source: 'system_tools', chars: -3 }`，并补 `{ source: 'tools' as ContextBreakdownItem['source'], chars: 42 }`（旧源须被过滤）。

  `appStore.tokenUsage.test.ts`：「token_usage 事件透传 context_breakdown」测试中 `{ source: 'tools', chars: -3 }` → `{ source: 'system_tools', chars: -3 }`，并补 `{ source: 'tools', chars: 42 }`；断言保持 `[{ source: 'messages', chars: 620 }]`。

  Run: `cd frontend && npx vitest run src/components/ContextUsagePanel.test.ts src/stores/appStore.tokenUsage.test.ts`
  Expected: FAIL（标签/白名单未更新）。

- [x] **Step 2: 实现前端改动**

  `pywebview.d.ts` / `appStore.ts` / `ContextUsagePanel.vue` 三处同步：

  ```ts
  // pywebview.d.ts
  export type ContextBreakdownSource =
    | 'messages'
    | 'system_prompt'
    | 'skills'
    | 'runtime_context'
    | 'system_tools'
    | 'mcp_tools';
  ```

  ```ts
  // appStore.ts
  const CONTEXT_BREAKDOWN_SOURCES = new Set<ContextBreakdownItem['source']>([
    'messages',
    'system_prompt',
    'skills',
    'runtime_context',
    'system_tools',
    'mcp_tools',
  ]);
  ```

  ```ts
  // ContextUsagePanel.vue（2026-09-23 对齐 ZCode：工具拆分 + 标签同步）
  const SOURCE_LABELS: Record<ContextBreakdownSource, string> = {
    messages: '消息',
    system_prompt: '系统提示词',
    skills: '技能',
    runtime_context: '其他',
    system_tools: '系统工具',
    mcp_tools: 'MCP 工具',
  };
  ```

- [x] **Step 3: 运行前端验证**

  Run: `cd frontend && npx vitest run src/components/ContextUsagePanel.test.ts src/stores/appStore.tokenUsage.test.ts src/components/InputArea.test.ts`
  Expected: PASS（全部）。

### Task 3: 独立验证（回归 + 构建 + 交叉检查）

**Files:**
- 只读验证；不修改文件。

- [x] **Step 1: 后端回归**

  Run: `venv\Scripts\python -m pytest tests/test_context_breakdown.py tests/test_agent_service.py tests/test_prompt_byte_stability.py tests/test_provider_payload_golden.py -q`
  Expected: PASS（字节黄金不得为迁就实现改写）。

- [x] **Step 2: 前端相关面 + 生产构建**

  Run: `cd frontend && npx vitest run src/components/ContextUsagePanel.test.ts src/stores/appStore.tokenUsage.test.ts src/components/InputArea.test.ts && npm run build`
  Expected: PASS + 构建成功（pywebview 加载 dist 产物，必须重建）。

- [x] **Step 3: 交叉检查（只读）**

  - 搜索 `core/`、`tests/`、`frontend/src/` 中是否还有 `'tools'` 作为 breakdown 来源的残留引用（应只剩历史注释/测试中「旧源被过滤」的显式断言）。
  - 核对前后端六源白名单一致（后端 `VALID_SOURCES`、前端 `CONTEXT_BREAKDOWN_SOURCES`、`SOURCE_LABELS`）。

---

## 验收与收尾

- 自动项全过后，GUI 手工验收由用户执行：悬停输入框圆环 → 面板出现「系统工具 / MCP 工具」两行（拆分正确），分类名与 ZCode 截图一致（消息 / 系统提示词 / 技能 / 其他 / 系统工具 / MCP 工具），数字仍为完整千分位。
- 改动留在工作区（dev 分支），待用户 GUI 验收后按项目惯例提交。

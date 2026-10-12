# 2026-08-29 全面测试问题清单与修复方案

> 测试执行：自动化功能测试负责人（黑盒 + 自动化基线 + 本地 Mock 模型闭环）
> 基线：`dev` @ `bdebe13`，工作区含未提交改动 39 文件（+4246/−202）
> 性质：只测试与定位，本文档不包含任何代码改动；所有修复需另行授权实施
> 证据目录：`%TEMP%\ycode-functional-20260829-110702\`（diag\、cli-data\、gui-data\）与 `C:\Users\Public\ycode-loop-ws`

---

## 一、结论摘要

| 维度 | 结果 |
|---|---|
| 定向回归（高风险 12 文件 / 安全 15 文件 / Provider 字节 / 前端 383） | 全部 PASS（高风险组 1 个失败见 D2） |
| 全量单命令 `pytest -q tests/` | **挂起（2/2 复现）**，见 D1 |
| CLI 黑盒（本地命令 + Mock 模型闭环） | 核心回路 PASS；发现 3 个文案/记录类问题（D6/D7/D8） |
| GUI 黑盒（冷启动×5 / 主题编辑器 / 选择器 / 文件面板） | PASS，无新增缺陷 |
| 修复建议 | 先 D1、D2（P1），再 D3、D4（P2），D5 与 P3 随低风险窗口处理 |

---

## 二、缺陷总表

| ID | 级别 | 标题 | 关键位置 | 当前改动引入 | 验证状态 |
|---|---|---|---|---|---|
| D1 | P1 | 全量 pytest 竞态挂起：权限询问 Future 无限等待 | `tests/test_harness_runtime.py:91`、`core/llm.py:772-784`、`core/harness/tool_pipeline.py:741` | 疑似（子集从未复现全量 2/2 挂起） | 已复现+线程转储 |
| D2 | P1 | 新增测试 `_bridge_without_init` 缺 `_turn_start_lock` 基线红 | `tests/test_gui_token_usage.py:117`、`gui_bridge.py:438/2575` | **是**（测试为本工作区新增） | 稳定复现 |
| D3 | P2 | `write_file` 不记录 undo 状态：新建文件不可撤销，force 分支成死代码 | `core/tools.py:482`（无 set_undo_state）、`core/agent_service.py:593-596` | 待查（历史路径） | 黑盒复现 |
| D4 | P2 | `YCODE_DATA_DIR` 隔离泄漏：输入历史硬编码 `~/.ycode` | `main.py:2691` | 否（v1.1.25 起） | 黑盒复现 |
| D5 | P2 | CLI `/theme` 运行时切换不生效（提示已切换，实际需重启） | `main.py:1662-1667`、`main.py:3219-3229` | 改动试图修复但未生效 | 黑盒复现 |
| D6 | P2 | 裸 `pytest -q` 被 `.superpowers` 遗留目录打断（16 收集错误） | `pytest.ini`（无 testpaths） | 否（目录 8/5 遗留） | 稳定复现 |
| D7 | P3 | 新建文件 `/undo` 文案误导 | `core/agent_service.py:587-588` | 待查 | 黑盒复现（与 D3 同根） |
| D8 | P3 | 后台任务启动行文案「✓ 命令执行完成」误导 | `core/tool_presenter.py:12`、`core/tool_progress.py:223` | 待查 | 黑盒复现 |
| D9 | P3 | pytest teardown 偶发 `ValueError: I/O operation on closed file` 吞错误摘要 | pytest capture 与收集错误路径相关 | 否（环境级） | 早期 5 次，后期未现 |

---

## 三、缺陷详情与修改方案

### D1（P1）全量 pytest 竞态挂起：权限询问 Future 无限等待

**现象**
`venv\Scripts\python -m pytest -q tests/` 两次运行均在 ~27%（约 455/1682）处永久挂起；被杀前主进程 17 分钟仅 10s CPU（阻塞非死循环）。唯一能跑完的是各种子集。

**证据**
- faulthandler 转储（`pytest-full-tests2.log`）：主线程栈
  `tests/test_harness_runtime.py:91 test_persistent_turn_records_two_model_steps_for_one_tool_round`
  → `tool_pipeline.py:741 _permission_decision` → `llm.py:965 check_permission_and_ask`
  → `llm.py:876 authorize_tool_call` → `llm.py:781 request.future.result(timeout=0.1)`（`llm.py:772-784` 的 `while True` 轮询，**无总超时**，仅 cancel_event 可解除）。
- 转储同时可见前序测试遗留线程：`cache_keepalive.py:158 _run`、`mcp_client.py:232 _run_loop`（全局状态污染证据）。
- 复现矩阵：全量 2/2 挂；33 文件前缀组合 3 次全过；H2（gui_lifecycle..workspace_groups）+harness_runtime 1/3 挂 → 时序敏感竞态。

**根因**
某前序测试留下权限全局状态（mode/broker）或干扰线程，使该测试的 fake provider 工具调用落入真实权限询问路径；测试内无人应答 Future → `while True` 永久等待。

**修改方案（按最小代价排序）**
1. **止血（配置级，建议立即做）**：`pytest.ini` 增加
   ```ini
   faulthandler_timeout = 120
   ```
   挂起将在 2 分钟变可见超时转储，CI 不再无限占用。
2. **定位污染源**：按 `diag/repro-*.log` 的二分法继续收窄（H2 内 15 文件 × 单文件 + harness_runtime，faulthandler_timeout=35）；重点排查 `test_gui_security/test_gui_tool_events/test_gui_message_final` 对 permission broker/mode 全局的 mock 是否未还原。
3. **加固（可选）**：`llm.py:_wait_permission_option` 支持从环境变量读取总超时（如 `YCODE_TEST_PERMISSION_TIMEOUT`，仅测试进程设置；生产默认保持无限等待语义不变，不影响交互 CLI/GUI）。

**回归验证**：`pytest -q tests/` 连续 3 次完整跑完；高风险组无回归。

---

### D2（P1）新增测试基线红：`_bridge_without_init` 缺 `_turn_start_lock`

**现象**
`tests/test_gui_token_usage.py::test_gui_session_rename_and_clear_update_cache_lifecycle` 稳定失败：
`AttributeError: 'ApiBridge' object has no attribute '_turn_start_lock'`（gui_bridge.py:2575 rename_session 内）。

**证据**
- 失败栈：`tests/test_gui_token_usage.py:117` 断言 `renamed["ok"] is True`；日志 gui_bridge.py:2581。
- 测试 helper `_bridge_without_init` 绕过 `__init__`，未创建 `_turn_start_lock`（正常初始化在 `gui_bridge.py:438`）。
- 同族警告：`lan-remote-startup` 线程 `_ensure_lan_server`（gui_bridge.py:3070）在旁路桥上缺 `_lan_server`/`_lan_lock`（:475-476 初始化；线程 spawn 位于 `get_initial_state` gui_bridge.py:1950，生产时序无竞态）。

**当前改动引入**：是。该测试为本工作区新增（+57 行）；`_turn_start_lock` 的使用（gui_bridge.py:2575）是已提交代码。

**修改方案（二选一，均最小）**
1. **测试侧（推荐，改动最小）**：`_bridge_without_init` 补齐旁路所需属性：
   ```python
   bridge._turn_start_lock = threading.Lock()
   bridge._lan_server = None
   bridge._lan_lock = threading.Lock()
   ```
2. **产品侧防御**：`ApiBridge` 将 `_turn_start_lock = None`、`_lan_server = None`、`_lan_lock = None` 声明为类属性默认值（实例 init 时覆盖），任何旁路/半初始化构造不再 AttributeError。方案 2 同时让 D2 的线程警告消失，但改变面稍大。

**回归验证**：`pytest -q tests/test_gui_token_usage.py`（应 10/10）；`pytest -q tests/test_gui_lifecycle.py tests/test_gui_token_usage.py` 无新警告。

---

### D3（P2）`write_file` 不记录 undo 状态：新建文件不可撤销

**现象（Mock 模型闭环实测）**
- 第 1 轮：mock 通过 `write_file` 新建 hello.txt → `/undo` 报「当前会话没有可以撤销的操作记录」。
- 第 2 轮：`write_file` 覆盖同一文件 → `/undo` 成功「恢复到上一个版本 + 抹除记忆」。

**根因**
`set_undo_state` 仅存在于 `edit_file` 相关路径（`core/tools.py:1808/2113/2158`）；`write_file`（`core/tools.py:482`）写入前**从不调用** `set_undo_state`。因此：
- 新建文件完全没有 undo 记录；
- `core/agent_service.py:593-596` 专为"删除新建文件需 force"设计的分支成为死代码；
- 与 `docs/all_features.md` §七「每当星瑶对文件进行修改（write_file 或 edit_file）前，系统自动备份该文件的旧版本」的描述相悖。

**修改方案（最小）**
在 `write_file`（core/tools.py:482）完成参数校验后、实际写盘前记录状态：
```python
from core.memory import set_undo_state
old = ""
try:
    with open(resolved, "r", encoding=..., errors=...) as f:  # 复用既有读取工具
        old = f.read()
except FileNotFoundError:
    old = ""
set_undo_state(resolved, old)
```
要点：old_content 为 `""` 表示新建，从而激活 `undo_last_edit`（agent_service.py:594-596）既有的 force 删除确认流程；注意与线程局部 `_undo_session` 标记（memory.py:168/208）的既有约定保持一致。`write_file` 的智能修改路径（:2113/:2158 一带）已各自记录，需一并核对是否重复记录。

**回归验证**
- `pytest -q tests/test_agent_service.py -k undo`（含 `test_undo_last_edit_requires_force_to_delete_new_file`，修复后该分支应从死代码变为可达）；
- Mock 闭环复测：新建→`/undo`→应提示需要确认删除；覆盖→`/undo`→内容还原。

---

### D4（P2）`YCODE_DATA_DIR` 隔离泄漏：输入历史硬编码

**现象**
`YCODE_DATA_DIR` 指向临时目录时，DB/config/memory 正确隔离，但斜杠命令历史仍写入真实 `~/.ycode/input_history.txt`（实测 9823→10237 字节）。

**根因**
`main.py:2691`：`_hist_dir = os.path.join(os.path.expanduser("~"), ".ycode")`——未走 `config.YCODE_DIR`（config.py:149 支持 env 覆盖）。conftest/多实例/便携化场景均受影响。

**修改方案**
```python
from config import YCODE_DIR
_hist_dir = str(YCODE_DIR)
```
并全局排查其余硬编码：`grep -rn 'expanduser' main.py core/ gui_bridge.py | grep -i ycode`，凡与数据目录相关的一律改走 `config.YCODE_DIR`。

**回归验证**：隔离实例输入命令后，临时目录出现 input_history.txt、真实 `~/.ycode/input_history.txt` mtime 不变；`pytest -q tests/test_config.py` 无回归。

---

### D5（P2）CLI `/theme` 运行时切换不生效

**现象**
`/theme matrix`、`/theme white` 均提示「✔ 已切换配色主题为：X」，但提示符/状态栏/分隔线颜色不变；重启后新主题正确生效（持久化本身正常，配置 `"theme": "white"` 落盘）。

**根因**
`main.py:1662-1667` 仅更新 `current_theme` 并 `app_instance.invalidate()`（本次改动新增的 invalidate 正是为此），但 prompt_toolkit 的样式对象在 `main.py:3219-3229` 启动时由 `Style.from_dict(THEMES.get(current_theme))` **一次性构建**，invalidate 重绘不会换色。

**修改方案（最小，两处）**
1. 抽出 `def build_cli_style(theme_name): return Style.from_dict({...})`（从 :3229 现场提取）。
2. `/theme` 分支在 :1662 后追加 `app_instance.style = build_cli_style(theme_name)`（prompt_toolkit 的 Application/PromptSession 支持 `style` 为可调用或直接赋值新 Style 实例，再 invalidate 即生效）。
   若验证 PromptSession 不支持运行期替换，退而求其次：传入 `style=lambda: build_cli_style(current_theme)` 动态样式。
   若两者成本超预期，至少把成功文案改为「已保存，重启后生效」以消除误导。

**回归验证**：`/theme matrix` 后提示符/状态栏立即变绿；`/theme white` 全局变浅；重启持久化不变；`pytest -q tests/test_terminal_markdown.py tests/test_turn_renderer.py` 无回归。

---

### D6（P2）裸 `pytest -q` 被 `.superpowers` 遗留目录打断

**现象/证据**
裸命令收集期报 16 个 `PermissionError [WinError 5]`，全部指向 `.superpowers\pytest-task*` 10 个目录（8/5 遗留，访问被拒）；16 个错误详情又常被 D9 吞掉。pytest.ini 无 `testpaths`，`norecursedirs` 未含 `.superpowers`。

**修改方案**
1. `pytest.ini`：
   ```ini
   [pytest]
   testpaths = tests
   norecursedirs = mytemp .pytest_cache .git .venv venv node_modules dist build .superpowers
   ```
2. `.superpowers\pytest-*` 目录本身由用户决定删除或解锁（本轮未触碰，属用户数据）。

**回归验证**：任意目录裸跑 `pytest -q` 收集 1682 项 0 错误。

---

### D7（P3）新建文件 `/undo` 文案误导

与 D3 同根。`core/agent_service.py:587-588` 的「当前会话没有可以撤销的操作记录」在"本轮确有写文件操作"场景下为不实陈述。D3 修复后该分支仅剩真正的无记录场景，文案自然准确；若 D3 暂缓，可先改为「当前会话没有可撤销的文件修改记录（新建文件的撤销需显式确认删除）」。

### D8（P3）后台任务启动行文案「✓ 命令执行完成 · 50.2s」

`run_in_background=true` 的任务启动后，回合内工具行显示「✓ 命令执行完成」（`core/tool_presenter.py:12` 将 execute_command 映射为"命令执行完成"；`core/tool_progress.py:223` 输出 summary），用户会误以为同步执行已结束。工具结果 JSON 实际含 task_id（模型侧正确）。

**修改方案**：`tool_progress.py:223` / presenter 对 `run_in_background` 结果分支为「后台任务已启动 ID=xxx（用 /tasks 管理）」；或在渲染处读取结果 JSON 的 background/task_id 字段区分。CLI 终结文案（main.py:1949「✔ 后台进程 … 已成功终结」）无需改动。

### D9（P3）pytest teardown 偶发崩溃吞摘要

`ValueError: I/O operation on closed file`（capture.py stop_global_capturing），早期 5 次、后期未现，与含收集错误的运行相关。D6 修复（消除收集错误）+ D1 止血（faulthandler_timeout）后大概率不再出现；若复发再单独立案。

---

## 四、建议修复顺序

1. **D6 + D1-止血**（pytest.ini 两行）——恢复"一条命令全量回归"的能力，是其余修复的验收前提。
2. **D2**（测试 helper 补 3 个属性）——基线回绿。
3. **D1-定位**（按既有二分法找污染源）——需要真实修复。
4. **D3 + D7**（write_file 记录 undo）。
5. **D4、D5**（隔离与主题，各为小改动）。
6. **D8** 文案。

## 五、修复后回归验收清单

```powershell
venv\Scripts\python -m pytest -q                      # D6 后应= pytest -q tests/，完整跑完（D1 验收）
venv\Scripts\python -m pytest -q tests\test_gui_token_usage.py            # D2
venv\Scripts\python -m pytest -q tests\test_agent_service.py -k undo      # D3/D7
venv\Scripts\python -m pytest -q tests\test_progress_guard.py tests\test_tool_pipeline.py tests\test_harness_runtime.py tests\test_cancellation.py    # D1 无回归
venv\Scripts\python -m pytest -q tests\test_prompt_byte_stability.py tests\test_provider_payload_golden.py tests\test_deepseek_prompt_ledger.py       # 字节稳定不回归
npm --prefix frontend test && npm --prefix frontend run build
```
黑盒抽检：CLI `/theme matrix` 即时变色（D5）；隔离实例输入命令后真实目录 input_history 不变（D4）；Mock 闭环新建文件→`/undo` 提示确认删除（D3）；后台任务回合显示"已启动 ID"（D8）。

## 六、测试覆盖说明（未修复项之外）

- 已覆盖：CLI 本地命令、Mock 模型写文件/撤销/后台任务三条真实闭环、GUI 冷启动×5/主题编辑器全流程/选择器/文件面板空态、自动化 1200+ 用例。
- 未覆盖：GUI 模型驱动闭环、子代理超时接管实机复现（自动化 fake-runner E2E 已覆盖）、崩溃恢复、LAN 手机端（需真机）、真实多轮长会话压缩。

---

## 七、修复执行记录（2026-08-29，未 commit，等待授权）

| ID | 修复内容 | 改动文件 | 验证结果 |
|---|---|---|---|
| D6+D1止血 | pytest.ini 加 `testpaths = tests`、`norecursedirs` 补 `.superpowers`、`faulthandler_timeout = 120` | pytest.ini | 裸 `pytest` 收集 1682 项 0 错误（原 16 错误） |
| D2 | `_bridge_without_init` 补 `_turn_start_lock/_message_queues/_lan_server/_lan_lock` | tests/test_gui_token_usage.py | 该文件 5/5 passed（原 1 failed） |
| D1 | tests/conftest.py 新增 autouse 夹具：每测试后快照还原 app_context 六项全局 + broker 应答通道（污染源=真实构造 ApiBridge 的测试残留 `ui_flavor="gui"`/broker handlers，llm.py:744-759 因此走 enqueue 死等） | tests/conftest.py | 最小复现 `test_gui_update+test_harness_runtime` 修复前挂起（进程 4 分钟不退），修复后 41 passed/20.6s；**全量 `pytest -q` 完整跑完 1679 passed/2 skipped/3:46（此前 2/2 挂起）** |
| D3+D7 | write_file（core/tools.py）写入成功后 `set_undo_state(str(resolved), old_content)`（old_content=""=新建）；TDD 先 RED 后 GREEN | core/tools.py、tests/test_agent_service.py（新增 test_write_file_records_undo_state） | 新用例 RED（复现"没有可以撤销的操作记录"）→GREEN；`-k "undo or write_file_records"` 3 passed；test_tools.py 188 passed |
| D4 | main.py:2691 输入历史改走 `config.YCODE_DIR`；同类硬编码一并修复 core/skills.py:44/72（发布标记/技能释放目录） | main.py、core/skills.py、tests/test_builtin_skills_sync.py（夹具改 patch config.YCODE_DIR） | 技能测试 12 passed/1 skipped；main.py py_compile OK |
| D5 | 样式构建抽为模块级 `_build_cli_style(theme_name)`，Application 传 `DynamicStyle(lambda: _build_cli_style(current_theme))` | main.py | CLI 黑盒：`/theme matrix` 后状态栏/边框/提示符**立即变绿**，`/theme white` 即时切回（修复前不变色需重启） |
| D8 | build_tool_display 输出 `background` 标记（识别"异步后台任务已成功启动"）；ToolDisplayEvent 加字段；_success_lines 分支「后台任务已启动（用 /tasks 管理）」 | core/tool_presenter.py、core/tool_progress.py、frontend/src/types/pywebview.d.ts、两个测试文件（新增 3 用例+更新 1 个精确字典断言） | 两文件 40 passed |
| 前端回归 | — | — | `npm test` EXIT=0（383+ 用例）、`npm run build` EXIT=0 |
| 编译检查 | — | — | 全部改动文件 py_compile 通过 |

### 回归中新发现（预存问题，未修改，需用户决策）

| 项 | 现象 | 归因依据 |
|---|---|---|
| N1 | `tests/test_task_manager.py` 3 个 kill 用例失败（拒绝路径不再清理 ACTIVE_TASKS 记录：`test_kill_refuses_to_kill_reused_pid` / `test_kill_refuses_when_process_identity_cannot_be_verified` / `test_kill_returns_false_when_taskkill_fails`） | 停用本次全部改动后依然失败（0.35s 隔离复现）；test_task_manager.py 为已提交测试，core/task_manager.py 属未提交改动（+131 行），疑似进行中改动与既有测试语义冲突 |
| N2 | `tests/test_llm_events_fixes.py` 失败（全量中 2 个、隔离运行 5 个，含 pywintypes DLL/plan 模式询问/上下文熔断用例，失败集合随运行环境浮动） | 原实施计划 Task 5 记录已对照 bdebe13 基线标注为"预先存在的环境/用例问题"；本次改动未触及该文件相关路径 |

### 未提交说明

按 AGENTS.md 约定未执行 git commit；全部修复以工作区改动形式存在（diff 统计从 +4246/−202 变为 +4451/−218，净增部分即本次修复）。

---

## 八、复检记录（2026-08-30：审查修复轮验证）

开发者按第七节清单实施修复并另行加固（未 commit，HEAD 仍为 bdebe13，工作区 55 文件 +5759/−364）。测试负责人复检结论：**全部通过**。

**审查加固亮点（在 D1-D9 修复基础上的改进）**
- D3/D7 加固：`set_undo_state` 增加 `existed_before`/`old_bytes` 参数（向后兼容），write_file 撤销优先保存原始字节（空文件/GBK 等非 UTF-8 文件可精确恢复，修复了原实现的替换字符风险）；会话路由兜底 `app_context.get_thread_session()`（解决 GUI/子代理线程的会话 TLS 不一致）；`undo_last_edit` 改 `_restore_file_bytes` 原子字节写回。
- N1 关闭：core/task_manager.py + tests/test_task_manager.py 同步修复，3 个 kill 用例转绿。
- N2 关闭：tests/test_llm_events_fixes.py 相关用例修复转绿。

**复检验证（真实命令结果）**
- 全量 `pytest -q`：**1707 passed / 2 skipped，EXIT=0，3:51 完整跑完**（对比修复前：2/2 挂起；上一轮：5 failed）
- 定点复跑原失败区：`tests/test_task_manager.py tests/test_llm_events_fixes.py` → 87 passed
- 前端：`npm test` 383 passed EXIT=0；`npm run build` EXIT=0
- 测试数净增：1707 vs 修复前 1684 可执行用例（新增约 23 个）

**最终状态**：D1-D9 全部闭环，N1/N2 预存问题同步关闭；全量单命令回归能力恢复。剩余事项：按 AGENTS.md 待用户授权后提交。

# Windows-MCP 能力对齐实施计划

> **For agentic workers:** 按 Task 顺序执行，每个 Task 自带测试与提交。

**Goal:** 零新增第三方依赖、安装包体积增量 < 1 MB，把 Windows-MCP 的增量能力做进星瑶。

**Architecture:** UI 动作扩展 `computer_action` 协议（wait_for、sequence 元素动作）；系统能力做成内置插件 `plugins/windows_tools`。

**设计文档:** `docs/2026-11-09-windows-mcp-parity-design.md`

## Global Constraints

- 不得新增第三方依赖，requirements.txt 与 pyproject.toml 不动。
- 不得改变现有 21 个动作的字段、语义与错误码。
- 错误码英文小写下划线，message 中文。
- 非 Windows 不导入 win32api / winreg / comtypes。
- wait_for 只在 target="desktop" 生效。
- dev 分支提交，提交信息中文。
- 测试统一用 venv/Scripts/python.exe -m pytest。

---

## Task 1 wait_for 协议层

Files:
- Modify: core/computer/models.py
- Modify: core/computer/actions.py
- Create: tests/test_computer_wait_for.py

Steps:
1) 写失败测试：解析默认值（condition=text_exists、timeout_ms=8000、interval_ms=300）、未知 condition 拒绝、text_exists 缺 text 拒绝、element_exists 缺 element_ref 拒绝、timeout 越界拒绝、browser target 拒绝、未知字段拒绝。
2) pytest tests/test_computer_wait_for.py -q 确认失败（未知动作类型 wait_for）。
3) 实现：
   - models.py：ComputerActionKind 增加 WAIT_FOR = "wait_for"；ComputerAction 增加 condition / window_name / timeout_ms / interval_ms 四个带默认值字段。
   - actions.py：新增 WAIT_FOR_CONDITIONS=("text_exists","element_exists","element_enabled","window_active")、默认值 8000/300、边界 1000-30000 / 100-2000；_ALLOWED["wait_for"]=_COMMON|{condition,text,window_name,element_ref,timeout_ms,interval_ms}；_DESKTOP_ONLY_KINDS 加入 wait_for；parse 分支按 condition 严格校验必填字段与边界；schema properties 增加四个字段。
4) pytest tests/test_computer_wait_for.py -q 预期 7 passed。
5) git commit -m "feat(computer): wait_for 协议层（条件等待解析与 schema）"

---

## Task 2 wait_for 执行层

Files:
- Modify: core/computer/platforms/windows_elements_uia.py
- Modify: core/computer/win32_adapter.py
- Modify: core/computer/windows_backend.py
- Modify: tests/test_computer_wait_for.py

硬约束：collect() 会整体替换 self._elements（windows_elements_uia.py:131），probe 绝不可调用 collect 或写 self._elements，否则 el-N 在等待期间全部失效。

Steps:
1) 写失败测试：超时抛 computer_wait_timeout；命中后 _elements 的 id 与内容不变；window_active 走 list_top_window_titles。
2) 确认失败（_wait_for_interruptible 不存在）。
3) 实现：
   - RealUIAHost.probe(hwnd, matcher)：element_exists / element_enabled 从 self._elements 只读取值；text_exists 用 ControlViewWalker BFS 匹配 CurrentName 或 ValuePattern 值，深度 MAX_TREE_DEPTH、节点 MAX_ELEMENTS，单节点异常跳过。
   - Win32InputAdapter.list_top_window_titles()：ctypes EnumWindows + IsWindowVisible + GetWindowTextW，返回可见且有标题的顶层窗口标题。
   - WindowsComputerBackend.execute 新增 wait_for 分支，锁外调用 _wait_for_interruptible(hwnd, action)：按 interval_ms 分片，每轮检查 _stop_event；window_active 匹配标题子串，其余走 probe；超时抛 computer_wait_timeout，message 提示改用 observe。
4) pytest tests/test_computer_wait_for.py tests/test_computer_elements.py tests/test_computer_windows_backend.py -q 全绿。
5) git commit -m "feat(computer): wait_for 执行层（只读 probe 与分片轮询）"

---

## Task 3 wait_for 计时接线

Files: core/computer/controller.py:852、tests/test_computer_controller.py
Steps:
1) 写失败测试：wait_for 的 computer_action_finished 事件里 input_ms=0、wait_ms>=0。
2) 确认失败。
3) 改为 if action.kind in ("wait", "wait_for")。
4) pytest tests/test_computer_controller.py tests/test_computer_wait_for.py -q 全绿。
5) git commit -m "feat(computer): wait_for 计入等待阶段计时"

---

## Task 4 sequence 支持元素动作

Files: core/computer/actions.py、tests/test_computer_sequence.py
Steps:
1) 写失败测试：sequence 接受 click_element + set_value_element；仍拒绝 list_elements。
2) 确认失败。
3) _SEQUENCE_STEP_KINDS 增加 click_element、set_value_element，list_elements 不在名单内。
4) pytest tests/test_computer_sequence.py -q 全绿。
5) git commit -m "feat(computer): sequence 支持元素引用动作"

---

## Task 5 插件骨架 + 进程 + 剪贴板

Files:
- Create: plugins/windows_tools/plugin.json
- Create: plugins/windows_tools/plugin.py
- Create: tests/test_plugin_windows_tools.py

Steps:
1) 写失败测试：非 Windows 注册为空；Windows 注册顺序为 windows_process、windows_clipboard、windows_registry、windows_notify（后者 Task 6 补齐）。
2) 确认失败（目录不存在）。
3) 实现：
   - plugin.json：name=windows_tools、version=0.1.0、entry=plugin.py、enabled=true。
   - register()：sys.platform != "win32" 或 YCODE_TOOL_PROFILE=cli 直接 return。
   - _process_tool：list 用 psutil.process_iter(["pid","name","memory_info"])，支持 filter/sort_by/limit；kill 支持 pid 或 name，拒绝 _PROTECTED_NAMES（system、system idle process、csrss.exe、wininit.exe、winlogon.exe、services.exe、lsass.exe）与 pid 0/4/当前进程；force 用 kill，否则 terminate。
   - _clipboard_tool：get 用 OpenClipboard + GetClipboardData(13) + GlobalLock + wstring_at；set 用 EmptyClipboard + GlobalAlloc(0x0042) + SetClipboardData；finally CloseClipboard。
   - 两个 ToolSpec：read_only=False、requires_permission=True、side_effect_scope="system"。
4) pytest tests/test_plugin_windows_tools.py -q 通过。
5) git commit -m "feat(plugin): windows_tools 进程与剪贴板能力"

---

## Task 6 注册表（只读）与通知

Files: plugins/windows_tools/plugin.py、tests/test_plugin_windows_tools.py
Steps:
1) 写失败测试：set 模式返回错误且含「不提供写入」；相对路径返回错误。
2) 确认失败（_registry_tool 不存在）。
3) 实现：
   - _registry_tool：mode 只接受 get/list；set/delete 直接拒绝；path 必须以 HKEY_CURRENT_USER / HKEY_LOCAL_MACHINE / HKEY_CLASSES_ROOT 开头，子路径非空且不含 ..；winreg.OpenKey(KEY_READ)；输出截断 4000 字符。
   - _notify_tool：title<=64、message<=256，win32gui.MessageBox(foreground, message, title, MB_OK|MB_ICONINFORMATION)。
   - windows_registry：read_only=True、requires_permission=False、side_effect_scope="none"。
   - windows_notify：requires_permission=False，描述写明会阻塞到点确定。
4) pytest tests/test_plugin_windows_tools.py -q 通过。
5) git commit -m "feat(plugin): windows_tools 只读注册表与通知"

---

## Task 7 提示词与工具描述同步

Files: core/computer/actions.py、system_prompt.md、system_prompt_gui.md
Steps:
1) COMPUTER_ACTION_TOOL_SCHEMA description 末尾追加：等待界面变化优先用 wait_for（四个 condition），不要用 wait 加反复 observe；超时返回 computer_wait_timeout。两个 system_prompt 的电脑操控段落追加：sequence 打包元素动作时，元素引用必须来自之前最近一次 list_elements。
2) pytest tests/test_prompt_byte_stability.py tests/test_computer_llm_generic.py tests/test_computer_provider_generic.py -q。若固定摘要或工具数量断言失败，按有意协议扩展处理并在提交信息注明。
3) git commit -m "docs(computer): 同步 wait_for 与 sequence 元素动作说明"

---

## Task 8 全量验证与体积验收

1) venv/Scripts/python.exe -m pytest tests/ -q 全绿，失败单独修复不得跳过。
2) venv/Scripts/python.exe -m py_compile core/computer/actions.py core/computer/windows_backend.py core/computer/platforms/windows_elements_uia.py core/computer/win32_adapter.py plugins/windows_tools/plugin.py，退出码 0。
3) venv/Scripts/python.exe build_release.py，dist/ycode 体积相对基线 126.0 MB 增量必须 < 1 MB；超限对比构建前后目录定位新增文件。
4) git commit --allow-empty -m "chore: Windows-MCP 对齐验收（体积增量 < 1MB，全量测试通过）"

---

## Self-Review

Spec 覆盖：设计 5.1 到 Task 1/2/3；5.2 到 Task 4；5.3 到 Task 5/6；5.4 到 Task 7；5.5 与 10 到 Task 8，无遗漏。

已知取舍：windows_notify 从托盘气球改为 MessageBox，因为气球通知在 Win10/11 需注册窗口类与图标、代码量与失败面更大且可能被系统静默丢弃；MessageBox 保证用户看到，代价是阻塞到点确定。

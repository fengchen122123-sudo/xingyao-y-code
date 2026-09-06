# 执行提示词：电脑操控通用化重构（Task A-E + GUI 焦点礼让）

> 用法：新开会话，把下面分割线之间的全部内容作为第一条消息发给 agent。

---

## 任务背景

ycode（E:\fch\Y-code_tui，Python，dev 分支）的电脑操控功能已完成两阶段改造
（sequence 批量动作、剪贴板输入、UIA 元素动作的 schema/backend/投影层），但
实测（会话 156，~/.ycode/ycode_gui.db session_id=156）单件任务仍需 21 分钟，
根因：元素能力没接进生产构造、模型靠裸截图猜坐标、点击前不激活窗口、ycode
自身 GUI 抢焦点。

你的任务：完成方案「阶段三」的 Task A-E + Task F'（GUI 焦点礼让）。

**开工前必读（按序）**：
1. `AGENTS.md`（工作区规则：dev 分支、py_compile 校验、惰性导入铁律、Journal 铁律）
2. `docs/superpowers/plans/2026-09-06-computer-control-universal.md`（五大支柱方案）
3. `docs/superpowers/specs/2026-09-06-computer-speedup-design.md`（阶段一二的设计补遗）

## 全局约束

- 只在 dev 分支提交，每个 Task 一个提交，提交信息注明（通用化重构 Task X）。
- Python 一律 `venv\Scripts\python`；每任务完成跑 `venv\Scripts\python -m py_compile <改动文件>`。
- **仓库有用户未提交的在途 WIP**（gui_bridge.py、gui_main.py、main.py、
  core/command_shell.py、core/computer/browser_backend.py、frontend/* 等）：
  除 Task F' 允许对 gui_bridge.py 做**外科手术式最小修改**外，绝不触碰、绝不
  提交、绝不还原这些文件里非你改动的部分；提交一律显式 `git add <文件>`。
- 重依赖惰性导入；测试不真实移动鼠标/截屏（用 tests/computer_fakes.py 的 fake）。
- `tests/test_prompt_byte_stability.py` 验证的是确定性/顺序/稳定前缀哈希，
  computer 工具是动态 schema 不在冻结 golden 内——改完提示词后跑它确认仍绿。

## Task A：元素能力生产接线

文件：`core/computer/windows_backend.py`、`core/computer/platforms/windows_elements.py`

1. `WindowsElementLocator` 改为惰性启动：现在 `__init__` 里无 seam 时立即构造
   `RealUIAHost`（会起 COM 线程）；改成首次 `collect/click/set_value` 才构造
   （`self._real is None` 时创建），`close()` 幂等已有。
2. `WindowsComputerBackend.create_default()`（约 165 行）加
   `element_locator=WindowsElementLocator()`。
3. backend 关闭时释放定位器：`close()` 里（`release_inputs` 之后）调用
   `self._element_locator.close()`（若非 None，try/except 留痕）。
4. 测试：`tests/test_computer_elements.py` 追加——create_default 后
   backend._element_locator 非 None；locator 未使用时不创建 RealUIAHost
   （用 monkeypatch 替换 RealUIAHost 构造断言未调用）；backend.close 后定位器
   closed。

## Task B：SoM 编号标注截图

新文件：`core/computer/som_annotator.py`；修改：`core/computer/windows_backend.py`

1. 纯函数 `annotate_screenshot(png_bytes: bytes, elements: tuple, scale_x: float, scale_y: float) -> bytes`：
   - 用 PIL 打开 PNG；对每个元素 dict（ref/kind/name/bounds/enabled）：
     bounds 是物理像素，乘 scale_x/scale_y 得图内坐标；
     画红色矩形框（width=2）+ 左上角标签（黑底白字，文字为 ref，如 "el-3"，
     `ImageFont.load_default()`，标签不越出图边界时内缩）；
   - 确定性：不写时间戳/随机数，同输入同输出字节（PIL PNG 编码参数固定）。
2. backend 维护元素有效性代数：`self._elements_surface_generation`，在
   `_dispatch_element_locked` 的 list_elements 分支记录当前
   `self._surface_generation`；observe 构造处，当
   `self._pending_elements` 或「上代元素表仍有效」且代数未变时，对编码后的
   PNG 执行标注再存储。具体规则：
   - `list_elements` 后紧邻的 observe：标注（pending 路径，现状已附加 elements）；
   - 后续 observe：若 `self._last_elements_generation == self._surface_generation`
     且元素表非空 → 同样标注并附加 elements；代数变化 → 清空元素表
     （`self._element_locator` 侧的 ref 同时失效：调用新方法
     `locator.invalidate()` 清空映射），观察退回普通截图。
   - `WindowsElementLocator` 加 `invalidate()`（锁内清 `self._elements`）。
3. 注意 observe 里元素 bounds 用的 scale：ComputerObservation 已有
   scale_x/scale_y（original→model），标注直接乘。
4. 测试：标注函数确定性（同输入两次字节一致）、元素框越界钳制、backend
   list_elements 后 observe 的 png 与普通 observe 不同且 elements 已附加、
   代数变化后元素表清空且观察无 elements。

## Task C：输入前置激活

文件：`core/computer/win32_adapter.py`、`core/computer/windows_backend.py`

1. `Win32InputAdapter` 增两个 seam（构造参数注入，默认 ctypes 实现）：
   - `window_from_point(x: int, y: int) -> int`：user32.WindowFromPoint(POINT)
     → HWND（restype 显式 c_void_p，64 位截断教训见 win32_adapter 既有注释）；
   - `set_foreground_window(hwnd: int) -> bool`：user32.SetForegroundWindow；
     失败时用经典 Alt 键技巧（keybd_event VK_MENU down+up）后重试一次，
     仍失败返回 False（best-effort，绝不抛错阻断输入）。
2. backend `_dispatch_locked`：pointer 类动作（click/double_click/drag）在发
   输入前：取目标点物理坐标 → `window_from_point` → 若 ≠
   `foreground_window_id()` → `set_foreground_window` → `time.sleep(0.15)`；
   键盘类动作（type_text/keypress）：若 `self._last_observation` 存在且
   surface_id 形如 "hwnd:N" → 解析 hwnd → 非前台则激活+等待。
   激活是否发生记录到 `self._last_activation_changed: bool`。
3. `ComputerObservation` 加字段 `input_activated: bool = False`（仅本地使用，
   **绝不**加入 `observation_provider_projection` 投影——投影字节不能变）；
   backend observe 构造后 `replace(observation, input_activated=...)`。
4. FakeWin32Adapter（tests/computer_fakes.py）补齐两个新方法的记录版。
5. 测试：目标窗口非前台时 click 前发生了 set_foreground_window 调用且顺序
   在 send_mouse 之前；已是前台时不调用；set_foreground 失败不阻断输入。

## Task D：no-op 点击自愈 + guard 文案

文件：`core/computer/controller.py`、`core/harness/tool_pipeline.py`

1. controller.execute 成功提交路径：若 `action.kind == "click"` 且
   `observation.input_activated` 且本步 `observation.screen_sha256` 与
   `run.last_screen_sha256`（进入本步前的值）完全一致 → 判定激活吞击：
   在同一 execute 内用同一 action 再执行一次 backend.execute + 重新 observe，
   以第二次观察为准（仍计 1 个 ordinal；第二次前后同样复核取消）。
   最多补击一次。注意先保存旧 screen_sha256 再更新基线。
2. `_repeat_call_blocked_result` 的拦截文案追加一句：
   「若该工具是 computer_action，窗口可能已移动，请先 list_elements 或 observe
   重建状态再操作。」
3. 测试：吞击场景（fake backend 第一击 screen_sha256 不变 + input_activated）
   自动补击恰好一次；非吞击（screen 变化）不补击；补击后取消事件置位则抛
   computer_cancelled。

## Task E：动态提示改革（元素优先学说）

文件：`core/computer/provider_generic.py`

`_GENERIC_DYNAMIC_PROMPT` 重写为（保持中文、保持每行一个要点结构）：

```
电脑操控使用说明（generic 协议）：
1. 第一次必须先调用 computer_action 的 observe 建立观察基线。
2. 原生应用窗口优先用 list_elements 拿控件列表：用 click_element 点击、
   set_value_element 写输入框（中文直接写值不过输入法）；标注截图里的
   编号（el-N）就是元素引用；只有元素不可达（自绘界面/远程/游戏）才退回
   截图坐标点击。
3. 每次只调用一个 computer_action，不得与其他工具混用；有把握的连续微动作
   （点击→输入→按键）用 kind=sequence 打包一次执行（中途不截图，完成后返回
   标注截图）；没把握时单独执行并逐次验证。
4. 每个动作后依据最新观察决定下一步，不得猜测成功；观察里的
   surface_generation 变化表示画面已切换，旧坐标与旧元素引用立即作废。
5. 出现焦点被抢、会话失活、窗口挪位时：先 list_elements 或 activate_surface
   重建基线，严禁按记忆里的旧坐标连续重试。
6. 遇到登录、验证码或需要凭据时，改用 request_user_takeover 请求用户接管。
7. 完成任务后输出正常最终结论，停止调用电脑工具，不继续无意义观察。
```

同步更新 `tests/test_computer_provider_generic.py` 与
`tests/test_computer_elements.py` 里对提示词的断言。

## Task F'：GUI 焦点礼让（外科手术，涉及用户 WIP 文件）

文件：`gui_bridge.py`（用户有未提交 WIP，最小化改动）

1. 先 grep `gui_bridge.py`、`gui_main.py`、`frontend/src` 里的
   `bring_to_front|focus|activate|topmost|show_window`，找出电脑操控事件
   （`computer_observation`/`computer_state`）派发链路上任何会把主窗口提到
   前台的调用点。
2. 在该调用点加守卫：`from core.computer.controller import get_computer_control_service`
   的 `snapshot().status == "running"`（或 paused/waiting_for_user）时跳过
   提前（惰性导入，放函数内）。只改这一处逻辑，不动其他代码、不重排格式。
3. 测试：GUI 层不便单测的话，写一个纯逻辑守卫函数（如
   `gui_bridge._gui_may_raise_window()`）并单测之；调用点替换为该函数。

## 收尾验收

1. `venv\Scripts\python -m pytest tests -q` 全绿（已知存量失败
   test_gui_model_settings 的 generic 文案断言是用户 WIP 所致，不修不提交，
   在总结里注明即可）。
2. 真机验收（需电脑操控开关开启）：让 ycode 执行「打开微信，给 AI交流进步群
   发一条测试消息」，验收基线：**≤15 次模型往返、≤5 分钟**；再测一件
   「打开记事本写一句话保存」≤3 分钟。实测数据（往返数/耗时/失败模式）写进
   交付总结；达不到基线则按数据继续修，不许凭感觉收工。
3. 交付总结列出：每任务提交哈希、全量测试结果、真机实测数据、遗留问题。

---

# 提示词结束

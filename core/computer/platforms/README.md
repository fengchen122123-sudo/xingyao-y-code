# 电脑操控平台接入

状态：Windows 使用已有实现；macOS/Linux 仅预留接口，明确不可用。平台隔离在 Windows 上用模拟测试验证，尚不代表通过 Mac/Linux 真机验收。

## 文件职责

- `__init__.py`：固定解析 sys.platform，win32 → Windows、darwin → macOS、linux → Linux；未知系统显式不可用。没有动态插件注册。
- `base.py`：ComputerPlatformAdapter 协议与未实现平台的显式拒绝行为。
- `windows.py`：将已有 Windows 桌面、热键、预检和浏览器安装探测连接到公共接口。导入模块不会截屏、启动浏览器或注册热键。
- `windows_surfaces.py`（2026-09-05 R10）：Windows 语义定位（窗口枚举/激活 + UIA 托盘候选，专属 COM 线程）与受控浏览器窗口标题提示（`browser_window_hints`，R5 受控页裁决的系统侧信号）。
- `macos.py`、`linux.py`：后续各系统实现的位置；当前所有执行入口抛 computer_unsupported_os。

## 平台必须提供的契约

| 成员 | 要求 |
|---|---|
| name | windows / macos / linux；未知系统为 unsupported |
| supported | 表示整套接入已经实现；不等于当前授权和运行环境预检通过 |
| unavailable_reason | 不可用的明确原因，供设置和错误展示 |
| find_browser_executable(browser) | 只查询指定 edge/chrome，返回路径或空串；不启动、不安装、不切换浏览器 |
| create_desktop_backend() | 返回实现 models.ComputerBackend 的对象；构造不输入、不截图 |
| create_hotkey_api() | 返回实现 hotkey.HotkeyApi 的对象；注册在 register 阶段执行 |
| preflight_desktop() | 校验平台授权、交互会话、坐标环境和停止能力；失败显式抛领域错误 |
| probe_hotkey() | 已有有效注册可复用；否则探测并清理；不支持的平台显式失败 |
| create_surface_locator() | 返回实现 surfaces.SurfaceLocator 的对象；构造不枚举窗口、不初始化 COM；未实现平台显式抛 computer_unsupported_os |
| browser_window_hints(pid) | 受控浏览器进程可见窗口标题提示 (hwnd, title, is_foreground)；只读标题；未实现平台显式抛错 |

实际调用链：settings → 平台支持与浏览器探测；Controller 默认工厂 → 平台门禁及桌面构造；runtime → 热键 API 构造；GUI → 平台预检。浏览器执行继续共用 BrowserComputerBackend。平台不可用时，默认工厂在构造 browser/desktop 之前拒绝。

公共动作、ComputerObservation、Controller、Provider、事件和配置格式保持原有契约。META 是逻辑键名，具体系统键由桌面后端映射。当前统一停止热键仍为 Ctrl+Alt+Esc；若以后更换组合键，须同步热键注册、展示与测试，不能只改平台实现。

## 实现 macOS 时

替换 MacOSPlatformAdapter 的未实现状态，为截图、输入、软件/窗口访问和热键提供本机实现；处理系统授权、Retina 缩放、显示器原点、前台切换及 Command 等键映射。平台代码单独依赖本机库，不能让 Windows/Linux 导入时加载它们。授权缺失要明确报告，不能返回空截图或假成功。

## 实现 Linux 时

在 LinuxPlatformAdapter 内明确识别和选择支持的图形会话，X11 与 Wayland 分别实现截图、输入授权与紧急停止。不能只判断 sys.platform == linux 就放行，也不能在 Wayland 失败后静默切到 X11 或假设 XWayland 能控制整个桌面。无图形会话或未实现的会话类型必须显式拒绝。

## 启用条件与测试

1. 实现全部平台契约，再设置 supported=True；不要仅为显示开关而改变此标记。
2. 公共单测通过 fake backend/input/hotkey 验证，不能从 pytest 发出真实输入。
3. 在目标系统单独进行预检、点击/输入/滚动、前后台切换、坐标映射、热键、暂停停止、取消和权限撤销真机验收。
4. 浏览器需验证所选系统通道与独立 profile，保持现有 BrowserComputerBackend 单 worker 线程归属。
5. 保留 settings/capability 的平台门禁，并检查目标系统的打包依赖、UI 文案和 smoke 脚本。当前 capability 和部分 UI 仍说明“目前仅支持 Windows”，正式启用新平台时要更新；这次预留不改 Provider 提示词或 tools 字节。

语义定位与局部观察动作（list_surfaces/activate_surface/invoke_surface/observe_region）已加入统一动作契约（desktop only，2026-09-05 协议扩展）。Windows 实现经 WindowsSurfaceLocator（UIA 依赖 comtypes，仅 win32 安装）。以后添加此类动作时，需要设计跨系统动作契约并运行 Provider byte-golden，不能绕过固定 Harness 或直接往模型输出注入本机脚本。

验证命令：

```powershell
venv\Scripts\python.exe -m pytest tests/test_computer_platforms.py tests/test_computer_settings.py tests/test_gui_system_settings.py -q
venv\Scripts\python.exe -m pytest tests -k computer -q
venv\Scripts\python.exe -m pytest tests/test_prompt_byte_stability.py tests/test_provider_payload_golden.py -q
```

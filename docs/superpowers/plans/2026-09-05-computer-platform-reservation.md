# 电脑操控跨平台扩展点 Implementation Plan

> 执行方式：当前任务内按步骤实施；不创建子任务、不提交 Git。

**Goal:** 为用户已确认的 Windows/macOS/Linux 独立后端方案建立实际代码接入点。

**Architecture:** 保留 ComputerBackend、动作协议、Controller 和 Provider。新增固定平台适配层，集中桌面构造、预检、浏览器探测和 HotkeyApi 构造；Windows 适配器调用已有实现。macOS/Linux 显式报告未实现，调用执行入口立即抛 computer_unsupported_os。

**Tech Stack:** Python Protocol、现有 Win32/Pillow/Playwright；不新增依赖。

## 约束

- 本次只预留接入，macOS/Linux 不宣称已可用，不加载 Windows 原语。
- 固定内核内置路由，不提供插件注册或自动换系统后端。
- Windows 设置字段、默认浏览器、profile 路径、提示词/tools/Provider 请求保持原样。
- 保留前次审计产物，既有可靠性缺陷不在本次顺带修复。

## 实施步骤

- [x] 新增 `tests/test_computer_platforms.py`，验证 win32/darwin/linux/未知系统路由、不可用平台显式失败、Windows 惰性委托、设置/控制器/runtime/GUI 共用入口；运行取得失败证据。
- [x] 新建 `core/computer/platforms/base.py`：`ComputerPlatformAdapter` 协议提供 name/supported/unavailable_reason 和 `find_browser_executable(browser)`、`create_desktop_backend()`、`create_hotkey_api()`、`preflight_desktop()`、`probe_hotkey()`。
- [x] 新建 Windows/macOS/Linux 模块及固定 `get_platform_adapter(platform=None)` 解析入口。Windows 模块仅在方法内导入本机执行依赖；另两平台使用显式不可用实现。
- [x] settings 的探测与平台支持判断、controller 默认桌面工厂、runtime 默认热键工厂、GUI 两个 preflight 接入平台层。原有 Windows 浏览器探测测试仅迁移 mock 路径，不修改断言。
- [x] 运行新增测试与电脑操控相关回归，执行编译与 diff 检查。生产提示词与请求未修改，额外运行现有 byte-golden 验证边界。
- [x] 写入平台接入说明，明确 macOS 权限/Retina 与 Linux X11/Wayland 需独立实现和真机验收。

验收示例：`get_platform_adapter('darwin').supported is False`，调用其 `create_desktop_backend()` 抛稳定错误；替换平台解析入口为注入适配器时，settings/controller/runtime/GUI 全部使用注入实现，无直接 Windows 调用。

## 执行记录

- 首轮新增测试：21 个失败，主要为平台模块尚不存在；随后实现并转绿。
- 补充默认工厂门禁测试：macOS/Linux 的 browser 两例出现 DID NOT RAISE，增加构造前平台门禁后转绿。
- 最终电脑操控相关回归：697 passed，1951 deselected。
- 平台/设置/GUI 定向：96 passed（与上述相关回归部分重叠，不累加）。
- prompt byte-stability / Provider byte-golden：19 passed。
- 变更生产模块 py_compile 和 git diff --check 通过。
- 未运行真实键鼠，未安装 Mac/Linux 依赖；未修复前次审计中的现有操作缺陷。

变更清单：新增 core/computer/platforms/ 下五个 Python 模块与 README；修改 controller.py、runtime.py、settings.py 和 gui_bridge.py 的平台接线；新增 tests/test_computer_platforms.py；迁移 test_computer_settings.py 与 test_gui_system_settings.py 的 Windows 浏览器探测 mock 路径，原断言保留。

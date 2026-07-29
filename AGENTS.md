# Y-CODE TUI Development Guidelines

This file outlines the standard commands, code styles, and guidelines for developing and maintaining the Y-CODE TUI project.

## 🛠️ Build & Run Commands

### Run Application
```bash
# Run using default python (venv should be activated)
python main.py

# Run explicitly with venv python on Windows
venv\Scripts\python main.py
```

### Validate Syntax (Compile Check)
```bash
venv\Scripts\python -m py_compile main.py core/llm.py core/memory.py core/memory_compressor.py core/themes.py
```

---

## 🔌 Plugin Architecture

ycode 支持进程内插件扩展工具能力，无需修改核心调度逻辑。

### Plugin Layout
```
plugins/<plugin_name>/          # 内置插件（随仓库发布）
  plugin.json                   # {"name","version","description","entry","enabled"}
  plugin.py                     # 必须暴露 register(registry) 函数
~/.ycode/plugins/<plugin_name>/ # 用户级插件（同名覆盖内置）
```

- 加载器：`core/plugin_loader.py`，由 `core/tool_registry.py:build_default_registry()` 末尾自动调用。
- 插件扫描结果（含禁用/失败/被覆盖）由 `plugin_loader.get_plugin_infos()` 提供，TUI 内用 `/plugins` 命令查看。
- 单插件异常只记录日志并跳过，绝不影响 ycode 启动。
- 插件内兄弟模块用 `from core.plugin_loader import import_sibling; m = import_sibling(__file__, "module")` 加载。
- 插件工具通过 `ToolSpec` 元数据自动获得权限拦截、schema 参数过滤与结果截断。

---

## 🎨 Code Style & Quality Guidelines

1. **Terminal Output Constraints**:
   - Since the TUI terminal interface prints raw text without a Markdown renderer, **never** output markdown formatting such as `**` (bold) or `*` (italic) around words. Use clean indentations, capitalization, or extra spacing to highlight text.
2. **Concurrency and UI Thread-safety**:
   - Any background worker threads or asynchronous helper threads **must not** directly touch global layout variables, call `print_formatted_text`, or run `app.invalidate()`.
   - Always route UI updates back to the main prompt_toolkit thread loop using `app_instance.loop.call_soon_threadsafe(callback)`.
3. **Database Transactions**:
   - Ensure you commit all SQLite operations using `conn.commit()` at the end of functions to prevent thread blockages or data loss.

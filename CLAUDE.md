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

### Run Tests
```bash
venv\Scripts\python -m pytest tests/ -q
```

### Build Windows Installer (打包安装包)
完整流程文档见 docs/BUILD.md（首次打包必读：环境准备、产物说明、常见问题）。
日常一条命令：

```bash
venv\Scripts\python build_release.py
```

产出 dist\ycode_setup.exe（主安装包，安装 GUI + 可选 TUI）。要点：
- 前端改过 → 加 --rebuild-frontend；只改 Python → 加 --skip-frontend 提速。
- 打包所需文件全部在版本库内：build_release.py、ycode.spec、
  ycode_setup.spec、ycode_uninstall.spec、ycode_setup.py、ycode_uninstall.py、
  packaging/runtime_hook_nullstdout.py、config.example.json、system_prompt.md、
  frontend/（Vue 源码）。不要移动或重命名它们；版本号只改 core/version.py。
- 构建产物 dist/、build/、frontend/dist 均被 .gitignore 忽略，永远不要提交。

---

## 🎨 Code Style & Quality Guidelines

1. **Terminal Output Constraints**:
   - Since the TUI terminal interface prints raw text without a Markdown renderer, **never** output markdown formatting such as `**` (bold) or `*` (italic) around words. Use clean indentations, capitalization, or extra spacing to highlight text.
2. **Concurrency and UI Thread-safety**:
   - Any background worker threads or asynchronous helper threads **must not** directly touch global layout variables, call `print_formatted_text`, or run `app.invalidate()`.
   - Always route UI updates back to the main prompt_toolkit thread loop using `app_instance.loop.call_soon_threadsafe(callback)`.
3. **Database Transactions**:
   - Ensure you commit all SQLite operations using `conn.commit()` at the end of functions to prevent thread blockages or data loss.

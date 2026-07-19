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

## 🎨 Code Style & Quality Guidelines

1. **Terminal Output Constraints**:
   - Since the TUI terminal interface prints raw text without a Markdown renderer, **never** output markdown formatting such as `**` (bold) or `*` (italic) around words. Use clean indentations, capitalization, or extra spacing to highlight text.
2. **Concurrency and UI Thread-safety**:
   - Any background worker threads or asynchronous helper threads **must not** directly touch global layout variables, call `print_formatted_text`, or run `app.invalidate()`.
   - Always route UI updates back to the main prompt_toolkit thread loop using `app_instance.loop.call_soon_threadsafe(callback)`.
3. **Database Transactions**:
   - Ensure you commit all SQLite operations using `conn.commit()` at the end of functions to prevent thread blockages or data loss.

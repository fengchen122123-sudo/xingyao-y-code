"""让测试收集阶段就与真实 ~/.ycode 运行数据完全隔离。"""

import atexit
import gc
import logging
import os
import shutil
import sys
import tempfile


_TEST_DATA_DIR = tempfile.mkdtemp(prefix="ycode-pytest-")
os.environ["YCODE_DATA_DIR"] = _TEST_DATA_DIR
atexit.register(shutil.rmtree, _TEST_DATA_DIR, ignore_errors=True)


def pytest_sessionfinish(session, exitstatus):
    """Windows 下先释放真实持有句柄，再删除隔离目录。"""
    managers = []
    main_module = sys.modules.get("main")
    if main_module is not None:
        managers.append(getattr(main_module, "manager", None))
    try:
        from core import app_context
        managers.append(app_context.get_manager())
    except Exception:
        pass
    for manager in managers:
        if manager is not None and hasattr(manager, "close"):
            try:
                manager.close()
            except Exception:
                pass

    warning_logger = logging.getLogger("py.warnings")
    for handler in list(warning_logger.handlers):
        if not getattr(handler, "_ycode_runtime_warning_handler", False):
            continue
        warning_logger.removeHandler(handler)
        handler.close()
    gc.collect()
    shutil.rmtree(_TEST_DATA_DIR, ignore_errors=True)

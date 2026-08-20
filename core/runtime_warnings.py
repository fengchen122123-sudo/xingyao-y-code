"""Route host-process warnings to a diagnostic file instead of the CLI."""

from __future__ import annotations

import logging
import os
from pathlib import Path


DEFAULT_WARNING_LOG = Path(
    os.environ.get("YCODE_DATA_DIR") or (Path.home() / ".ycode")
) / "runtime-warnings.log"
_HANDLER_MARKER = "_ycode_runtime_warning_handler"


def configure_runtime_warnings(log_path=None):
    """Capture Python warnings without hiding them from diagnostics."""
    target = Path(log_path) if log_path is not None else DEFAULT_WARNING_LOG
    target.parent.mkdir(parents=True, exist_ok=True)

    warning_logger = logging.getLogger("py.warnings")
    for handler in list(warning_logger.handlers):
        # 只移除/close 本模块自己添加的 handler（带标记属性），
        # 绝不动第三方库挂在 py.warnings 上的 handler
        if not getattr(handler, _HANDLER_MARKER, False):
            continue
        warning_logger.removeHandler(handler)
        handler.close()

    handler = logging.FileHandler(target, encoding="utf-8")
    setattr(handler, _HANDLER_MARKER, True)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    warning_logger.addHandler(handler)
    warning_logger.setLevel(logging.WARNING)
    warning_logger.propagate = False
    # Re-hook even when another runtime replaced warnings.showwarning after an
    # earlier captureWarnings(True) call.
    logging.captureWarnings(False)
    logging.captureWarnings(True)
    return target

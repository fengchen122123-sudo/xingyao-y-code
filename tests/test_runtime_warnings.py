import importlib.util
import logging
import warnings
from pathlib import Path


def test_runtime_warnings_are_logged_without_reaching_terminal(tmp_path, capsys):
    module_path = Path("core/runtime_warnings.py")
    assert module_path.exists(), "运行时 Warning 路由模块尚未实现"

    spec = importlib.util.spec_from_file_location("runtime_warnings_under_test", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    warning_logger = logging.getLogger("py.warnings")
    original_handlers = list(warning_logger.handlers)
    original_level = warning_logger.level
    original_propagate = warning_logger.propagate
    log_path = tmp_path / "runtime-warnings.log"

    try:
        module.configure_runtime_warnings(log_path)
        warnings.warn("third-party runtime warning", RuntimeWarning)

        captured = capsys.readouterr()
        assert "third-party runtime warning" not in captured.err
        assert "third-party runtime warning" in log_path.read_text(encoding="utf-8")
    finally:
        logging.captureWarnings(False)
        for handler in list(warning_logger.handlers):
            if handler not in original_handlers:
                warning_logger.removeHandler(handler)
                handler.close()
        warning_logger.handlers = original_handlers
        warning_logger.setLevel(original_level)
        warning_logger.propagate = original_propagate


def test_main_configures_warning_routing_before_third_party_imports():
    main_source = Path("main.py").read_text(encoding="utf-8")

    configure_position = main_source.index("configure_runtime_warnings()")
    third_party_position = main_source.index("import pyfiglet")

    assert configure_position < third_party_position

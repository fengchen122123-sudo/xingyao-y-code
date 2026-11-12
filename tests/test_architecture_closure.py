"""architecture_report.py closure 子命令的单元测试。

口径：入口的传递依赖闭包（只算包内模块）× 模块数 / 行数 / 最大深度；
超基线 +10% 告警（整数比较 current*10 > baseline*11）。
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "architecture_report.py"


@pytest.fixture(scope="module")
def report():
    name = "architecture_report_under_test"
    spec = importlib.util.spec_from_file_location(name, _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    # dataclass 定义需要模块已登记在 sys.modules 里，否则 @dataclass 取不到 __module__
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


@pytest.fixture()
def package(tmp_path: Path) -> Path:
    pkg = tmp_path / "pkg"
    _write(pkg / "__init__.py", "")
    _write(pkg / "a.py", "import pkg.b\n")
    _write(pkg / "b.py", "from pkg import c\n")
    _write(pkg / "c.py", "VALUE = 1\n")
    _write(pkg / "d.py", "OTHER = 2\n")
    return pkg


def test_closure_counts_transitive_modules(report, package):
    metrics, missing = report.compute_closure_metrics(
        package, entries=("pkg.a", "pkg.d", "pkg.nope")
    )
    assert missing == ["pkg.nope"]
    assert metrics["pkg.a"]["module_count"] == 3   # a → b → c
    assert metrics["pkg.a"]["max_depth"] == 2
    assert metrics["pkg.a"]["total_lines"] == 3
    assert metrics["pkg.d"]["module_count"] == 1
    assert metrics["pkg.d"]["max_depth"] == 0


def test_closure_ignores_stdlib_and_third_party(report, tmp_path):
    pkg = tmp_path / "solo"
    _write(pkg / "__init__.py", "")
    _write(pkg / "main.py", "import json\nimport os.path\nimport requests\n")
    metrics, _ = report.compute_closure_metrics(pkg, entries=("solo.main",))
    assert metrics["solo.main"]["module_count"] == 1


def test_closure_alerts_on_growth(report):
    current = {"pkg.a": {"module_count": 12, "total_lines": 1200, "max_depth": 3}}
    baseline = {"pkg.a": {"module_count": 10, "total_lines": 1000, "max_depth": 3}}
    alerts = report.find_closure_alerts(current, baseline)
    kinds = {(entry, metric) for entry, metric, _cur, _old in alerts}
    assert ("pkg.a", "total_lines") in kinds
    assert ("pkg.a", "module_count") in kinds


def test_closure_no_alert_within_ratio(report):
    current = {"pkg.a": {"module_count": 10, "total_lines": 1100, "max_depth": 3}}
    baseline = {"pkg.a": {"module_count": 10, "total_lines": 1000, "max_depth": 3}}
    assert report.find_closure_alerts(current, baseline) == []


def test_closure_first_seen_entry_never_alerts(report):
    current = {"pkg.new": {"module_count": 50, "total_lines": 5000, "max_depth": 9}}
    assert report.find_closure_alerts(current, {}) == []


def test_save_closure_baseline_preserves_files_section(report, tmp_path):
    target = tmp_path / "baseline.json"
    target.write_text(
        json.dumps({"schema": 1, "files": {"core/a.py": 10}}, ensure_ascii=False),
        encoding="utf-8",
    )
    report.save_closure_baseline(target, {"pkg.a": {"module_count": 3, "total_lines": 3, "max_depth": 2}})
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["files"] == {"core/a.py": 10}
    assert data["closures"]["pkg.a"]["module_count"] == 3
    # 确定性：两次写入字节一致
    first = target.read_bytes()
    report.save_closure_baseline(target, {"pkg.a": {"module_count": 3, "total_lines": 3, "max_depth": 2}})
    assert target.read_bytes() == first
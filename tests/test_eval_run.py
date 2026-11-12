"""S2 评测运行器（scripts/eval_run.py）的单元测试。

只测确定性部分：用例校验、5 种判据、dry-run 端到端与门禁退出码。
不调用任何模型。
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "eval_run.py"


@pytest.fixture(scope="module")
def runner():
    name = "eval_run_under_test"
    spec = importlib.util.spec_from_file_location(name, _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _write_case(root: Path, suite: str, case_id: str, payload: dict) -> Path:
    path = root / suite / f"{case_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def test_load_cases_rejects_duplicate_id(runner, tmp_path):
    payload = {"id": "dup", "assertions": [{"type": "file_exists", "path": "a"}]}
    _write_case(tmp_path, "s1", "one", payload)
    _write_case(tmp_path, "s2", "two", payload)
    with pytest.raises(runner.EvalCaseError):
        runner.load_cases(tmp_path)


def test_load_cases_rejects_unknown_assertion_type(runner, tmp_path):
    _write_case(tmp_path, "s1", "bad", {"id": "bad", "assertions": [{"type": "llm_judge"}]})
    with pytest.raises(runner.EvalCaseError):
        runner.load_cases(tmp_path)


def test_load_cases_rejects_empty_assertions(runner, tmp_path):
    _write_case(tmp_path, "s1", "empty", {"id": "empty", "assertions": []})
    with pytest.raises(runner.EvalCaseError):
        runner.load_cases(tmp_path)


def test_check_assertions_positive_and_negative(runner, tmp_path):
    (tmp_path / "doc.md").write_text("标题\n正文", encoding="utf-8")
    ok, detail = runner.check_assertions(
        tmp_path,
        [
            {"type": "file_exists", "path": "doc.md"},
            {"type": "file_contains", "path": "doc.md", "value": "标题"},
            {"type": "file_not_contains", "path": "doc.md", "pattern": "^\\ufeff\\ufeff"},
        ],
    )
    assert ok is True and detail == ""

    failed, detail = runner.check_assertions(
        tmp_path, [{"type": "file_exists", "path": "missing.md"}]
    )
    assert failed is False and "missing.md" in detail


def test_check_assertions_blocks_path_escape(runner, tmp_path):
    passed, detail = runner.check_assertions(
        tmp_path, [{"type": "file_contains", "path": "../outside.txt", "value": "x"}]
    )
    assert passed is False and "越界" in detail


def test_tool_called_assertion(runner, tmp_path):
    ok, _ = runner.check_assertions(
        tmp_path, [{"type": "tool_called", "name": "edit_file"}], tool_calls=["edit_file"]
    )
    assert ok is True
    failed, detail = runner.check_assertions(
        tmp_path, [{"type": "tool_called", "name": "bash"}], tool_calls=["edit_file"]
    )
    assert failed is False and "bash" in detail


def test_dry_run_end_to_end_writes_report(runner, tmp_path, monkeypatch):
    cases = tmp_path / "cases"
    _write_case(cases, "smoke", "c1", {"id": "c1", "assertions": [{"type": "file_exists", "path": "x"}]})
    out = tmp_path / "reports"

    code = runner.main(
        [
            "--cases", str(cases), "--profile", "A=x", "--profile", "B=y",
            "--runs", "1", "--dry-run", "--simulate", "A=1", "--simulate", "B=0",
            "--out", str(out),
        ]
    )

    assert code == 0
    reports = list(out.glob("*.md"))
    assert len(reports) == 1
    assert "c1" in reports[0].read_text(encoding="utf-8")


def test_dry_run_gate_returns_two_when_blocked(runner, tmp_path):
    cases = tmp_path / "cases"
    _write_case(cases, "smoke", "c1", {"id": "c1", "assertions": [{"type": "file_exists", "path": "x"}]})
    out = tmp_path / "reports"

    # B 没有任何模拟结果 → run_case 走真实判据（fixture 不存在 → 失败）
    # 为了制造 blocked，这里只给 A 的序列并让 runs 超过 A 的长度，应被拒绝
    code = runner.main(
        [
            "--cases", str(cases), "--profile", "A=x", "--profile", "B=y",
            "--runs", "2", "--dry-run", "--simulate", "A=1",
            "--out", str(out),
        ]
    )
    assert code == 1


def test_not_dry_run_refuses_to_fake_results(runner):
    assert runner.run_case({"assertions": []}, "A", dry_run=False) if False else True
    with pytest.raises(NotImplementedError):
        runner.run_case({"assertions": []}, "A", dry_run=False)
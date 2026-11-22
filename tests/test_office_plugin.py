# -*- coding: utf-8 -*-

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytest.importorskip("openpyxl", reason="office_write 依赖 openpyxl（见 requirements.txt）")

from core.workspace_context import workspace_scope  # noqa: E402


def _registry():
    from core.tool_registry import build_default_registry

    return build_default_registry()


def _office_write_spec():
    specs = [spec for spec in _registry().all() if spec.name == "office_write"]
    assert specs, "office_write 未注册进默认注册表"
    return specs[0]


def _sheets():
    return [{"name": "销售", "rows": [["月份", "金额"], ["1月", 100], ["2月", 200]]}]


def test_office_write_is_listed_in_recorder_coverage():
    from core.turn_undo import service as tu_service

    spec = _office_write_spec()
    assert spec.mutates_files is True
    assert "office_write" in tu_service._RECORDED_FIRST_PARTY_TOOLS
    assert "office_write" not in tu_service.validate_recorder_coverage(_registry())


def test_office_write_degrades_to_plain_write_without_checkpoint(tmp_path):
    spec = _office_write_spec()
    target = tmp_path / "报表.xlsx"
    with workspace_scope(str(tmp_path)):
        result = spec.execute(path=str(target), sheets=_sheets())

    assert not result.is_error, result.content
    assert target.exists()

    from openpyxl import load_workbook

    wb = load_workbook(str(target))
    assert wb["销售"]["A1"].value == "月份"
    assert wb["销售"]["B3"].value == 200


def test_office_write_refuses_overwrite_without_flag(tmp_path):
    spec = _office_write_spec()
    target = tmp_path / "报表.xlsx"
    target.write_bytes(b"original-bytes")
    with workspace_scope(str(tmp_path)):
        result = spec.execute(path=str(target), sheets=_sheets())

    assert result.is_error
    assert target.read_bytes() == b"original-bytes"


def test_office_write_rejects_non_xlsx(tmp_path):
    spec = _office_write_spec()
    target = tmp_path / "报告.docx"
    with workspace_scope(str(tmp_path)):
        result = spec.execute(path=str(target), sheets=_sheets())

    assert result.is_error
    assert "xlsx" in result.content
    assert not target.exists()

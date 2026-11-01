# -*- coding: utf-8 -*-
"""plugins/office 写入工具的按回合撤销接入用例（规格 9.5）。

覆盖：
- office_write 声明 mutates_files，且已列入记录器覆盖白名单（启动校验通过）
- 无活动检查点时降级为裸写，产物可被 openpyxl 读回
- 目标已存在且未传 overwrite 时拒绝，原文件字节不变
- 非 .xlsx 扩展名直接拒绝

依赖：office_write 需要 openpyxl；未安装时整组跳过（requirements.txt 已声明）。
"""

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
    """声明了 mutates_files 就必须在覆盖白名单里，否则启动校验直接失败。"""
    from core.turn_undo import service as tu_service

    spec = _office_write_spec()
    assert spec.mutates_files is True
    assert "office_write" in tu_service._RECORDED_FIRST_PARTY_TOOLS
    assert "office_write" not in tu_service.validate_recorder_coverage(_registry())


def test_office_write_degrades_to_plain_write_without_checkpoint(tmp_path):
    """没有活动检查点（CLI / 测试 / 未开启撤销）时按裸写降级，产物照常可用。"""
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
    """覆盖保护：未传 overwrite 时一个字节都不碰。"""
    spec = _office_write_spec()
    target = tmp_path / "报表.xlsx"
    target.write_bytes(b"original-bytes")
    with workspace_scope(str(tmp_path)):
        result = spec.execute(path=str(target), sheets=_sheets())

    assert result.is_error
    assert target.read_bytes() == b"original-bytes"


def test_office_write_rejects_non_xlsx(tmp_path):
    """只写 .xlsx；docx/pptx 直出不支持，要明确拒绝而不是产出坏文件。"""
    spec = _office_write_spec()
    target = tmp_path / "报告.docx"
    with workspace_scope(str(tmp_path)):
        result = spec.execute(path=str(target), sheets=_sheets())

    assert result.is_error
    assert "xlsx" in result.content
    assert not target.exists()

"""
tests/test_build_release_env_check.py
=====================================
构建环境硬校验单测：build_release.verify_spec_collect_all_targets 必须把
"构建环境缺失的 collect_all 目标"拦在 PyInstaller Analysis 之前。

collect_all 对构建环境缺失的包静默返回空三元组（spec 不报错、Analysis 照常
通过），产物会直接丢掉该依赖——曾让打包版 GUI 内嵌终端启动即报
No module named 'winpty'，且整个构建流程无任何失败信号。
"""

import importlib.util

import build_release


def _patch_find_spec(monkeypatch, missing_names):
    """把 importlib.util.find_spec 换成查表桩：missing_names 里的包视为不存在，
    其余回落到真实实现（避免破坏测试运行器自身的 import 机制）。"""
    real_find_spec = importlib.util.find_spec

    def fake_find_spec(name, *args, **kwargs):
        if name in missing_names:
            return None
        return real_find_spec(name, *args, **kwargs)

    monkeypatch.setattr(importlib.util, "find_spec", fake_find_spec)


def test_collect_all_targets_from_spec_parses_quote_styles_and_dedupes(tmp_path):
    spec = tmp_path / "ycode.spec"
    spec.write_text(
        "a = collect_all('litellm')\n"
        "b = collect_all(  'litellm'  )\n"
        'c = collect_all("tiktoken_ext")\n',
        encoding="utf-8",
    )
    assert build_release.collect_all_targets_from_spec(str(spec)) == [
        "litellm",
        "tiktoken_ext",
    ]


def test_collect_all_targets_from_spec_skips_comment_lines(tmp_path):
    """整行注释里的 collect_all 字样是文档不是配置，不能解析成假目标。"""
    spec = tmp_path / "ycode.spec"
    spec.write_text(
        "# 以前这里写过 collect_all('fake_comment_pkg')\n"
        "d, e, f = collect_all('winpty')  # 行尾注释 collect_all('also_fake')\n",
        encoding="utf-8",
    )
    assert build_release.collect_all_targets_from_spec(str(spec)) == ["winpty"]


def test_missing_target_is_reported(monkeypatch):
    _patch_find_spec(monkeypatch, {"winpty"})
    assert build_release.find_missing_collect_all_targets(["litellm", "winpty"]) == [
        "winpty"
    ]


def test_all_present_reports_nothing(monkeypatch):
    _patch_find_spec(monkeypatch, set())
    assert build_release.find_missing_collect_all_targets(["litellm", "winpty"]) == []


def test_find_spec_import_error_counts_as_missing(monkeypatch):
    """find_spec 对父包缺失等场景抛 ImportError，应按缺失处理而不是外溢中断构建。"""
    def raising_find_spec(name, *args, **kwargs):
        raise ImportError("No module named 'parent'")

    monkeypatch.setattr(importlib.util, "find_spec", raising_find_spec)
    assert build_release.find_missing_collect_all_targets(["parent.child"]) == [
        "parent.child"
    ]


def test_real_spec_targets_importable_in_build_env():
    """真实 ycode.spec 的全部 collect_all 目标在当前测试环境必须可 import。

    与构建入口 verify_spec_collect_all_targets 同一道闸：依赖清单或 spec
    新增 collect_all 而环境没跟上时，跑测试即可发现，不必等打包失败。
    """
    import os

    project_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    targets = build_release.collect_all_targets_from_spec(
        os.path.join(project_dir, "ycode.spec")
    )
    assert "winpty" in targets  # 防止 spec 改名导致本测试空转
    assert build_release.find_missing_collect_all_targets(targets) == []

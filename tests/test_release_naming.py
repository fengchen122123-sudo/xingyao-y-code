"""
tests/test_release_naming.py
============================
发布最终分发名单测：build_release.versioned_setup_name 决定落到 dist/ 的
安装包名（主安装包 XingYaoCode-{version}，CLI 独立包 Y-code-cli-{version}）。
"""

import build_release


def test_versioned_setup_name_main_installer_uses_new_brand():
    assert build_release.versioned_setup_name("ycode_setup", "1.2.28") == "XingYaoCode-1.2.28"


def test_versioned_setup_name_cli_installer_keeps_ycode_cli_name():
    assert build_release.versioned_setup_name("ycode_cli_setup", "1.2.28") == "Y-code-cli-1.2.28"


def test_versioned_setup_name_unknown_base_falls_back():
    """非安装包基名走兜底模板，避免新产物静默套用主包品牌名。"""
    assert build_release.versioned_setup_name("ycode_uninstall", "1.2.28") == "ycode_uninstall_v1.2.28"

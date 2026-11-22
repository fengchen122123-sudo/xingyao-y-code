import importlib.util

import build_release


def _patch_find_spec(monkeypatch, missing_names):
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
    def raising_find_spec(name, *args, **kwargs):
        raise ImportError("No module named 'parent'")

    monkeypatch.setattr(importlib.util, "find_spec", raising_find_spec)
    assert build_release.find_missing_collect_all_targets(["parent.child"]) == [
        "parent.child"
    ]


def test_real_spec_targets_importable_in_build_env():
    import os

    project_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    targets = build_release.collect_all_targets_from_spec(
        os.path.join(project_dir, "ycode.spec")
    )
    assert "winpty" in targets
    assert build_release.find_missing_collect_all_targets(targets) == []


def test_find_missing_core_bundle_files_catches_missing_webview_arch(tmp_path):
    for rel_path in build_release.REQUIRED_CORE_BUNDLE_FILES:
        target = tmp_path / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"dll")

    assert build_release.find_missing_core_bundle_files(str(tmp_path)) == []

    arm64_dll = tmp_path / "_internal" / "webview" / "lib" / "runtimes" / "win-arm64" / "native" / "WebView2Loader.dll"
    arm64_dll.unlink()
    missing = build_release.find_missing_core_bundle_files(str(tmp_path))
    assert len(missing) == 1
    assert "win-arm64" in missing[0]


def test_verify_core_bundle_files_exits_when_incomplete(tmp_path):
    import pytest

    with pytest.raises(SystemExit) as exc_info:
        build_release.verify_core_bundle_files(str(tmp_path))
    assert exc_info.value.code == 1

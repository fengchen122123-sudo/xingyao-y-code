import os
from pathlib import Path

import config
import core.skills as skills


def _skill(path: Path, name: str, description: str, body: str) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n{body}\n",
        encoding="utf-8",
    )
    parsed = skills.parse_skill_md(str(path))
    assert parsed is not None
    return parsed


def test_active_skill_catalog_exposes_metadata_without_body(tmp_path, monkeypatch):
    skill_path = tmp_path / "skills" / "demo" / "SKILL.md"
    parsed = _skill(skill_path, "demo", "Demo skill", "SECRET BODY")
    monkeypatch.setattr(skills, "get_active_skill_ids", lambda: [str(skill_path)])
    monkeypatch.setattr(skills, "scan_all_skills", lambda: [parsed])

    catalog = skills.get_active_skills_catalog()

    assert "demo" in catalog
    assert "Demo skill" in catalog
    assert str(skill_path) in catalog
    assert "SECRET BODY" not in catalog


def test_default_skill_scan_uses_configured_ycode_dir(tmp_path, monkeypatch):
    configured_dir = tmp_path / "isolated-ycode"
    configured_skills = configured_dir / "skills"
    configured_skills.mkdir(parents=True)
    legacy_home = tmp_path / "legacy-home"
    (legacy_home / ".ycode" / "skills").mkdir(parents=True)

    monkeypatch.setattr(config, "YCODE_DIR", str(configured_dir))
    monkeypatch.setattr(skills, "sync_builtin_skills", lambda: None)
    monkeypatch.setattr(skills.os.path, "expanduser", lambda _value: str(legacy_home))

    paths = [Path(path).resolve() for path in skills.get_skill_dirs()]

    assert configured_skills.resolve() in paths
    assert (legacy_home / ".ycode" / "skills").resolve() not in paths


def test_active_skill_catalog_is_stable_and_compatibility_api_is_lazy(
    tmp_path, monkeypatch
):
    z_path = tmp_path / "skills" / "zeta" / "SKILL.md"
    a_path = tmp_path / "skills" / "alpha" / "SKILL.md"
    available = [
        _skill(z_path, "zeta", "Z skill", "Z BODY"),
        _skill(a_path, "alpha", "A skill", "A BODY"),
    ]
    monkeypatch.setattr(
        skills,
        "get_active_skill_ids",
        lambda: [str(z_path), str(a_path)],
    )
    monkeypatch.setattr(skills, "scan_all_skills", lambda: available)

    catalog = skills.get_active_skills_catalog()

    assert catalog.index("alpha") < catalog.index("zeta")
    assert "A BODY" not in catalog
    assert "Z BODY" not in catalog
    assert skills.get_active_skills_markdown() == catalog


def test_active_skill_ids_atomic_write_and_backup_recovery(tmp_path, monkeypatch):
    active_file = tmp_path / "active_skills.json"
    monkeypatch.setattr(skills, "ACTIVE_SKILLS_FILE", str(active_file))

    ids = ["C:/skills/demo/SKILL.md", "C:/skills/alpha/SKILL.md"]
    skills.save_active_skill_ids(ids)
    assert skills.get_active_skill_ids() == ids

    skills.save_active_skill_ids(ids)
    assert (tmp_path / "active_skills.json.bak").exists()
    active_file.write_text("[broken", encoding="utf-8")

    assert skills.get_active_skill_ids() == ids


def test_catalog_header_is_action_directive_not_passive_note(tmp_path, monkeypatch):
    skill_path = tmp_path / "skills" / "demo" / "SKILL.md"
    parsed = _skill(skill_path, "demo", "Demo skill", "BODY")
    monkeypatch.setattr(skills, "get_active_skill_ids", lambda: [str(skill_path)])
    monkeypatch.setattr(skills, "scan_all_skills", lambda: [parsed])

    catalog = skills.get_active_skills_catalog()

    assert "先扫一遍本目录" in catalog
    assert "哪怕轻微相关" in catalog
    assert "不要为了确认是否相关而加载无关技能" not in catalog


def test_catalog_appends_trigger_hint_for_english_description(tmp_path, monkeypatch):
    en_path = tmp_path / "skills" / "systematic-debugging" / "SKILL.md"
    zh_path = tmp_path / "skills" / "mes-db" / "SKILL.md"
    en = _skill(en_path, "systematic-debugging", "Use when encountering any bug", "BODY")
    zh = _skill(zh_path, "mes-db", "连接并操作本地 SQL Server 数据库", "BODY")
    monkeypatch.setattr(skills, "get_active_skill_ids", lambda: [str(en_path), str(zh_path)])
    monkeypatch.setattr(skills, "scan_all_skills", lambda: [en, zh])

    catalog = skills.get_active_skills_catalog()

    assert "（触发:排查/报错/异常/bug）" in catalog
    zh_line = next(line for line in catalog.splitlines() if "mes-db" in line)
    assert "触发:" not in zh_line


def test_frontmatter_triggers_override_builtin_hints(tmp_path, monkeypatch):
    skill_path = tmp_path / "skills" / "systematic-debugging" / "SKILL.md"
    skill_path.parent.mkdir(parents=True, exist_ok=True)
    skill_path.write_text(
        "---\nname: systematic-debugging\ndescription: Use when debugging\ntriggers: 自定义触发词\n---\nBODY\n",
        encoding="utf-8",
    )
    parsed = skills.parse_skill_md(str(skill_path))
    monkeypatch.setattr(skills, "get_active_skill_ids", lambda: [str(skill_path)])
    monkeypatch.setattr(skills, "scan_all_skills", lambda: [parsed])

    catalog = skills.get_active_skills_catalog()

    assert "（触发:自定义触发词）" in catalog
    assert "排查/报错" not in catalog


def test_skill_catalog_bounds_untrusted_metadata(tmp_path, monkeypatch):
    skill_path = tmp_path / "skills" / "demo" / "SKILL.md"
    parsed = _skill(
        skill_path,
        "n" * (skills.SKILL_NAME_MAX_CHARS + 20),
        "d" * (skills.SKILL_DESCRIPTION_MAX_CHARS + 20),
        "BODY",
    )
    parsed["triggers"] = "t" * (skills.SKILL_TRIGGERS_MAX_CHARS + 20)
    monkeypatch.setattr(skills, "get_active_skill_ids", lambda: [str(skill_path)])
    monkeypatch.setattr(skills, "scan_all_skills", lambda: [parsed])

    catalog = skills.get_active_skills_catalog()

    assert "n" * (skills.SKILL_NAME_MAX_CHARS + 1) not in catalog
    assert "d" * (skills.SKILL_DESCRIPTION_MAX_CHARS + 1) not in catalog
    assert "t" * (skills.SKILL_TRIGGERS_MAX_CHARS + 1) not in catalog


def test_scan_skills_rejects_symlink_outside_search_root(tmp_path, monkeypatch):
    root = tmp_path / "skills"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "SKILL.md").write_text(
        "---\nname: escaped\ndescription: outside\n---\nBODY\n",
        encoding="utf-8",
    )
    try:
        (root / "escaped").symlink_to(outside, target_is_directory=True)
    except OSError:
        import pytest
        pytest.skip("当前 Windows 环境不允许创建测试符号链接")
    monkeypatch.setattr(skills, "get_skill_dirs", lambda: [str(root)])

    assert skills.scan_all_skills() == []


def _source_env(tmp_path, monkeypatch):
    src = tmp_path / "builtin_src"
    src.mkdir()
    ycode_dir = tmp_path / "user_home" / ".ycode"
    ycode_dir.mkdir(parents=True)
    monkeypatch.setattr(skills, "_BUILTIN_SYNC_DONE", False)
    monkeypatch.setattr(skills, "_builtin_skills_dir", lambda: str(src))
    monkeypatch.setattr(config, "YCODE_DIR", str(ycode_dir))
    monkeypatch.setattr(skills, "ACTIVE_SKILLS_FILE", str(tmp_path / "active_skills.json"))
    return src, ycode_dir / "skills", ycode_dir


def _write_skill(skill_dir: Path, name: str, body: str = "BODY") -> Path:
    skill_dir.mkdir(parents=True, exist_ok=True)
    path = skill_dir / "SKILL.md"
    path.write_text(
        f"---\nname: {name}\ndescription: {name} skill\n---\n{body}\n",
        encoding="utf-8",
    )
    return path


def _freeze_tree_mtime(root: Path, ts: float = 1_700_000_000.0) -> None:
    for entry in root.rglob("*"):
        os.utime(entry, (ts, ts))
    os.utime(root, (ts, ts))


def test_classify_skill_source_builtin_when_fingerprint_matches_index(tmp_path, monkeypatch):
    src, _user, _yc = _source_env(tmp_path, monkeypatch)
    skill_dir = tmp_path / "skills" / "demo"
    _write_skill(skill_dir, "demo")
    fingerprint = skills._skill_dir_fingerprint(str(skill_dir))
    assert fingerprint

    index = {"demo": fingerprint}

    assert skills.classify_skill_source(str(skill_dir), index) == "builtin"


def test_classify_skill_source_user_when_indexed_but_content_modified(tmp_path, monkeypatch):
    src, _user, _yc = _source_env(tmp_path, monkeypatch)
    skill_dir = tmp_path / "skills" / "demo"
    _write_skill(skill_dir, "demo")
    recorded = skills._skill_dir_fingerprint(str(skill_dir))

    (skill_dir / "SKILL.md").write_text("用户改写内容", encoding="utf-8")

    assert skills.classify_skill_source(str(skill_dir), {"demo": recorded}) == "user"


def test_classify_skill_source_user_when_name_absent_from_index(tmp_path, monkeypatch):
    src, _user, _yc = _source_env(tmp_path, monkeypatch)
    skill_dir = tmp_path / "skills" / "mine"
    _write_skill(skill_dir, "mine")

    assert skills.classify_skill_source(str(skill_dir), {}) == "user"
    assert skills.classify_skill_source(str(skill_dir), {"other": "x"}) == "user"


def test_classify_skill_source_builtin_when_under_builtin_dir(tmp_path, monkeypatch):
    src, _user, _yc = _source_env(tmp_path, monkeypatch)
    skill_dir = src / "demo"
    _write_skill(skill_dir, "demo")

    assert skills.classify_skill_source(str(skill_dir), {}) == "builtin"


def test_scan_all_skills_each_item_exposes_valid_source(tmp_path, monkeypatch):
    src, _user, _yc = _source_env(tmp_path, monkeypatch)
    skills_root = tmp_path / "skills"
    builtin_dir = skills_root / "demo"
    user_dir = skills_root / "mine"
    _write_skill(builtin_dir, "demo")
    _write_skill(user_dir, "mine")

    config._atomic_write_json(
        skills._builtin_skills_index_file(),
        {"demo": skills._skill_dir_fingerprint(str(builtin_dir))},
        indent=None,
    )
    monkeypatch.setattr(skills, "get_skill_dirs", lambda: [str(skills_root)])

    found = skills.scan_all_skills()

    assert len(found) == 2
    for info in found:
        assert "source" in info
        assert info["source"] in {"builtin", "user"}
    sources = {info["name"]: info["source"] for info in found}
    assert sources["demo"] == "builtin"
    assert sources["mine"] == "user"


def test_sync_indexes_only_unmodified_released_skills(tmp_path, monkeypatch):
    src, user_skills, _yc = _source_env(tmp_path, monkeypatch)
    _write_skill(src / "plugin-creator", "plugin-creator", body="内置正文")
    _write_skill(src / "user-mod", "user-mod", body="内置正文")
    _write_skill(user_skills / "user-mod", "user-mod", body="用户自建内容")

    skills.sync_builtin_skills()

    index = skills.load_builtin_skill_index()
    assert "plugin-creator" in index
    assert index["plugin-creator"] == skills._skill_dir_fingerprint(
        str(user_skills / "plugin-creator")
    )
    assert "user-mod" not in index

    assert skills.classify_skill_source(str(user_skills / "plugin-creator")) == "builtin"
    assert skills.classify_skill_source(str(user_skills / "user-mod")) == "user"

def test_scan_all_skills_classifies_builtin_on_first_scan_before_index_exists(
    tmp_path, monkeypatch
):
    import os
    import shutil

    src, dest, _ycode_dir = _source_env(tmp_path, monkeypatch)
    skill_name = "plugin-creator"
    _write_skill(src / skill_name, skill_name, body="builtin body")

    dest.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src / skill_name, dest / skill_name)
    config._atomic_write_json(skills._builtin_released_file(), [skill_name], indent=None)
    index_file = skills._builtin_skills_index_file()
    assert not os.path.exists(index_file)

    monkeypatch.setattr(skills, "get_user_skill_search_paths", lambda: [])
    monkeypatch.setattr(skills.os.path, "expanduser", lambda _value: str(tmp_path / "fake_home"))

    found = skills.scan_all_skills()

    by_name = {info["name"]: info for info in found}
    assert skill_name in skills.load_builtin_skill_index()


def test_fingerprint_ignores_dependency_directories(tmp_path):
    skill_dir = tmp_path / "skills" / "demo"
    _write_skill(skill_dir, "demo")
    vendor = skill_dir / "scripts" / "node_modules" / "pkg"
    vendor.mkdir(parents=True)
    (vendor / "index.js").write_text("module.exports = 1;\n", encoding="utf-8")
    (skill_dir / "dist").mkdir()
    (skill_dir / "dist" / "bundle.js").write_text("bundle\n", encoding="utf-8")

    before = skills._skill_dir_fingerprint(str(skill_dir))
    assert before

    (vendor / "index.js").write_text("module.exports = 2;\n", encoding="utf-8")
    (vendor / "extra.js").write_text("extra\n", encoding="utf-8")
    (skill_dir / "dist" / "bundle.js").unlink()
    assert skills._skill_dir_fingerprint(str(skill_dir)) == before

    (skill_dir / "SKILL.md").write_text("本体改动", encoding="utf-8")
    assert skills._skill_dir_fingerprint(str(skill_dir)) != before


def test_fingerprint_cache_hit_and_invalidation(tmp_path, monkeypatch):
    skill_dir = tmp_path / "skills" / "demo"
    _write_skill(skill_dir, "demo")
    (skill_dir / "sub").mkdir()
    (skill_dir / "sub" / "note.md").write_text("note\n", encoding="utf-8")
    _freeze_tree_mtime(skill_dir)

    calls = []
    real_uncached = skills._skill_dir_fingerprint_uncached

    def counting(skill_dir_arg):
        calls.append(skill_dir_arg)
        return real_uncached(skill_dir_arg)

    monkeypatch.setattr(skills, "_skill_dir_fingerprint_uncached", counting)

    first = skills._skill_dir_fingerprint(str(skill_dir))
    second = skills._skill_dir_fingerprint(str(skill_dir))
    assert first and first == second
    assert len(calls) == 1

    (skill_dir / "sub" / "note.md").write_text("note changed\n", encoding="utf-8")
    third = skills._skill_dir_fingerprint(str(skill_dir))
    assert len(calls) == 2
    assert third != first


def test_skill_source_label_normalizes_unknown_to_user():
    assert skills.skill_source_label({"source": "builtin"}) == "内置"
    assert skills.skill_source_label({"source": "user"}) == "用户"
    assert skills.skill_source_label({}) == "用户"
    assert skills.skill_source_label({"source": None}) == "用户"
    assert skills.skill_source_label({"source": "BUILTIN"}) == "用户"
    assert skills.skill_source_label(None) == "用户"


def test_gui_bridge_refresh_skills(tmp_path, monkeypatch):
    import gui_bridge
    from gui_bridge import ApiBridge

    skill_path = tmp_path / "skills" / "refreshed_demo" / "SKILL.md"
    parsed = _skill(skill_path, "refreshed_demo", "Refreshed demo", "BODY")
    monkeypatch.setattr(gui_bridge, "get_active_skill_ids", lambda: [str(skill_path)])
    monkeypatch.setattr(gui_bridge, "scan_all_skills", lambda: [parsed])

    bridge = ApiBridge.__new__(ApiBridge)
    res = bridge.refresh_skills()

    assert res["ok"] is True
    assert len(res["skills"]) == 1
    assert res["skills"][0]["name"] == "refreshed_demo"
    assert res["skills"][0]["enabled"] is True

    direct = bridge.get_skills()
    assert len(direct) == 1
    assert direct[0]["name"] == "refreshed_demo"


def test_delete_skill_removes_directory_and_active_id(tmp_path, monkeypatch):
    root = tmp_path / "skills"
    skill_dir = root / "demo"
    path = _write_skill(skill_dir, "demo")
    (skill_dir / "notes.txt").write_text("gone", encoding="utf-8")
    monkeypatch.setattr(skills, "get_skill_dirs", lambda: [str(root)])
    monkeypatch.setattr(skills, "ACTIVE_SKILLS_FILE", str(tmp_path / "active.json"))
    skills.save_active_skill_ids([str(path)])

    result = skills.delete_skill(str(path))

    assert result["ok"] is True
    assert not skill_dir.exists()
    assert skills.get_active_skill_ids() == []


def test_delete_skill_rejects_path_outside_search_root(tmp_path, monkeypatch):
    root = tmp_path / "skills"
    root.mkdir()
    outside = tmp_path / "outside" / "demo"
    path = _write_skill(outside, "demo")
    monkeypatch.setattr(skills, "get_skill_dirs", lambda: [str(root)])

    result = skills.delete_skill(str(path))

    assert result["ok"] is False
    assert path.is_file()


def test_delete_skill_rejects_builtin_source(tmp_path, monkeypatch):
    src, _user, _yc = _source_env(tmp_path, monkeypatch)
    path = _write_skill(src / "bundled", "bundled")
    monkeypatch.setattr(skills, "get_skill_dirs", lambda: [str(src)])

    result = skills.delete_skill(str(path))

    assert result["ok"] is False
    assert path.is_file()

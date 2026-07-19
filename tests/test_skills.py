from pathlib import Path

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
    """active_skills.json 原子写可回读；主文件损坏时从 .bak 恢复而不是静默返回 []"""
    active_file = tmp_path / "active_skills.json"
    monkeypatch.setattr(skills, "ACTIVE_SKILLS_FILE", str(active_file))

    ids = ["C:/skills/demo/SKILL.md", "C:/skills/alpha/SKILL.md"]
    skills.save_active_skill_ids(ids)
    assert skills.get_active_skill_ids() == ids

    # 再写一次以生成 .bak，然后破坏主文件
    skills.save_active_skill_ids(ids)
    assert (tmp_path / "active_skills.json.bak").exists()
    active_file.write_text("[broken", encoding="utf-8")

    assert skills.get_active_skill_ids() == ids

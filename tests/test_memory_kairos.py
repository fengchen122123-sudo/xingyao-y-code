import json

import core.memory_kairos as mk


class _FakeMessage:
    def __init__(self, content):
        self.content = content


class _FakeChoice:
    def __init__(self, content):
        self.message = _FakeMessage(content)


class _FakeResponse:
    def __init__(self, content):
        self.choices = [_FakeChoice(content)]


def _run_dream_with_actions(tmp_path, monkeypatch, actions):
    """在隔离的记忆目录下，用假 LLM 输出驱动一次 dream 整理。"""
    memory_dir = tmp_path / "memory"
    monkeypatch.setattr(mk, "MEMORY_DIR", memory_dir)
    (memory_dir / "logs").mkdir(parents=True)
    (memory_dir / "logs" / "today.md").write_text("- 记了一笔\n", encoding="utf-8")

    import litellm
    monkeypatch.setattr(
        litellm, "completion",
        lambda **kwargs: _FakeResponse(json.dumps(actions, ensure_ascii=False)),
    )
    return memory_dir, mk.run_dream_consolidation_sync("model", "key")


def test_memory_filename_path_traversal_is_rejected(tmp_path, monkeypatch):
    """LLM 输出的恶意文件名（路径穿越）必须被拒绝，不得写出记忆目录之外"""
    escape_target = tmp_path / "evil.md"
    memory_dir, result = _run_dream_with_actions(tmp_path, monkeypatch, [
        {"action": "create_or_update", "filename": "../evil.md", "content": "恶意内容"},
        {"action": "create_or_update", "filename": "sub/dir.md", "content": "恶意内容"},
        {"action": "create_or_update", "filename": "good.md", "content": "正常内容"},
    ])

    assert not escape_target.exists(), "路径穿越文件不应被写出记忆目录"
    assert not (memory_dir / "sub").exists(), "不允许写入子目录结构"
    assert "拒绝非法记忆文件名" in result
    # 合法文件正常写入
    assert (memory_dir / "good.md").read_text(encoding="utf-8") == "正常内容"


def test_is_safe_memory_filename():
    assert mk._is_safe_memory_filename("rules.md")
    assert not mk._is_safe_memory_filename("../x.md")
    assert not mk._is_safe_memory_filename("a/b.md")
    assert not mk._is_safe_memory_filename("a\\b.md")
    assert not mk._is_safe_memory_filename("")
    assert not mk._is_safe_memory_filename(None)


def test_memory_md_index_cannot_be_overwritten(tmp_path, monkeypatch):
    """索引文件 MEMORY.md 不允许被 create_or_update 整体覆写（绕过去重合并逻辑）"""
    memory_dir, result = _run_dream_with_actions(tmp_path, monkeypatch, [
        {"action": "create_or_update", "filename": "MEMORY.md", "content": "伪造的索引"},
        {"action": "create_or_update", "filename": "memory.md", "content": "小写也不行"},
    ])

    assert "拒绝" in result
    idx = memory_dir / "MEMORY.md"
    assert not idx.exists(), "索引文件不应被 create_or_update 覆写创建"


def test_ensure_memory_dir_creates_all_subdirectories(tmp_path, monkeypatch):
    """ensure_memory_dir 应创建系统提示词里承诺的所有子目录。

    复现场景：系统提示词说"directory already exists, write to it directly"，
    但旧版只创建 MEMORY_DIR 和 logs/，主 Agent 写 feedback/xxx.md 时
    触发 FileNotFoundError。修复后应一次创建全部子目录。
    """
    memory_dir = tmp_path / "memory"
    monkeypatch.setattr(mk, "MEMORY_DIR", memory_dir)

    mk.ensure_memory_dir()

    # 系统提示词承诺的目录必须全部存在
    assert memory_dir.exists()
    assert (memory_dir / "logs").exists()
    assert (memory_dir / "user").exists()
    assert (memory_dir / "feedback").exists()
    assert (memory_dir / "project").exists()
    assert (memory_dir / "reference").exists()


def test_ensure_memory_dir_is_idempotent(tmp_path, monkeypatch):
    """ensure_memory_dir 重复调用不应报错（exist_ok=True）"""
    memory_dir = tmp_path / "memory"
    monkeypatch.setattr(mk, "MEMORY_DIR", memory_dir)

    mk.ensure_memory_dir()
    mk.ensure_memory_dir()  # 不应抛异常

    assert (memory_dir / "feedback").exists()


"""回合收尾折叠会话全部电脑操控观察图（提速计划 Task 6）。"""

from __future__ import annotations

import json

import pytest

from core.memory import MemoryManager


@pytest.fixture()
def mm(tmp_path):
    return MemoryManager(db_path=str(tmp_path / "fold.db"))


def _insert_observation(manager, session_id, with_image=True):
    content = [
        {"type": "text", "text": "【电脑观察】请只依据这张最新截图决定下一步。"},
    ]
    if with_image:
        content.append(
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}
        )
    manager.conn.execute(
        "INSERT INTO messages(session_id, role, content) VALUES (?, 'user', ?)",
        (
            session_id,
            json.dumps(
                {
                    "role": "user",
                    "content": content,
                    "_computer_v1": {"run_id": "r", "ordinal": 1, "target": "desktop"},
                },
                ensure_ascii=False,
            ),
        ),
    )
    manager.conn.commit()


def test_fold_removes_all_computer_images_including_latest(mm):
    _insert_observation(mm, "s-fold", with_image=True)
    _insert_observation(mm, "s-fold", with_image=True)
    folded = mm.fold_session_computer_media("s-fold")
    assert folded == 2
    rows = mm.conn.execute(
        "SELECT content FROM messages WHERE session_id=?", ("s-fold",)
    ).fetchall()
    for (raw,) in rows:
        assert "image_url" not in raw
        assert "已折叠" in raw
        # _computer_v1 私有元数据原样保留
        assert "_computer_v1" in raw


def test_fold_is_idempotent(mm):
    _insert_observation(mm, "s-fold2")
    assert mm.fold_session_computer_media("s-fold2") == 1
    assert mm.fold_session_computer_media("s-fold2") == 0


def test_fold_never_touches_plain_media_messages(mm):
    """无 _computer_v1 的普通多模态消息不触碰（职责归 _fold_stale_media_locked）。"""
    mm.conn.execute(
        "INSERT INTO messages(session_id, role, content) VALUES (?, 'user', ?)",
        (
            "s-fold3",
            json.dumps([{"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]),
        ),
    )
    mm.conn.commit()
    assert mm.fold_session_computer_media("s-fold3") == 0
    raw = mm.conn.execute(
        "SELECT content FROM messages WHERE session_id=?", ("s-fold3",)
    ).fetchone()[0]
    assert "image_url" in raw

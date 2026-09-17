"""
tests/test_session_search.py
=============================
测试会话全局检索功能（标题模糊匹配 + 对话内容全文检索）。
"""

import os
import tempfile
import pytest
from core.memory import MemoryManager
from gui_bridge import ApiBridge


@pytest.fixture
def temp_memory():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    mem = MemoryManager(db_path=db_path)
    yield mem
    mem.conn.close()
    if os.path.exists(db_path):
        try:
            os.remove(db_path)
        except OSError:
            pass


def test_search_sessions_title_match(temp_memory):
    # 创建测试会话
    temp_memory.ensure_session("session_api_router", workdir="GLOBAL", source="GUI")
    temp_memory.ensure_session("session_database_migration", workdir="/tmp/project", source="GUI")
    temp_memory.add_message("session_api_router", "user", "开始工作")
    temp_memory.add_message("session_database_migration", "user", "开始数据库迁移")

    # 1. 搜索标题 "api"
    results = temp_memory.search_sessions_and_messages("api", scope="title")
    assert len(results) == 1
    assert results[0]["session_name"] == "session_api_router"
    assert results[0]["type"] == "title"

    # 2. 搜索标题 "migration"
    results2 = temp_memory.search_sessions_and_messages("migration", scope="title")
    assert len(results2) == 1
    assert results2[0]["session_name"] == "session_database_migration"

    # 3. 搜索不存在的标题
    results3 = temp_memory.search_sessions_and_messages("nonexistent", scope="title")
    assert len(results3) == 0


def test_search_sessions_content_match_and_snippet(temp_memory):
    temp_memory.ensure_session("session_debug", workdir="GLOBAL", source="GUI")
    # 添加含有关键词的消息
    temp_memory.add_message(
        "session_debug",
        "user",
        "请帮我调试一下这个函数，传入参数是 sk-1234567890abcdef1234567890 密钥的时候会报错。",
    )
    temp_memory.add_message(
        "session_debug",
        "assistant",
        "这个报错的原因是参数格式校验失败，我们需要使用 APIRouter 进行重新封装。",
    )

    # 1. 检索内容 "APIRouter"
    res = temp_memory.search_sessions_and_messages("APIRouter", scope="content")
    assert len(res) >= 1
    assert res[0]["type"] == "content"
    assert res[0]["role"] == "assistant"
    assert "APIRouter" in res[0]["snippet"]

    # 2. 检索并测试敏感信息脱敏 (sk- 开头的密钥)
    res_key = temp_memory.search_sessions_and_messages("密钥", scope="content")
    assert len(res_key) >= 1
    snippet = res_key[0]["snippet"]
    assert "sk-***" in snippet
    assert "sk-1234567890abcdef" not in snippet


def test_search_sessions_scope_filter(temp_memory):
    temp_memory.ensure_session("session_unique_title", workdir="GLOBAL", source="GUI")
    temp_memory.add_message("session_unique_title", "user", "这里有一段普通的对话内容。")

    temp_memory.ensure_session("session_other", workdir="GLOBAL", source="GUI")
    temp_memory.add_message("session_other", "user", "我们在消息里提到了 unique_title 这个词。")

    # scope="title" 应该只匹配 session_unique_title
    res_title = temp_memory.search_sessions_and_messages("unique_title", scope="title")
    assert len(res_title) == 1
    assert res_title[0]["session_name"] == "session_unique_title"
    assert res_title[0]["type"] == "title"

    # scope="content" 应该只匹配 session_other
    res_content = temp_memory.search_sessions_and_messages("unique_title", scope="content")
    assert len(res_content) == 1
    assert res_content[0]["session_name"] == "session_other"
    assert res_content[0]["type"] == "content"

    # scope="all" 两者都匹配
    res_all = temp_memory.search_sessions_and_messages("unique_title", scope="all")
    assert len(res_all) == 2


def test_search_sessions_workdir_filter(temp_memory):
    temp_memory.ensure_session("s_global", workdir="GLOBAL", source="GUI")
    temp_memory.add_message("s_global", "user", "公共通用关键词")

    temp_memory.ensure_session("s_proj", workdir="/workspace/demo", source="GUI")
    temp_memory.add_message("s_proj", "user", "项目内通用关键词")

    # 过滤 GLOBAL
    res_global = temp_memory.search_sessions_and_messages("通用关键词", workdir="GLOBAL")
    assert len(res_global) == 1
    assert res_global[0]["session_name"] == "s_global"

    # 不限工作区
    res_all = temp_memory.search_sessions_and_messages("通用关键词", workdir=None)
    assert len(res_all) == 2


def test_api_bridge_search_sessions_full(temp_memory):
    bridge = ApiBridge()
    bridge._memory = temp_memory
    temp_memory.ensure_session("session_fastapi", workdir="GLOBAL", source="GUI")
    temp_memory.add_message("session_fastapi", "user", "如何使用 FastAPI 实现流式响应？")

    # 调用 bridge 接口
    res = bridge.search_sessions_full(query="FastAPI", scope="all")
    assert res["ok"] is True
    assert res["total"] >= 1
    hit = res["results"][0]
    assert hit["display_name"] == "session_fastapi"
    assert hit["workdir_label"] == "自由对话"

    # 空查询返回空列表
    empty_res = bridge.search_sessions_full(query="")
    assert empty_res["ok"] is True
    assert empty_res["total"] == 0
    assert empty_res["results"] == []


def test_search_wildcard_escaping(temp_memory):
    temp_memory.ensure_session("session_percent_100%", workdir="GLOBAL", source="GUI")
    temp_memory.ensure_session("session_underscore_test", workdir="GLOBAL", source="GUI")
    temp_memory.ensure_session("session_normal", workdir="GLOBAL", source="GUI")

    # 1. 搜索包含 % 的关键词
    res_percent = temp_memory.search_sessions_and_messages("100%", scope="title")
    assert len(res_percent) == 1
    assert res_percent[0]["session_name"] == "session_percent_100%"

    # 2. 搜索单个下划线 _，不应作为通配符匹配全部字符
    res_under = temp_memory.search_sessions_and_messages("_test", scope="title")
    assert len(res_under) == 1
    assert res_under[0]["session_name"] == "session_underscore_test"


def test_search_limit(temp_memory):
    temp_memory.ensure_session("s_bulk", workdir="GLOBAL", source="GUI")
    for i in range(20):
        temp_memory.add_message("s_bulk", "user", f"批量测试消息编号 {i} 包含同一个标志 flag_123")

    res = temp_memory.search_sessions_and_messages("flag_123", scope="content", limit=5)
    # 同一会话最多提取 2 条
    assert len(res) <= 2


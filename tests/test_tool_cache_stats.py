"""Tests for tool cache hit/miss statistics (core.tool_cache)."""

import tempfile

from core import tool_cache


def test_stats_initial_empty_and_zero_hit_rate():
    tool_cache.reset_stats()
    stats = tool_cache.get_stats()
    assert stats["tools"] == {}
    assert stats["total_hits"] == 0
    assert stats["total_misses"] == 0
    assert stats["hit_rate"] == 0.0


def test_read_file_hit_and_miss_counted():
    tool_cache.reset_stats()
    key = tool_cache.read_file_key("no-such-file-for-stats.txt", 0, None, False)
    # miss
    assert tool_cache.read_file_get(key) is None
    # hit
    tool_cache.read_file_put(key, "content")
    assert tool_cache.read_file_get(key) == "content"

    stats = tool_cache.get_stats()
    assert stats["tools"]["read_file"] == {"hits": 1, "misses": 1}


def test_search_code_and_find_definition_counted_separately():
    tool_cache.reset_stats()
    with tempfile.TemporaryDirectory() as tmpdir:
        sc_key = tool_cache.search_key("search_code", "foo", tmpdir)
        fd_key = tool_cache.search_key("find_definition", "MyClass", tmpdir, (".py",))

        assert tool_cache.search_get(sc_key) is None  # search_code miss
        tool_cache.search_put(sc_key, "result")
        assert tool_cache.search_get(sc_key) == "result"  # search_code hit

        assert tool_cache.search_get(fd_key) is None  # find_definition miss
        tool_cache.search_put(fd_key, "result")
        assert tool_cache.search_get(fd_key) == "result"  # find_definition hit

    stats = tool_cache.get_stats()
    assert stats["tools"]["search_code"] == {"hits": 1, "misses": 1}
    assert stats["tools"]["find_definition"] == {"hits": 1, "misses": 1}


def test_glob_hit_and_miss_counted():
    tool_cache.reset_stats()
    with tempfile.TemporaryDirectory() as tmpdir:
        key = tool_cache.glob_key("*.py", tmpdir)
        assert tool_cache.glob_get(key) is None  # miss
        tool_cache.glob_put(key, "files")
        assert tool_cache.glob_get(key) == "files"  # hit

    assert tool_cache.get_stats()["tools"]["glob"] == {"hits": 1, "misses": 1}


def test_shared_hit_and_miss_counted():
    tool_cache.reset_stats()
    tool_cache.shared_put("s1", ("k", 1), "v")
    assert tool_cache.shared_get("s1", ("k", 1)) == "v"  # hit
    assert tool_cache.shared_get("s1", ("k", 2)) is None  # miss (key absent)
    assert tool_cache.shared_get("s2", ("k", 1)) is None  # miss (no session)

    stats = tool_cache.get_stats()
    assert stats["tools"]["shared"] == {"hits": 1, "misses": 2}


def test_reset_stats_clears_and_restarts():
    tool_cache.reset_stats()
    key = tool_cache.read_file_key("no-such-file-reset.txt", 0, None, False)
    tool_cache.read_file_put(key, "content")
    assert tool_cache.read_file_get(key) == "content"

    tool_cache.reset_stats()
    assert tool_cache.get_stats()["tools"] == {}

    # Cache entries survive reset_stats; a new get counts as a fresh hit.
    assert tool_cache.read_file_get(key) == "content"
    assert tool_cache.get_stats()["tools"]["read_file"] == {"hits": 1, "misses": 0}


def test_total_hit_rate():
    tool_cache.reset_stats()
    key1 = tool_cache.read_file_key("no-such-file-rate-1.txt", 0, None, False)
    key2 = tool_cache.read_file_key("no-such-file-rate-2.txt", 0, None, False)

    # 2 misses
    assert tool_cache.read_file_get(key1) is None
    assert tool_cache.read_file_get(key2) is None
    # 2 hits
    tool_cache.read_file_put(key1, "content")
    tool_cache.read_file_put(key2, "content")
    assert tool_cache.read_file_get(key1) == "content"
    assert tool_cache.read_file_get(key2) == "content"

    stats = tool_cache.get_stats()
    assert stats["total_hits"] == 2
    assert stats["total_misses"] == 2
    assert stats["hit_rate"] == 0.5

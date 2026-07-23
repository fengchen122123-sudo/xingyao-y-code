import os
import tempfile
import time

from core import tool_cache


def test_read_file_cache_invalidation_on_mtime_change():
    tool_cache.clear_all()
    with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False, encoding='utf-8') as f:
        f.write('hello')
        tmp = f.name
    try:
        key = tool_cache.read_file_key(tmp, 0, None, False)
        assert tool_cache.read_file_get(key) is None
        tool_cache.read_file_put(key, 'content')
        assert tool_cache.read_file_get(key) == 'content'

        time.sleep(0.05)
        with open(tmp, 'a', encoding='utf-8') as f:
            f.write('!')
        new_key = tool_cache.read_file_key(tmp, 0, None, False)
        assert new_key != key
        assert tool_cache.read_file_get(new_key) is None
    finally:
        os.unlink(tmp)


def test_search_code_cache():
    tool_cache.clear_all()
    with tempfile.TemporaryDirectory() as tmpdir:
        key = tool_cache.search_key('search_code', 'foo', tmpdir)
        assert tool_cache.search_get(key) is None
        tool_cache.search_put(key, 'result')
        assert tool_cache.search_get(key) == 'result'


def test_find_definition_cache():
    tool_cache.clear_all()
    with tempfile.TemporaryDirectory() as tmpdir:
        key = tool_cache.search_key('find_definition', 'MyClass', tmpdir, ('.py',))
        assert tool_cache.search_get(key) is None
        tool_cache.search_put(key, 'result')
        assert tool_cache.search_get(key) == 'result'


def test_glob_cache():
    tool_cache.clear_all()
    with tempfile.TemporaryDirectory() as tmpdir:
        key = tool_cache.glob_key('*.py', tmpdir)
        assert tool_cache.glob_get(key) is None
        tool_cache.glob_put(key, 'files')
        assert tool_cache.glob_get(key) == 'files'


def test_shared_cache_isolated_by_session():
    tool_cache.shared_clear('s1')
    tool_cache.shared_clear('s2')
    tool_cache.shared_put('s1', ('k', 1), 'v')
    assert tool_cache.shared_get('s1', ('k', 1)) == 'v'
    assert tool_cache.shared_get('s2', ('k', 1)) is None
    tool_cache.shared_clear('s1')
    assert tool_cache.shared_get('s1', ('k', 1)) is None


def test_truncate_bytes_preserves_utf8():
    text = '中文字符' * 10
    out = tool_cache.truncate_bytes(text, 20, 'test')
    assert '截断' in out
    assert '...' in out
    assert out.endswith(']')


def test_truncate_lines():
    lines = [f'line {i}' for i in range(10)]
    out = tool_cache.truncate_lines(lines, 3, 'test')
    assert 'line 0' in out
    assert 'line 2' in out
    assert '已截断' in out
    assert '省略 7 行' in out

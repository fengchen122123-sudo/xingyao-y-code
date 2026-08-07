def test_third_identical_successful_call_is_blocked():
    from core.llm import _RepeatCallGuard
    guard = _RepeatCallGuard()
    sig_args = '{"file_path":"a.py"}'
    assert guard.should_execute("read_file", sig_args) is True
    guard.record("read_file", sig_args, ok=True)
    assert guard.should_execute("read_file", sig_args) is True
    guard.record("read_file", sig_args, ok=True)
    # 第三次同签名成功调用被拦截
    assert guard.should_execute("read_file", sig_args) is False


def test_guard_ignores_failed_calls_and_resets_on_different_args():
    from core.llm import _RepeatCallGuard
    guard = _RepeatCallGuard()
    guard.record("read_file", '{"file_path":"a.py"}', ok=False)
    guard.record("read_file", '{"file_path":"a.py"}', ok=False)
    assert guard.should_execute("read_file", '{"file_path":"a.py"}') is True
    assert guard.should_execute("read_file", '{"file_path":"b.py"}') is True


def test_guard_signature_is_canonical():
    from core.llm import _RepeatCallGuard
    guard = _RepeatCallGuard()
    # key 顺序与空白差异不影响签名
    assert guard.signature("read_file", '{"a": 1, "b": 2}') == guard.signature(
        "read_file", '{"b":2,"a":1}'
    )
    # 非法 JSON 退化为原始字符串签名，不崩溃
    assert guard.signature("read_file", "{oops") == "read_file:{oops"

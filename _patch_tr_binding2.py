"""补丁 2：logout 清进程状态时同时复位 restored 标记（避免退出后还去试探 /auth/me）。"""

from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent
AUTH = ROOT / "core" / "tokenrhythm_auth.py"
TESTS = ROOT / "tests" / "test_tokenrhythm_auth.py"

OLD_STATE = '''        _STATE.update(
            {
                "logged_in": False,
                "challenge_id": None,
                "challenge_phone": None,
                "terms_version": None,
                "terms_fetched_at": 0.0,
            }
        )'''

NEW_STATE = '''        _STATE.update(
            {
                "logged_in": False,
                "challenge_id": None,
                "challenge_phone": None,
                "terms_version": None,
                "terms_fetched_at": 0.0,
                # 本地会话文件随后会被删掉：不再认为"有可恢复的会话"
                "restored": False,
            }
        )'''

OLD_ASSERT = '''    assert order == {"clear_binding": 1, "clear_session": 1}
    assert auth._load_binding() is None
    assert not auth._SESSION_FILE.exists()
    assert auth._STATE["logged_in"] is False'''

NEW_ASSERT = '''    assert order == {"clear_binding": 1, "clear_session": 1}
    assert auth._load_binding() is None
    assert not auth._SESSION_FILE.exists()
    assert auth._STATE["logged_in"] is False
    assert auth._STATE["restored"] is False'''


def replace_once(path: pathlib.Path, old: str, new: str, label: str) -> None:
    text = path.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"[FAIL] {label}：锚点命中 {count} 次")
    path.write_text(text.replace(old, new, 1), encoding="utf-8", newline="\n")
    print(f"[OK] {label}")


def main() -> int:
    replace_once(AUTH, OLD_STATE, NEW_STATE, "_reset_state 复位 restored")
    replace_once(TESTS, OLD_ASSERT, NEW_ASSERT, "logout 用例补 restored 断言")
    print("[DONE]")
    return 0


if __name__ == "__main__":
    sys.exit(main())

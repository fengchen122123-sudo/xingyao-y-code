import os
import subprocess
import sys
import tempfile

PYTHON = sys.executable
WC_TOOL = os.path.join(os.path.dirname(__file__), "wc-tool.py")


def run_wc(args):
    cmd = [PYTHON, WC_TOOL] + args
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=False,
        cwd=os.path.dirname(__file__),
    )
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def test_normal_file():
    path = os.path.join(os.path.dirname(__file__), "sample.txt")
    code, out, err = run_wc([path])
    assert code == 0
    assert err == ""
    assert out == "3\t8\t48"


def test_empty_file():
    fd, path = tempfile.mkstemp()
    os.close(fd)
    try:
        code, out, err = run_wc([path])
        assert code == 0
        assert err == ""
        assert out == "0\t0\t0"
    finally:
        os.remove(path)


def test_file_not_found():
    code, out, err = run_wc(["nonexistent.txt"])
    assert code != 0
    assert "not found" in err.lower() or "no such file" in err.lower()


def test_combined_flags():
    path = os.path.join(os.path.dirname(__file__), "sample.txt")
    code, out, err = run_wc(["-l", "-w", "-c", path])
    assert code == 0
    assert err == ""
    assert out == "3\t8\t48"

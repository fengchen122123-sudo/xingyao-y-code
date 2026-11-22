import sys


class _NullWriter:
    """吞掉所有写入/刷新调用的哑输出流，兼容 print()/flush()/isatty() 等常见用法。"""

    def write(self, *args, **kwargs):
        return 0

    def flush(self, *args, **kwargs):
        pass

    def isatty(self):
        return False

    def fileno(self):
        # 部分第三方库会在异常处理路径里探测 fileno()；未真正打开文件描述符时
        # 抛出 OSError 是标准库自身的行为（对齐 io.IOBase），调用方通常已有
        # try/except 兜底，这里保持一致即可。
        raise OSError("null stream has no fileno")


if sys.stdout is None:
    sys.stdout = _NullWriter()
if sys.stderr is None:
    sys.stderr = _NullWriter()

"""
packaging/runtime_hook_nullstdout.py
PyInstaller 运行时钩子，仅注册给 GUI 入口 (Y-code.exe, console=False) 的 Analysis 使用。

背景：
console=False（即传统 Windows GUI 子系统程序，等价于 pythonw.exe 语义）下，
Python 解释器初始化时 sys.stdout / sys.stderr 会直接是 None（没有可写的控制台
句柄）。项目里任何遗留的 print(...) 调用（包括第三方库启动期间自己打的日志），
一旦触发就会因为在 None 对象上调用 .write()/.flush() 抛出 AttributeError，
导致 GUI 静默崩溃退出——用户只会看到程序“打开又立刻消失”，且没有任何控制台
窗口能看到报错堆栈，非常难排查。

这个钩子会在用户代码（gui_main.py 等）被 import 之前运行，把 None 的
stdout/stderr 替换成一个写/刷新皆为空操作的哑对象，从根源上避免这类崩溃。
"""
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

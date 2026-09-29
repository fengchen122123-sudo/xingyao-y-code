"""GUI 内嵌交互式终端（底部面板）的常驻 shell 会话。

承载用户在终端面板里手动敲入的持久 shell（Windows 走 pywinpty/ConPTY，
POSIX 走标准库 pty），模型/工具链路不经过这里——模型执行命令仍走
execute_command 工具的权限确认流程。旧版右侧面板单命令终端
（run_terminal_command）已随该功能上线一并下线删除。

为什么用 pywinpty 而不是手写 ctypes ConPTY：实测 Windows 11 24H2/25H2 上
自实现的 CreatePseudoConsole + PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE 组合
（含官方文档与 EchoCon 两种写法）子进程均无法挂上伪终端（属性被静默忽略，
microsoft/terminal 也有同类 24H2 回归报告），而 pywinpty（Rust 封装）在同
环境验证可用，故 Windows 侧直接复用该库。

线程模型（会话最多 3 个守护线程）：
- 读线程阻塞读 PTY 输出，进批量缓冲（50ms / 8KB 双阈值），节拍线程负责
  静默期冲刷；事件回调里只调 emit_event（内部 put_nowait 进 _js_queue），
  绝不直接触碰 WebView（gui_bridge 的 JS 桥铁律）。
- 监视线程等子进程退出后取回退出码并触发收尾，保证「最终输出冲刷完才发
  退出事件」的顺序。
"""

from __future__ import annotations

import codecs
import logging
import os
import shutil
import signal
import subprocess
import threading
import time
from typing import Callable, Optional, Tuple

logger = logging.getLogger(__name__)

# 单次冲刷的批量阈值：过密的 evaluate_js 会拖垮 WebView，与流式 token
# 批量推送同一思路（gui_bridge 的 JS 事件派发队列）
_FLUSH_INTERVAL = 0.05
_FLUSH_BYTES = 8 * 1024

# 终端尺寸夹取范围：ConPTY 对 0/负值与超大列宽都会报错
MIN_COLS, MAX_COLS = 2, 500
MIN_ROWS, MAX_ROWS = 2, 300


class PtyTerminalError(Exception):
    """终端会话创建/操作失败（携带可直接展示给用户的中文消息）。"""


def clamp_terminal_size(cols: object, rows: object) -> Tuple[int, int]:
    """把前端传来的尺寸夹取到安全区间（非法输入按默认值兜底）。"""
    def _clamp(value: object, default: int, lo: int, hi: int) -> int:
        try:
            n = int(value)
        except (TypeError, ValueError):
            return default
        return max(lo, min(hi, n))

    return _clamp(cols, 80, MIN_COLS, MAX_COLS), _clamp(rows, 24, MIN_ROWS, MAX_ROWS)


def resolve_shell_command() -> Tuple[str, str]:
    """返回 (可执行文件, 参数列表)：优先系统自带 PowerShell。"""
    if os.name == "nt":
        for exe in ("powershell.exe", "pwsh.exe"):
            path = shutil.which(exe)
            if path:
                return path, [path, "-NoLogo"]
        raise PtyTerminalError("未找到 PowerShell（powershell.exe / pwsh.exe）")
    shell = os.environ.get("SHELL") or "/bin/bash"
    if not shutil.which(shell):
        raise PtyTerminalError(f"未找到 shell：{shell}")
    return shell, [shell]


class PtyTerminalSession:
    """一个常驻 shell 的伪终端会话；回调均在后台线程触发。

    on_output(text) 携带已解码文本（批量冲刷）；on_exit(code) 在最终输出
    冲刷之后触发一次。回调内部异常只记日志，不影响会话收尾。
    """

    def __init__(
        self,
        cwd: str,
        cols: int = 80,
        rows: int = 24,
        on_output: Optional[Callable[[str], None]] = None,
        on_exit: Optional[Callable[[Optional[int]], None]] = None,
    ):
        self.cwd = cwd
        self._cols, self._rows = clamp_terminal_size(cols, rows)
        self._on_output = on_output
        self._on_exit = on_exit

        # 全部实例属性先于线程启动初始化完毕（读线程/监视线程都依赖）
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._out_lock = threading.Lock()
        # on_output 派发顺序锁：reader 与 ticker 可能并发冲刷，锁内取残留并回调，
        # 保证两次 on_output 的调用次序与取出次序一致（前端拼接不会倒序）
        self._emit_lock = threading.Lock()
        self._pending: list[str] = []
        self._pending_bytes = 0
        self._pending_ts: Optional[float] = None
        self._close_lock = threading.Lock()
        self._closed = False
        self._exit_code: Optional[int] = None
        self._exit_event = threading.Event()
        # 读线程收尾完成标记：读线程不再产出新数据时 set()；
        # _finish 与它汇合并把残留冲刷完，之后才派发退出事件
        self._reader_done = threading.Event()

        self._proc = None  # pywinpty.PtyProcess（Windows）
        self._popen: Optional[subprocess.Popen] = None  # POSIX 路径使用
        self._proc_pid: Optional[int] = None
        self._master_fd: Optional[int] = None  # POSIX 路径使用

        if os.name == "nt":
            self._spawn_windows()
        else:
            self._spawn_posix()

        self._reader = threading.Thread(
            target=self._reader_loop, daemon=True, name="gui-pty-reader"
        )
        self._ticker = threading.Thread(
            target=self._ticker_loop, daemon=True, name="gui-pty-ticker"
        )
        self._monitor = threading.Thread(
            target=self._monitor_loop, daemon=True, name="gui-pty-monitor"
        )
        self._reader.start()
        self._ticker.start()
        self._monitor.start()

    # ------------------------------------------------------------------
    # 平台 spawn
    # ------------------------------------------------------------------
    def _spawn_windows(self) -> None:
        from winpty import PtyProcess

        _, argv = resolve_shell_command()
        try:
            self._proc = PtyProcess.spawn(
                argv, cwd=self.cwd, dimensions=(self._rows, self._cols)
            )
        except PtyTerminalError:
            raise
        except Exception as e:
            raise PtyTerminalError(f"启动 shell 失败: {e}")
        self._proc_pid = self._proc.pid

    def _spawn_posix(self) -> None:
        """GUI 面向 Windows，此路径仅供 POSIX 开发环境使用。"""
        import fcntl
        import pty
        import struct
        import termios

        executable, _ = resolve_shell_command()
        master, slave = pty.openpty()
        try:
            self._popen = subprocess.Popen(
                [executable],
                cwd=self.cwd,
                stdin=slave,
                stdout=slave,
                stderr=slave,
                start_new_session=True,
                close_fds=True,
            )
        except Exception as e:
            os.close(master)
            os.close(slave)
            raise PtyTerminalError(f"启动 shell 失败: {e}")
        os.close(slave)
        self._master_fd = master
        self._proc_pid = self._popen.pid
        try:
            fcntl.ioctl(
                master, termios.TIOCSWINSZ,
                struct.pack("HHHH", self._rows, self._cols, 0, 0),
            )
        except Exception as e:
            # 初始尺寸设置失败：清理已建好的 master fd 与子进程，避免泄漏
            # （此时三线程尚未启动，不会与收尾逻辑并发）
            self._master_fd = None
            try:
                os.close(master)
            except OSError:
                pass
            try:
                if self._popen.poll() is None:
                    self._popen.kill()
            except Exception:
                logger.debug("清理 POSIX 终端子进程失败（忽略）", exc_info=True)
            raise PtyTerminalError(f"启动 shell 失败: {e}")

    # ------------------------------------------------------------------
    # 对外操作
    # ------------------------------------------------------------------
    @property
    def pid(self) -> Optional[int]:
        return self._proc_pid

    def is_alive(self) -> bool:
        """会话是否仍可用：退出事件未置位，且子进程确实还活着。"""
        if self._exit_event.is_set():
            return False
        # 兜底：monitor 卡在 _proc.wait() 时退出事件迟迟不置位，
        # 这里直接探测进程存活状态，避免无限复用"假活"会话
        try:
            if os.name == "nt":
                if self._proc is not None and not self._proc.isalive():
                    return False
            else:
                if self._popen is not None and self._popen.poll() is not None:
                    return False
        except Exception:
            logger.debug("终端存活探测失败，回退退出事件判定", exc_info=True)
        return True

    def write(self, data: str) -> bool:
        """写入用户输入（xterm.js 的 onData 原样字符串，UTF-8 语义）。

        返回 bool：成功 True；会话已关闭或写入异常 False——调用方据此
        上报失败，避免 shell 已死时用户键入被静默丢弃。
        """
        if self._closed:
            return False
        if not data:
            return True  # 空输入无需写入，不算失败
        try:
            if os.name == "nt":
                self._proc.write(data)
            else:
                os.write(self._master_fd, data.encode("utf-8"))
            return True
        except Exception:
            logger.debug("终端输入写入失败（会话可能已退出）", exc_info=True)
            return False

    def resize(self, cols: int, rows: int) -> bool:
        """同步终端尺寸；返回 bool（成功 True，会话已关闭/设置失败 False）。"""
        cols, rows = clamp_terminal_size(cols, rows)
        self._cols, self._rows = cols, rows
        if self._closed:
            return False
        try:
            if os.name == "nt":
                self._proc.setwinsize(rows, cols)
            else:
                import fcntl
                import struct
                import termios

                fcntl.ioctl(
                    self._master_fd,
                    termios.TIOCSWINSZ,
                    struct.pack("HHHH", rows, cols, 0, 0),
                )
            return True
        except Exception:
            logger.debug("终端 resize 失败（会话可能已退出）", exc_info=True)
            return False

    def kill(self) -> None:
        """终止 shell 并释放资源（幂等，可从任意线程调用）。"""
        self._terminate_and_close()
        # 给读线程机会把退出事件发出去
        self._exit_event.wait(3.0)

    def _terminate_and_close(self) -> None:
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
        try:
            if os.name == "nt":
                if self._proc is not None:
                    self._proc.terminate(force=True)
            else:
                if self._popen is not None and self._popen.poll() is None:
                    try:
                        os.killpg(os.getpgid(self._popen.pid), signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        self._popen.kill()
                if self._master_fd is not None:
                    try:
                        os.close(self._master_fd)
                    except OSError:
                        pass
                    self._master_fd = None
        except Exception:
            logger.debug("终端资源释放异常（忽略）", exc_info=True)

    # ------------------------------------------------------------------
    # 读线程 / 批量冲刷 / 监视与收尾
    # ------------------------------------------------------------------
    def _reader_loop(self) -> None:
        try:
            if os.name == "nt":
                while True:
                    # pywinpty 阻塞读，返回 str；PTY 关闭时抛异常结束循环
                    text = self._proc.read(4096)
                    if not text:
                        continue
                    self._feed_text(text)
            else:
                while True:
                    try:
                        chunk = os.read(self._master_fd, 8192)
                    except OSError:
                        break
                    if not chunk:
                        break
                    self._feed_text(self._decoder.decode(chunk))
        except Exception:
            logger.debug("终端读线程结束", exc_info=True)
        finally:
            try:
                self._flush()
                try:
                    self._feed_text(self._decoder.decode(b"", final=True))
                    self._flush()
                except Exception:
                    pass
            finally:
                # 读线程到此不再产出新数据：先置位再等退出事件，
                # _finish 汇合该标记并把残留冲刷完，才发 pty_exit
                self._reader_done.set()
            # monitor 负责发退出事件；这里兜底等待，防止极端时序下先于退出码返回
            self._exit_event.wait(2.0)

    def _monitor_loop(self) -> None:
        """等子进程退出 → 取退出码 → 关流（读线程随之退出）→ 收尾。"""
        try:
            if os.name == "nt":
                self._proc.wait()
                try:
                    self._exit_code = int(self._proc.exitstatus)
                except Exception:
                    self._exit_code = 0
            else:
                self._exit_code = self._popen.wait()
        except Exception:
            logger.debug("终端退出监视线程异常", exc_info=True)
            if self._exit_code is None:
                self._exit_code = 0
        finally:
            self._terminate_and_close()
            self._finish()

    def _ticker_loop(self) -> None:
        """静默期冲刷：小批量输出不依赖下一个数据块到达。"""
        while not self._exit_event.wait(_FLUSH_INTERVAL):
            self._flush()

    def _feed_text(self, text: str) -> None:
        if not text:
            return
        with self._out_lock:
            self._pending.append(text)
            self._pending_bytes += len(text)
            if self._pending_ts is None:
                self._pending_ts = time.monotonic()
            should_flush = self._pending_bytes >= _FLUSH_BYTES or (
                time.monotonic() - self._pending_ts >= _FLUSH_INTERVAL
            )
        if should_flush:
            self._flush()

    def _flush(self) -> None:
        callback = self._on_output
        if callback is None:
            with self._out_lock:
                self._pending.clear()
                self._pending_bytes = 0
                self._pending_ts = None
            return
        # 顺序锁覆盖「取残留 + 派发」整段：reader 与 ticker 并发冲刷时，
        # 两次 on_output 的调用次序与取出次序一致，前端不会倒序拼接
        # （回调只做 emit_event，不会重入本会话，无死锁风险）
        with self._emit_lock:
            with self._out_lock:
                if not self._pending:
                    self._pending_ts = None
                    return
                text = "".join(self._pending)
                self._pending.clear()
                self._pending_bytes = 0
                self._pending_ts = None
            try:
                callback(text)
            except Exception:
                logger.debug("终端输出回调异常", exc_info=True)

    def _finish(self) -> None:
        """monitor 专属收尾：等读线程汇合 → 冲刷残留输出 → 发退出事件。"""
        # 有界等待读线程收尾：读线程可能仍阻塞在 PTY 读取上，
        # 超时兜底继续收尾，避免退出事件被无限期拖延
        self._reader_done.wait(1.0)
        self._flush()
        code = self._exit_code if self._exit_code is not None else 0
        self._exit_event.set()
        callback = self._on_exit
        if callback:
            try:
                callback(code)
            except Exception:
                logger.debug("终端退出回调异常", exc_info=True)

    def __del__(self):
        try:
            if getattr(self, "_proc_pid", None):
                self._terminate_and_close()
        except Exception:
            pass


def create_pty_terminal(
    cwd: str,
    cols: int = 80,
    rows: int = 24,
    on_output: Optional[Callable[[str], None]] = None,
    on_exit: Optional[Callable[[Optional[int]], None]] = None,
) -> PtyTerminalSession:
    """创建一个常驻 shell 会话；失败抛 PtyTerminalError。"""
    # 先校验 shell 可用性，避免半初始化状态
    resolve_shell_command()
    return PtyTerminalSession(
        cwd=cwd, cols=cols, rows=rows, on_output=on_output, on_exit=on_exit
    )

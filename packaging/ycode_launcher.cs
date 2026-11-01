// ---------------------------------------------------------------------------
// packaging/ycode_launcher.cs
// ycode.exe 控制台启动器（几 KB 的 .NET Framework 4.x 程序，csc 编译，见
// build_release.build_cli_launcher）。
//
// 背景：安装包里原先有两个 PyInstaller exe —— Y-code.exe（GUI, console=False）
// 与 ycode.exe（CLI, console=True），各自内嵌一份约 31MB 的 Python 字节码归档
// （PYZ）。实测两份 PYZ 里共用模块 5723 个、相同部分约 30.83MB，CLI 独有模块
// 仅 0.12MB。合并后只保留 GUI 入口 Y-code.exe，ycode.exe 退化为本启动器：
// 它把调用方参数原样透传并追加 --cli 拉起同目录的 Y-code.exe，由
// gui_main._dispatch_cli_argv 在任何 GUI 重模块 import 之前转交 CLI 主流程。
//
// 实现要点（与 gui_main.py 的 --cli 分支配套，改动其一必须同步另一侧）：
//   1. 参数不该重新拼接：用 GetCommandLineW 截取"自身之后"的原始命令行串直接
//      追加，保留调用方原有的引号语义（例如 -p "解释 a b" 的空格边界、空参数
//      ""、带引号的路径）。用 args 重新拼接会丢掉这些信息。
//   2. 必须继承标准输入/输出/错误（bInheritHandles=TRUE + STARTF_USESTDHANDLES），
//      CLI 的交互界面与管道输出才能落到调用方的控制台/管道。
//   3. 等待子进程退出并原样返回其退出码（自动化脚本据 ycode -p 的退出码判成败）。
//   4. 绝不设置 CREATE_NO_WINDOW：CLI 需要与调用方共享同一个控制台窗口。
// ---------------------------------------------------------------------------
using System;
using System.IO;
using System.Reflection;
using System.Runtime.InteropServices;

namespace YCodeLauncher
{
    internal static class Program
    {
        private const int StdInputHandle = -10;
        private const int StdOutputHandle = -11;
        private const int StdErrorHandle = -12;
        private const int StartfUseStdHandles = 0x00000100;
        private const uint Infinite = 0xFFFFFFFF;

        private const string TargetExeName = "Y-code.exe";
        private const string CliFlag = "--cli";

        [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
        private struct STARTUPINFO
        {
            public int cb;
            public string lpReserved;
            public string lpDesktop;
            public string lpTitle;
            public int dwX;
            public int dwY;
            public int dwXSize;
            public int dwYSize;
            public int dwXCountChars;
            public int dwYCountChars;
            public int dwFillAttribute;
            public int dwFlags;
            public short wShowWindow;
            public short cbReserved2;
            public IntPtr lpReserved2;
            public IntPtr hStdInput;
            public IntPtr hStdOutput;
            public IntPtr hStdError;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct PROCESS_INFORMATION
        {
            public IntPtr hProcess;
            public IntPtr hThread;
            public int dwProcessId;
            public int dwThreadId;
        }

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern bool CreateProcessW(
            string lpApplicationName,
            string lpCommandLine,
            IntPtr lpProcessAttributes,
            IntPtr lpThreadAttributes,
            bool bInheritHandles,
            int dwCreationFlags,
            IntPtr lpEnvironment,
            string lpCurrentDirectory,
            ref STARTUPINFO lpStartupInfo,
            out PROCESS_INFORMATION lpProcessInformation);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern uint WaitForSingleObject(IntPtr hHandle, uint dwMilliseconds);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool GetExitCodeProcess(IntPtr hProcess, out uint lpExitCode);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool CloseHandle(IntPtr hObject);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern IntPtr GetStdHandle(int nStdHandle);

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern IntPtr GetCommandLineW();

        /// <summary>句柄是否可用（NULL 与 INVALID_HANDLE_VALUE 都不可用）。</summary>
        private static bool IsUsableHandle(IntPtr handle)
        {
            return handle != IntPtr.Zero && handle != new IntPtr(-1);
        }

        /// <summary>取完整命令行（GetCommandLineW 返回的是不含 argv 拆分的原始串）。</summary>
        private static string GetCommandLineText()
        {
            IntPtr ptr = GetCommandLineW();
            if (ptr == IntPtr.Zero)
            {
                return string.Empty;
            }
            return Marshal.PtrToStringUni(ptr) ?? string.Empty;
        }

        /// <summary>
        /// 截出"本程序自身"之后的原始参数串（返回值保留原有的前导空格与引号）。
        /// 优先按可执行文件绝对路径精确截断（路径含空格且调用方未加引号时也正确），
        /// 否则按 Windows 命令行解析规则跳过 argv[0]：前导空白 → 引号串或首个空白前。
        /// </summary>
        private static string ExtractTrailingArguments(string commandLine, string selfPath)
        {
            if (string.IsNullOrEmpty(commandLine))
            {
                return string.Empty;
            }
            if (!string.IsNullOrEmpty(selfPath) && commandLine.Length > selfPath.Length)
            {
                if (string.Compare(commandLine, 0, selfPath, 0, selfPath.Length, true) == 0)
                {
                    return commandLine.Substring(selfPath.Length);
                }
            }
            int i = 0;
            while (i < commandLine.Length && (commandLine[i] == ' ' || commandLine[i] == '\t'))
            {
                i++;
            }
            if (i < commandLine.Length && commandLine[i] == '"')
            {
                i++;
                while (i < commandLine.Length && commandLine[i] != '"')
                {
                    i++;
                }
                if (i < commandLine.Length)
                {
                    i++;
                }
            }
            else
            {
                while (i < commandLine.Length && commandLine[i] != ' ' && commandLine[i] != '\t')
                {
                    i++;
                }
            }
            return commandLine.Substring(i);
        }

        private static int Main()
        {
            string selfPath = null;
            try
            {
                selfPath = Assembly.GetEntryAssembly().Location;
            }
            catch (Exception)
            {
                // 极端情况下（Assembly.Location 不可用）退回 AppDomain 基目录
                selfPath = null;
            }
            string baseDir = string.IsNullOrEmpty(selfPath)
                ? AppDomain.CurrentDomain.BaseDirectory
                : Path.GetDirectoryName(selfPath);
            if (string.IsNullOrEmpty(baseDir))
            {
                baseDir = AppDomain.CurrentDomain.BaseDirectory;
            }
            string target;
            try
            {
                baseDir = Path.GetFullPath(baseDir);
                target = Path.Combine(baseDir, TargetExeName);
            }
            catch (Exception)
            {
                baseDir = AppDomain.CurrentDomain.BaseDirectory;
                target = Path.Combine(baseDir, TargetExeName);
            }

            if (!File.Exists(target))
            {
                Console.Error.WriteLine("错误：未找到 " + TargetExeName + "，无法进入 CLI 模式。");
                Console.Error.WriteLine("  启动器目录：" + baseDir);
                Console.Error.WriteLine("  期望目标：" + target);
                Console.Error.WriteLine("  请重新安装 星瑶Code（CLI 与 GUI 共用同一份程序，两者必须在同一目录）。");
                return 2;
            }

            // 目标路径加引号；--cli 后直接接调用方原始参数串（其自带前导空格与引号）
            string commandLine = "\"" + target + "\" " + CliFlag
                + ExtractTrailingArguments(GetCommandLineText(), selfPath);

            STARTUPINFO startupInfo = new STARTUPINFO();
            startupInfo.cb = Marshal.SizeOf(typeof(STARTUPINFO));
            IntPtr hIn = GetStdHandle(StdInputHandle);
            IntPtr hOut = GetStdHandle(StdOutputHandle);
            IntPtr hErr = GetStdHandle(StdErrorHandle);
            if (IsUsableHandle(hIn) || IsUsableHandle(hOut) || IsUsableHandle(hErr))
            {
                startupInfo.dwFlags = StartfUseStdHandles;
                startupInfo.hStdInput = IsUsableHandle(hIn) ? hIn : IntPtr.Zero;
                startupInfo.hStdOutput = IsUsableHandle(hOut) ? hOut : IntPtr.Zero;
                startupInfo.hStdError = IsUsableHandle(hErr) ? hErr : IntPtr.Zero;
            }

            PROCESS_INFORMATION processInfo;
            // lpCurrentDirectory 传 null：子进程继承调用方的当前工作目录（CLI 的工作区
            // 语义就是"用户当时所在目录"）；dwCreationFlags=0 不隐藏共享控制台。
            bool ok = CreateProcessW(
                target,
                commandLine,
                IntPtr.Zero,
                IntPtr.Zero,
                true,
                0,
                IntPtr.Zero,
                null,
                ref startupInfo,
                out processInfo);
            if (!ok)
            {
                int error = Marshal.GetLastWin32Error();
                Console.Error.WriteLine("错误：启动 " + TargetExeName + " 失败（Win32 错误码 " + error + "）。");
                Console.Error.WriteLine("  目标：" + target);
                return 3;
            }

            try
            {
                WaitForSingleObject(processInfo.hProcess, Infinite);
                uint exitCode;
                if (!GetExitCodeProcess(processInfo.hProcess, out exitCode))
                {
                    int error = Marshal.GetLastWin32Error();
                    Console.Error.WriteLine("错误：读取 " + TargetExeName + " 退出码失败（Win32 错误码 " + error + "）。");
                    return 4;
                }
                return unchecked((int)exitCode);
            }
            finally
            {
                if (IsUsableHandle(processInfo.hThread))
                {
                    CloseHandle(processInfo.hThread);
                }
                if (IsUsableHandle(processInfo.hProcess))
                {
                    CloseHandle(processInfo.hProcess);
                }
            }
        }
    }
}

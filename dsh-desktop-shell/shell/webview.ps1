# dsh-desktop-shell — native WebView2 window hosting the DeepSeek Harness Web GUI.
#
# Spawned by the dsh-desktop-shell plugin as:
#   powershell.exe -NoProfile -STA -ExecutionPolicy Bypass -File webview.ps1
#     -Url http://127.0.0.1:<port> -Title ... -Width ... -Height ... [-LogFile <path>]
#
# Requirements (all present on a stock Windows 10/11):
#   * Windows PowerShell 5.1 (powershell.exe) with .NET Framework 4.8
#   * The WebView2 Runtime (evergreen, ships with Windows updates)
#   * The vendored managed WebView2 assemblies + WebView2Loader.dll in ./lib
#
# The script lives exactly as long as the window: closing the window ends the
# script, which ends the powershell process, which the plugin observes and
# (by default) turns into a graceful dsh shutdown.

param(
    [string]$Url = "http://127.0.0.1:3080",
    [string]$Title = "DeepSeek Harness",
    [int]$Width = 1320,
    [int]$Height = 860,
    [string]$LogFile = ""
)

$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$libDir = Join-Path $scriptDir "lib"

function Write-Log {
    param([string]$Message)
    if ($LogFile -ne "") {
        try {
            Add-Content -Path $LogFile -Encoding UTF8 -Value ("{0} [webview.ps1] {1}" -f (Get-Date -Format o), $Message)
        } catch {
            # logging must never break the window
        }
    }
}

try {
    Write-Log ("start: url=" + $Url + " title=" + $Title + " scriptDir=" + $scriptDir)

    # 1. Preload the native WebView2 loader so the managed assemblies can find
    #    it (they DllImport "WebView2Loader.dll", which is not in the system
    #    search path). Preloading makes resolution deterministic.
    Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
namespace DesktopShell {
    public static class NativeLoader {
        [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
        public static extern IntPtr LoadLibrary(string lpFileName);
    }
}
"@
    $loaderPath = Join-Path $libDir "WebView2Loader.dll"
    if (-not (Test-Path $loaderPath)) {
        throw "dsh-desktop-shell: WebView2Loader.dll missing at $loaderPath"
    }
    $handle = [DesktopShell.NativeLoader]::LoadLibrary($loaderPath)
    if ($handle -eq [IntPtr]::Zero) {
        throw "dsh-desktop-shell: failed to load WebView2Loader.dll (Win32 error $([System.Runtime.InteropServices.Marshal]::GetLastWin32Error()))"
    }
    Write-Log "loader preloaded"

    # 2. Load WPF, then the managed WebView2 assemblies (net462 flavor, run on
    #    .NET Framework 4.8 under Windows PowerShell 5.1).
    Add-Type -AssemblyName PresentationFramework, PresentationCore, WindowsBase
    Add-Type -AssemblyName System.Drawing
    Add-Type -Path (Join-Path $libDir "Microsoft.Web.WebView2.Core.dll")
    Add-Type -Path (Join-Path $libDir "Microsoft.Web.WebView2.Wpf.dll")
    Write-Log "managed WebView2 assemblies loaded"

    # 3. Build the window.
    $window = New-Object System.Windows.Window
    $window.Title = $Title
    $window.Width = $Width
    $window.Height = $Height
    $window.WindowStartupLocation = [System.Windows.WindowStartupLocation]::CenterScreen
    $window.Background = [System.Windows.Media.Brushes]::White

    $webView = New-Object Microsoft.Web.WebView2.Wpf.WebView2
    $creation = New-Object Microsoft.Web.WebView2.Wpf.CoreWebView2CreationProperties
    $creation.UserDataFolder = Join-Path $env:LOCALAPPDATA "dsh-desktop-shell\webview2"
    $webView.CreationProperties = $creation
    $webView.DefaultBackgroundColor = [System.Drawing.Color]::White
    $window.Content = $webView

    # Ctrl+W closes the window (desktop-app convention). Closing ends the script.
    $window.Add_KeyDown({
        param($sender, $e)
        $ctrl = [System.Windows.Input.ModifierKeys]::Control
        if ($e.Key -eq [System.Windows.Input.Key]::W -and (([int]$e.KeyboardDevice.Modifiers) -band [int]$ctrl)) {
            $sender.Close()
        }
    })

    # Dark title bar (DWM immersive dark mode), matching the ycode GUI's dark
    # titlebar behavior. Set once the window's native handle exists.
    Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
namespace DesktopShell {
    public static class Dwm {
        [DllImport("dwmapi.dll", PreserveSig = true)]
        public static extern int DwmSetWindowAttribute(IntPtr hwnd, int attr, ref int value, int size);
    }
}
"@
    $window.Add_SourceInitialized({
        param($sender, $e)
        try {
            $h = (New-Object System.Windows.Interop.WindowInteropHelper($sender)).Handle
            $v = 1
            $r = [DesktopShell.Dwm]::DwmSetWindowAttribute($h, 20, [ref]$v, 4)
            Write-Log ("titlebar dark set, dwm result=" + $r)
        } catch {
            Write-Log ("titlebar dark failed: " + $_.Exception.Message)
        }
    })

    # If the WebView2 engine cannot initialize, close the window (which tells
    # the host to shut down) instead of leaving a blank window hanging.
    $webView.Add_CoreWebView2InitializationCompleted({
        param($sender, $e)
        if ($e.InitException) {
            $msg = "WebView2 init failed: " + $e.InitException.Message
            Write-Log $msg
            [Console]::Error.WriteLine("dsh-desktop-shell: " + $msg)
            $window.Dispatcher.Invoke([Action] { $window.Close() })
        } else {
            Write-Log "WebView2 initialized"
        }
    })

    # Deferred navigation: the control initializes itself on first render, then
    # loads $Url. No explicit EnsureCoreWebView2Async needed (and none safe to
    # block on from this thread).
    $webView.Source = $Url
    Write-Log "navigating to $Url"

    $window.ShowDialog() | Out-Null
    Write-Log "window closed, script ending"
} catch {
    $msg = "fatal: " + $_.Exception.ToString()
    Write-Log $msg
    [Console]::Error.WriteLine("dsh-desktop-shell: " + $msg)
    exit 1
}

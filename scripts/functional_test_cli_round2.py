# -*- coding: utf-8 -*-
"""CLI 功能测试 · 第二轮：覆盖第一批脚本（functional_test_cli_parity.py）未触及的 20 个命令。

与第一批相同的隔离手段：
- YCODE_DATA_DIR 指向临时目录（config.json / ycode.db / permissions.json / 记忆文件 /
  /share 导出文件全部隔离）；
- print_formatted_text / schedule_ui 打桩（后台线程输出同步捕获）；
- 全程不携带真实 API key：/dream、/compress 走"密钥未配置/对话过少"的优雅失败路径，
  headless 冒烟验证无 key 时退出码 1（0=成功/1=失败/2=配置错误契约）；
  /update 已由第一批脚本覆盖（真实网络），本脚本不再重复。

运行：venv/Scripts/python scripts/functional_test_cli_round2.py
"""

import os
import sys
import time
import tempfile
import shutil
import subprocess
import pathlib

sys.stdout.reconfigure(encoding="utf-8")

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = tempfile.mkdtemp(prefix="ycode_func_r2_")
os.environ["YCODE_DATA_DIR"] = DATA_DIR
sys.path.insert(0, REPO)

print(f"[隔离] YCODE_DATA_DIR = {DATA_DIR}")

import main  # noqa: E402  导入即播种隔离配置
import config  # noqa: E402
from core.model_manager import load_config  # noqa: E402
import queue as queue_mod  # noqa: E402

RESULTS = []
KNOWN_ISSUES = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond)))
    mark = "PASS" if cond else "FAIL"
    tail = f"  | {str(detail)[:110]}" if detail else ""
    print(f"[{mark}] {name}{tail}")


printed = []
main.print_formatted_text = lambda *a, **k: printed.append(str(a[0]))
main.app_instance = None
main.current_session = "功能测试会话二"
main.schedule_ui = lambda cb: cb()  # /dream /compress 后台线程输出同步化


def out():
    return "\n".join(printed)


def clear_out():
    printed.clear()


def safe_call(cmd_text):
    """执行命令，崩溃视为 FAIL 证据。"""
    clear_out()
    try:
        main.handle_slash_command(cmd_text)
        return None
    except Exception as exc:  # noqa: BLE001
        import traceback
        return f"{type(exc).__name__}: {exc}\n{traceback.format_exc()[-200:]}"


def wait_output(marker, timeout=30):
    """后台线程（/dream /compress）经同步 schedule_ui 输出，轮询等待标记出现。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if marker in out():
            return True
        time.sleep(0.1)
    return False


def _listed_exports():
    # /share 默认导出目录 = config.YCODE_DIR/exports（遵守 YCODE_DATA_DIR）
    exp_dir = pathlib.Path(DATA_DIR) / "exports"
    if not exp_dir.is_dir():
        return []
    return [str(p) for p in exp_dir.iterdir() if p.is_file()]


# ---------------------------------------------------------------------------
# T1 /theme（真实 config 持久化）
# ---------------------------------------------------------------------------
from core.themes import THEMES  # noqa: E402

_orig_theme = load_config().get("theme", "default")

safe_call("/theme list")
check("T1.1 /theme list 展示全部主题", all(t in out() for t in list(THEMES.keys())[:3]))

_target = [t for t in THEMES if t != _orig_theme][0]
safe_call(f"/theme {_target}")
check("T1.2 切换主题并持久化", f"已切换配色主题为" in out() and load_config().get("theme") == _target)

safe_call("/theme 不存在_主题xyz")
check("T1.3 非法主题报错", "未知主题配色" in out() and "可选主题" in out())

safe_call(f"/theme {_orig_theme}")
check("T1.4 主题还原", load_config().get("theme") == _orig_theme)

# ---------------------------------------------------------------------------
# T2 /permission（与 GUI set_permission_mode 同一套别名归一）
# ---------------------------------------------------------------------------
_orig_perm = load_config().get("permission_mode", "confirm")

safe_call("/permission")
check("T2.1 无参展示模式列表", "confirm" in out() and "用法" in out())

safe_call("/permission accept_edits")
check("T2.2 切换自动编辑模式", "自动编辑" in out() and load_config().get("permission_mode") == "accept_edits")

safe_call("/permission 错误模式xyz")
check("T2.3 非法模式报错", "未知权限模式" in out())

safe_call(f"/permission {_orig_perm}")
check("T2.4 权限模式还原", load_config().get("permission_mode") == _orig_perm)

# ---------------------------------------------------------------------------
# T3 /turns（真实 turn_limits 校验链）
# ---------------------------------------------------------------------------
from core.harness.turn_limits import load_turn_step_limit, MIN_TURN_STEP_LIMIT, MAX_TURN_STEP_LIMIT  # noqa: E402

_orig_turns = load_turn_step_limit()
safe_call("/turns")
check("T3.1 无参显示当前轮次", str(_orig_turns) in out() and f"{MIN_TURN_STEP_LIMIT}" in out())

_mid = (MIN_TURN_STEP_LIMIT + MAX_TURN_STEP_LIMIT) // 2
safe_call(f"/turns {_mid}")
check("T3.2 设置合法轮次", load_turn_step_limit() == _mid and "已更新" in out())

safe_call("/turns 不是数字")
check("T3.3 非整数报错", "必须是整数" in out())

safe_call(f"/turns {MAX_TURN_STEP_LIMIT + 1000}")
check("T3.4 超上限报错", "❌" in out() and load_turn_step_limit() == _mid)

safe_call(f"/turns {_orig_turns}")
check("T3.5 轮次还原", load_turn_step_limit() == _orig_turns)

# ---------------------------------------------------------------------------
# T4 /sandbox（真实 config 键 allow_outside_workspace）
# ---------------------------------------------------------------------------
_orig_sandbox = bool(load_config().get("allow_outside_workspace", False))

safe_call("/sandbox")
check("T4.1 状态展示", "沙盒状态" in out() and "用法" in out())

safe_call("/sandbox on")
check("T4.2 on 解除限制", "已解除" in out() and load_config().get("allow_outside_workspace") is True)

safe_call("/sandbox off")
check("T4.3 off 恢复限制", "已开启" in out() and load_config().get("allow_outside_workspace") is False)

safe_call("/sandbox 错误参数")
check("T4.4 非法参数报错", "未知参数" in out())

if _orig_sandbox:
    safe_call("/sandbox on")

# ---------------------------------------------------------------------------
# T5 /whitelist（真实 permission_store 持久化，落在隔离目录）
# ---------------------------------------------------------------------------
safe_call("/whitelist")
check("T5.1 list 展示双区白名单", "任务白名单" in out() and "持久化白名单" in out())

safe_call("/whitelist add command git status")
check("T5.2 add 新增命令规则", "已新增" in out())

safe_call("/whitelist add command git status")
check("T5.3 重复 add 提示已存在", "已存在" in out())

safe_call("/whitelist")
check("T5.4 list 显示新规则", "git status" in out())

safe_call("/whitelist remove command git status")
check("T5.5 remove 删除规则", "已删除" in out())

safe_call("/whitelist remove command git status")
check("T5.6 重复 remove 提示未找到", "未找到" in out())

safe_call("/whitelist add 写错类型 xyz")
check("T5.7 非法规则类型报错", "❌" in out())

safe_call("/whitelist clear")
check("T5.8 clear 未带确认不动手", "confirm" in out())

safe_call("/whitelist clear confirm")
check("T5.9 clear confirm 清空", "已清空" in out())

safe_call("/whitelist 胡乱动作")
check("T5.10 未知动作报用法", "用法" in out())

# ---------------------------------------------------------------------------
# T6 /undo 与 /checkpoint（TurnUndoService 真实链路，空会话路径）
# ---------------------------------------------------------------------------
err = safe_call("/undo list")
check("T6.1 /undo list 空会话优雅提示", err is None and "没有按回合撤销记录" in out(), err or "")

err = safe_call("/checkpoint list")
check("T6.2 /checkpoint list 空会话优雅提示", err is None and "没有检查点记录" in out(), err or "")

err = safe_call("/undo 00000000")
check("T6.3 /undo 不存在的回合不崩溃", err is None and out().strip() != "", err or "(无输出)")

# ---------------------------------------------------------------------------
# T7 /diagram（真实 diagram_export 扫描）
# ---------------------------------------------------------------------------
err = safe_call("/diagram list")
check("T7 /diagram list 空记录", err is None and ("还没有生成过流程图" in out() or "暂无" in out()), err or out()[:60])

# ---------------------------------------------------------------------------
# T8 /worktree（真实 git worktree list，只读）
# ---------------------------------------------------------------------------
err = safe_call("/worktree list")
check("T8.1 /worktree list 列出真实仓库", err is None and "worktree" in out().lower() and REPO.lower()[:3] in out().lower()[:200], err or "")

err = safe_call("/worktree 胡乱子命令")
check("T8.2 未知子命令报用法", "未知子命令" in out())

# ---------------------------------------------------------------------------
# T9 /mcp（隔离目录无 mcp.json → 空态路径；不触发真实连接）
# ---------------------------------------------------------------------------
err = safe_call("/mcp status")
check("T9.1 /mcp status 空配置提示", err is None and "未配置任何 MCP 服务" in out(), err or out()[:60])

err = safe_call("/mcp tools")
check("T9.2 /mcp tools 空工具提示", err is None and "没有可用的 MCP 工具" in out(), err or "")

err = safe_call("/mcp enable")
check("T9.3 enable 缺名报用法", "用法" in out())

err = safe_call("/mcp enable 不存在服务xyz")
check("T9.4 enable 不存在服务报错", "⚠" in out())

err = safe_call("/mcp 胡乱子命令")
check("T9.5 未知子命令报用法", "未知子命令" in out())

# ---------------------------------------------------------------------------
# T10 /plugins（真实内置插件扫描）
# ---------------------------------------------------------------------------
from core import plugin_loader  # noqa: E402

err = safe_call("/plugins")
_infos = plugin_loader.get_plugin_infos()
check("T10.1 /plugins 展示内置插件清单", err is None and (str(len(_infos)) in out() or "已发现" in out()) and ("hunyuan_image" in out() or not _infos), err or out()[:60])

err = safe_call("/plugins enable 不存在插件xyz")
check("T10.2 enable 不存在插件报错", "⚠" in out())

err = safe_call("/plugins 胡乱动作")
check("T10.3 未知子命令报用法", "未知子命令" in out())

err = safe_call("/plugins reload")
check("T10.4 reload 刷新清单", err is None and "插件清单已刷新" in out(), err or "")

# ---------------------------------------------------------------------------
# T11 /usage（隔离空 DB → 空态 + 非法日期）
# ---------------------------------------------------------------------------
err = safe_call("/usage")
check("T11.1 空库提示暂无用量", err is None and "暂无 token 用量记录" in out(), err or out()[:60])

err = safe_call("/usage 不是日期")
check("T11.2 非法日期报用法", "用法" in out())

# ---------------------------------------------------------------------------
# T12 /clear（logo 重绘，不崩溃）
# ---------------------------------------------------------------------------
err = safe_call("/clear")
check("T12 /clear 重绘 logo", err is None and len(out().strip()) > 0, err or "")

# ---------------------------------------------------------------------------
# T13 /memory 与 /remember（真实 memory_kairos 文件链，隔离目录）
# ---------------------------------------------------------------------------
err = safe_call("/memory")
check("T13.1 空记忆提示", err is None and "没有任何长期记忆索引" in out(), err or "")

err = safe_call("/remember")
check("T13.2 /remember 缺参报用法", "用法错误" in out())

err = safe_call("/remember 功能测试：CLI 第二轮白名单走查结论")
from core.memory_kairos import daily_log_path  # noqa: E402
_log_ok = os.path.isfile(str(daily_log_path()))
check("T13.3 /remember 写入今日日志", err is None and "成功记录" in out() and _log_ok, err or f"log_exists={_log_ok}")

# ---------------------------------------------------------------------------
# T14 /dream（无 key → 优雅失败文案，后台线程输出经 schedule_ui 同步捕获）
# ---------------------------------------------------------------------------
err = safe_call("/dream")
_dream_ok = err is None and wait_output("密钥尚未配置", timeout=20)
check("T14 /dream 无密钥优雅提示", _dream_ok, err or out()[:80])

# ---------------------------------------------------------------------------
# T15 /compress（空会话：force 与非 force 均应有"对话过少"提示，不再静默）
# ---------------------------------------------------------------------------
err = safe_call("/compress force")
_c1 = err is None and wait_output("对话过少", timeout=20)
check("T15.1 force 空会话提示对话过少", _c1, err or out()[:80])

err = safe_call("/compress")
_c2 = err is None and wait_output("对话过少", timeout=20)
check("T15.2 非 force 空会话同样提示对话过少", _c2, err or out()[:80])

# ---------------------------------------------------------------------------
# T16 /new（真实会话创建与切换）
# ---------------------------------------------------------------------------
_prev_session = main.current_session
err = safe_call("/new")
_new_session = main.current_session
check("T16 /new 创建并切换会话", err is None and "已创建并切换到全新会话" in out()
      and _new_session != _prev_session and _new_session.startswith("session_")
      and main.manager.ensure_session(_new_session) is not None, err or "")

# ---------------------------------------------------------------------------
# T17 /share（真实导出链：md/html/json 三格式落盘；Path.home 已打桩隔离）
# ---------------------------------------------------------------------------
main.manager.add_messages(main.current_session, [("user", "导出测试问题"), ("assistant", "导出测试回答")])
for fmt, ext in (("md", ".md"), ("html", ".html"), ("json", ".json")):
    err = safe_call(f"/share {fmt}")
    _found = any(f.endswith(ext) and DATA_DIR in f for f in _listed_exports())
    check(f"T17.{ {'md':1,'html':2,'json':3}[fmt] } /share {fmt} 导出文件落盘", err is None and _found, err or "")
safe_call("/share pdf")
check("T17.4 /share 非法格式报错", "未知格式" in out())

# ---------------------------------------------------------------------------
# T18 /exit
# ---------------------------------------------------------------------------
err = safe_call("/exit")
check("T18 /exit 告别语", err is None and "去休息啦" in out(), err or "")

# ---------------------------------------------------------------------------
# T19-T22 进程级 headless 冒烟（真实子进程，独立隔离目录）
# ---------------------------------------------------------------------------
def run_headless_proc(args, stdin_text=None, timeout=180):
    env = dict(os.environ)
    env["YCODE_DATA_DIR"] = tempfile.mkdtemp(prefix="ycode_func_headless_")
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(
        [sys.executable, os.path.join(REPO, "main.py"), *args],
        input=stdin_text, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=timeout, env=env,
    )
    return proc

try:
    proc = run_headless_proc(["-h"], timeout=60)
    check("T19 -h 帮助退出码 0", proc.returncode == 0 and "-p" in proc.stdout and "headless" in proc.stdout.lower(),
          f"rc={proc.returncode}")

    proc = run_headless_proc(["-p", "-"], stdin_text="")
    check("T20 空stdin退出码2+stderr提示", proc.returncode == 2 and "为空" in (proc.stderr or "") + proc.stdout,
          f"rc={proc.returncode}")

    # 退出码契约（run_headless 文档）：0=成功 / 1=任务失败 / 2=配置错误。
    # 无 key 时 LLM 请求失败 → turn_error 事件 → headless 收尾排空事件队列
    # 检出失败 → 退出码 1 + stderr 失败说明。
    proc = run_headless_proc(["-p", "功能冒烟：请原样回复收到"], stdin_text=None)
    _all_out = (proc.stdout or "") + (proc.stderr or "")
    check("T21 无key一次性任务失败退出码1+stderr说明",
          proc.returncode == 1 and "任务失败" in (proc.stderr or "") and "Traceback" not in _all_out,
          f"rc={proc.returncode} err={((proc.stderr or '').strip()[-90:])!r}")

    proc = run_headless_proc(["-p", "-", "--session", "冒烟会话"], stdin_text="功能冒烟 stdin 内容")
    _all_out2 = (proc.stdout or "") + (proc.stderr or "")
    check("T22 stdin模式+--session 失败退出码1",
          proc.returncode == 1 and "任务失败" in (proc.stderr or "") and "Traceback" not in _all_out2,
          f"rc={proc.returncode} err={((proc.stderr or '').strip()[-90:])!r}")
except subprocess.TimeoutExpired as exc:
    check("T19-T22 headless 进程冒烟（超时——疑似无 key 下挂起）", False, str(exc)[:100])

# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------
passed = sum(1 for _, ok in RESULTS if ok)
failed = len(RESULTS) - passed
print("\n" + "=" * 60)
print(f"第二轮功能测试汇总: {passed} 通过 / {failed} 失败（共 {len(RESULTS)} 项）")
if KNOWN_ISSUES:
    print("已知缺陷跟踪（不计入失败）:")
    for item in KNOWN_ISSUES:
        print(f"  - {item}")
if failed:
    print("失败项:")
    for name, ok in RESULTS:
        if not ok:
            print(f"  - {name}")
print("=" * 60)

shutil.rmtree(DATA_DIR, ignore_errors=True)
sys.exit(1 if failed else 0)

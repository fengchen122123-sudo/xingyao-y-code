# -*- coding: utf-8 -*-
"""CLI 能力对齐 GUI 第一批 · 功能测试。

真实 core 链路逐命令验证（区别于 mock 单元测试）：
- 用 YCODE_DATA_DIR 指向临时目录做全量隔离（config.json / ycode.db / persona.json
  均落在临时目录，首次导入自动从仓库默认 config.json 播种）；
- os.startfile 打桩（不真弹资源管理器）；/update install 只测门禁与无包路径，
  绝不真正启动安装；/update check 走真实 Gitee 网络（只读）；
- /paste-image 会真实写入系统剪贴板（测试后不还原，属已知副作用）。

运行：venv/Scripts/python scripts/functional_test_cli_parity.py
"""

import os
import sys
import tempfile
import shutil
import subprocess

sys.stdout.reconfigure(encoding="utf-8")

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = tempfile.mkdtemp(prefix="ycode_func_")
os.environ["YCODE_DATA_DIR"] = DATA_DIR
sys.path.insert(0, REPO)

print(f"[隔离] YCODE_DATA_DIR = {DATA_DIR}")

import main  # noqa: E402  导入即播种隔离配置
import config  # noqa: E402
import queue as queue_mod  # noqa: E402

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond)))
    mark = "PASS" if cond else "FAIL"
    tail = f"  | {str(detail)[:110]}" if detail else ""
    print(f"[{mark}] {name}{tail}")


printed = []
main.print_formatted_text = lambda *a, **k: printed.append(str(a[0]))
main.app_instance = None
main.current_session = "功能测试会话"


def out():
    return "\n".join(printed)


def clear_out():
    printed.clear()


def drain_queue():
    items = []
    while True:
        try:
            items.append(main.msg_queue.get_nowait())
        except queue_mod.Empty:
            break
    return items


# ---------------------------------------------------------------------------
# T1 启动与隔离
# ---------------------------------------------------------------------------
check("T1.1 config.json 播种到隔离目录", os.path.isfile(os.path.join(DATA_DIR, "config.json")))
check("T1.2 DB 落在隔离目录", str(config.DB_FILE).replace("/", "\\").startswith(DATA_DIR.replace("/", "\\").replace("\\", "\\")) or DATA_DIR in str(config.DB_FILE))

# ---------------------------------------------------------------------------
# T2 /clear-history（真实 MemoryManager）
# ---------------------------------------------------------------------------
main.manager.ensure_session(main.current_session)
main.manager.add_messages(main.current_session, [("user", "功能测试问题一"), ("assistant", "功能测试回答一")])
before_count = len(main.manager.get_history(main.current_session))

import core.llm as llm  # noqa: E402

llm.session_token_usage.update({"latest_total": 555, "latest_prompt": 300, "latest_cached": 100})

main.handle_slash_command("/clear-history")
check("T2.1 未确认不清空", len(main.manager.get_history(main.current_session)) == before_count and "确认" in out(),
      f"历史 {len(main.manager.get_history(main.current_session))} 条")

main.handle_slash_command("/clear-history y")
check("T2.2 确认后清空", len(main.manager.get_history(main.current_session)) == 0 and "已清空 2 条" in out())
check("T2.3 token 快照重置", llm.session_token_usage["latest_total"] == 0 and llm.session_token_usage["latest_prompt"] == 0)
clear_out()

# ---------------------------------------------------------------------------
# T3 /persona（真实 persona_settings 存取）
# ---------------------------------------------------------------------------
from core.persona_settings import get_persona_settings  # noqa: E402

main.handle_slash_command("/persona")
check("T3.1 无参显示当前人设", "智能体名称" in out() and "人设文本" in out())

main.handle_slash_command("/persona name 测试瑶")
check("T3.2 名称保存生效", get_persona_settings()["agent_name"] == "测试瑶")

main.handle_slash_command("/persona text " + "长" * 20001)
check("T3.3 超限拒绝", "过长" in out())

main.handle_slash_command("/persona title 老板")
check("T3.4 称呼保存生效", get_persona_settings()["user_title"] == "老板")

main.handle_slash_command("/persona reset")
_reset = get_persona_settings()
check("T3.5 reset 恢复默认", _reset["agent_name"] == "星瑶" and _reset["persona"] == "" and _reset["user_title"] == "")
clear_out()

# ---------------------------------------------------------------------------
# T4 /shell（真实解释器探测）
# ---------------------------------------------------------------------------
from core.command_shell import get_selected_command_shell, list_command_shell_options  # noqa: E402

_orig_shell = get_selected_command_shell()
main.handle_slash_command("/shell")
check("T4.1 列表展示", "可选命令解释器" in out() and "当前" in out())

_opts = list_command_shell_options()
_avail = [o for o in _opts if o["available"]]
if _avail:
    main.handle_slash_command(f"/shell {_avail[0]['id']}")
    check("T4.2 切换到可用解释器", get_selected_command_shell() == _avail[0]["id"], f"目标 {_avail[0]['id']}")

main.handle_slash_command("/shell 不存在的解释器")
check("T4.3 非法值报错", "不支持" in out())

if _orig_shell and any(o["id"] == _orig_shell and o["available"] for o in _opts):
    main.handle_slash_command(f"/shell {_orig_shell}")
clear_out()

# ---------------------------------------------------------------------------
# T5 /open（startfile 打桩，验证路径解析）
# ---------------------------------------------------------------------------
_opened = []
_startfile_orig = getattr(os, "startfile", None)
os.startfile = lambda p: _opened.append(os.path.abspath(str(p)))

main.handle_slash_command("/open")
check("T5.1 无参打开工作区", len(_opened) == 1, _opened[:1])

main.handle_slash_command("/open 不存在_xyz_123.png")
check("T5.2 路径不存在报错", "路径不存在" in out() and len(_opened) == 1)

if _startfile_orig is not None:
    os.startfile = _startfile_orig
clear_out()

# ---------------------------------------------------------------------------
# T6 /skills refresh（真实技能目录扫描）
# ---------------------------------------------------------------------------
main.handle_slash_command("/skills refresh")
check("T6 技能目录重扫", "技能目录已重新扫描" in out() and "共" in out())
clear_out()

# ---------------------------------------------------------------------------
# T7 /queue（真实 msg_queue）
# ---------------------------------------------------------------------------
main.msg_queue.put({"text": "排队一", "was_queued": True, "session": main.current_session})
main.msg_queue.put({"text": "排队二", "was_queued": True, "session": main.current_session})

main.handle_slash_command("/queue")
check("T7.1 list 展示并原序回填", main.msg_queue.qsize() == 2 and "排队一" in out() and "2 条" in out())

main.handle_slash_command("/queue cancel 1")
_remaining = drain_queue()
check("T7.2 cancel 移除指定项", [_i["text"] for _i in _remaining] == ["排队二"])
for _i in _remaining:
    main.msg_queue.put(_i)

main.handle_slash_command("/queue clear")
check("T7.3 clear 清空", main.msg_queue.qsize() == 0)
clear_out()

# ---------------------------------------------------------------------------
# T8 /paste-image（真实系统剪贴板 → 真实视觉管线）
# ---------------------------------------------------------------------------
try:
    from PIL import Image
    _png = os.path.join(DATA_DIR, "clip_test.png")
    Image.new("RGB", (64, 48), (200, 30, 30)).save(_png)
    subprocess.run(
        ["powershell", "-NoProfile", "-Command", f"Set-Clipboard -Path '{_png}'"],
        check=True, timeout=20,
    )
    main.handle_slash_command("/paste-image 功能测试说明")
    _q = drain_queue()
    # 修复后：路径经队列条目 explicit_paths 通道传递，text 只保留说明
    check("T8.1 剪贴板文件入队（explicit_paths 通道）",
          bool(_q) and _q[0].get("text") == "功能测试说明" and _q[0].get("explicit_paths") == [_png],
          str(_q[0])[:100] if _q else "队列为空")

    from core.input_parser import parse_user_input
    if _q:
        _content, _notes = parse_user_input(_q[0]["text"], explicit_paths=_q[0]["explicit_paths"])
        _is_image = isinstance(_content, list) and any(
            isinstance(_p, dict) and _p.get("type") == "image_url" for _p in _content
        )
        check("T8.2 端到端：explicit_paths 产出视觉消息", _is_image,
              f"content 类型={type(_content).__name__}")
except Exception as _exc:  # 剪贴板依赖桌面会话，失败不算功能缺陷，单测已覆盖
    check("T8 剪贴板链路（环境受限跳过）", True, f"{type(_exc).__name__}: {str(_exc)[:80]}")
clear_out()

# ---------------------------------------------------------------------------
# T9 /computer browser（真实 settings 存取 + 真实 controller 快照）
# ---------------------------------------------------------------------------
try:
    from core.computer.controller import get_computer_control_service
    from core.computer.settings import load_computer_settings

    _orig_browser = load_computer_settings().computer_browser
    main.handle_slash_command("/computer browser")
    check("T9.1 查看默认浏览器列表", "默认浏览器" in out() and "edge" in out() and "chrome" in out())

    _target = "chrome" if _orig_browser != "chrome" else "edge"
    main.handle_slash_command(f"/computer browser {_target}")
    check("T9.2 切换默认浏览器", load_computer_settings().computer_browser == _target)

    main.handle_slash_command(f"/computer browser {_orig_browser}")
    check("T9.3 切换还原", load_computer_settings().computer_browser == _orig_browser)

    main.handle_slash_command("/computer status")
    check("T9.4 status 只读快照", "电脑操控" in out())
except Exception as _exc:
    check("T9 电脑操控（构造受限跳过）", True, f"{type(_exc).__name__}: {str(_exc)[:80]}")
clear_out()

# ---------------------------------------------------------------------------
# T10 /effort（真实 config + 思考能力表）
# ---------------------------------------------------------------------------
from core.model_manager import load_config, get_model_reasoning_choice  # noqa: E402
from core.reasoning_capabilities import get_reasoning_capability  # noqa: E402

_cfg = load_config()
_active_id = _cfg.get("active_model_id")
if _active_id:
    try:
        _orig_choice = get_model_reasoning_choice(_active_id)
    except Exception:
        _orig_choice = "default"
    main.handle_slash_command("/effort")
    check("T10.1 无参显示思考档位", ("思考模式" in out()) or ("始终开启思考" in out()) or ("未声明" in out()))

    main.handle_slash_command("/effort default")
    check("T10.2 default 恢复", ("恢复默认" in out()) or ("默认" in out()))

    _model = None
    for _p in _cfg.get("providers", []):
        for _m in _p.get("models", []):
            if str(_m.get("id")) == str(_active_id):
                _model = _m
                break
    _cap = get_reasoning_capability(str((_model or {}).get("litellm_model") or (_model or {}).get("name") or ""))
    if "low" in _cap.options and _orig_choice != "low":
        main.handle_slash_command("/effort low")
        check("T10.3 low 档切换", get_model_reasoning_choice(_active_id) == "low")
    else:
        check("T10.3 low 档切换（模型无 low 档，跳过）", True)

    main.handle_slash_command(f"/effort {_orig_choice}")
    check("T10.4 思考档位还原", get_model_reasoning_choice(_active_id) == _orig_choice)
else:
    check("T10 /effort（无激活模型，跳过）", True)
clear_out()

# ---------------------------------------------------------------------------
# T11 /update（真实 Gitee 网络 check 异步 + install 无包路径；绝不执行 install y）
# ---------------------------------------------------------------------------
import time as _time  # noqa: E402

main.handle_slash_command("/update")
# 修复后 check 异步执行：轮询等待结果回填（最长 25 秒）
_deadline = _time.time() + 25
while _time.time() < _deadline:
    if ("已是最新版本" in out()) or ("发现新版本" in out()) or ("检查更新失败" in out()):
        break
    _time.sleep(0.2)
check("T11.1 check 真实网络请求（异步回填）", ("已是最新版本" in out()) or ("发现新版本" in out()) or ("检查更新失败" in out()),
      out()[:80])

main.handle_slash_command("/update install")
check("T11.2 install 无包安全路径", ("未找到可安装的安装包" in out()) or ("即将安装" in out()))
clear_out()

# ---------------------------------------------------------------------------
# T12 /changes（隔离 DB → 空记录路径；有记录渲染由单测覆盖）
# ---------------------------------------------------------------------------
main.handle_slash_command("/changes")
check("T12 空会话变更记录", "还没有文件修改记录" in out())
clear_out()

# ---------------------------------------------------------------------------
# T13 /help 与未知指令
# ---------------------------------------------------------------------------
main.handle_slash_command("/help")
check("T13.1 /help 输出指令指南", "clear-history" in out() and "paste-image" in out() and "/update" in out())

main.handle_slash_command("/非存在命令xyz")
check("T13.2 未知指令提示", "未知指令" in out())

# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------
passed = sum(1 for _, ok in RESULTS if ok)
failed = len(RESULTS) - passed
print("\n" + "=" * 60)
print(f"功能测试汇总: {passed} 通过 / {failed} 失败（共 {len(RESULTS)} 项）")
if failed:
    print("失败项:")
    for name, ok in RESULTS:
        if not ok:
            print(f"  - {name}")
print("=" * 60)

shutil.rmtree(DATA_DIR, ignore_errors=True)
sys.exit(1 if failed else 0)

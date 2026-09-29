"""飞书卡片结构自检：把各类卡片发到真实会话，让飞书校验器逐张验收。

为什么需要它：飞书对卡片结构极其严格，结构有问题时**整卡被拒**，而用户侧只看到
「发消息没回复」——历史上已经踩过两次（表单内元素重名 230099/11310、note 标签）。
单元测试能挡住一部分红线（见 tests/test_feishu_cards.py），但平台校验器才是最终裁判，
本脚本把"我们的卡片能不能被飞书接受"变成一条可随时执行的命令。

用法：
    # 只打印卡片 JSON 与体积，不发送（CI/离线可用）
    python scripts/feishu_card_check.py --dry-run

    # 发到「最后一次说话的会话」（凭据与 chat 从配置/绑定状态读取）
    python scripts/feishu_card_check.py --send

    # 发到指定会话，或只挑几张
    python scripts/feishu_card_check.py --send --chat oc_xxx --only completion,permission

退出码：0 = 全部卡片被飞书接受；1 = 有卡片被拒（附飞书返回的 ErrPath/ErrMsg）。
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
from core.feishu.cards import (  # noqa: E402
    build_result_card,
    completion_card,
    failure_card,
    permission_card,
    question_card,
    with_status,
)
from core.feishu.client import FeishuApiClient, FeishuApiError  # noqa: E402
from core.feishu.commands import (  # noqa: E402
    agent_card,
    inbox_card,
    model_list_card,
    panel_card,
    session_list_card,
    stall_card,
    status_card,
    text_card,
)
from core.feishu.config import load_feishu_config  # noqa: E402

# 测试用假数据（真实 request_id 不存在，卡片上的按钮点了会提示"已失效"，属预期）
SESSION_LABEL = "飞书 · ou_abcdefgh"
WORKDIR = r"D:\pxx\xingyao-y-code"
SESSIONS = [
    {"name": "session_a", "display_name": "重构鉴权逻辑", "workdir": WORKDIR},
    {"name": "session_b", "display_name": "会话条目 2", "workdir": "GLOBAL"},
]


def build_cards():
    return {
        "completion": completion_card(SESSION_LABEL, "已列出当前目录：共 12 个文件，包括 README.md、core/ 等。", workdir=WORKDIR),
        "completion_cancelled": completion_card(SESSION_LABEL, "", workdir=WORKDIR, cancelled=True),
        "failure": failure_card(SESSION_LABEL, "模型调用失败：连接超时"),
        "result_ok": build_result_card("✅ 已批准", ["访问项目外目录 · C:/Windows/*"]),
        "result_bad": build_result_card("✖ 已拒绝", ["执行命令 · rm -rf build"]),
        "permission": permission_card(
            action_name="run_command", summary="执行命令：rm -rf build",
            targets=["**command**　rm -rf build"], session_label=SESSION_LABEL,
            workdir=WORKDIR, request_id="req-selftest", allow_always=True, buttons_enabled=True,
        ),
        "permission_collapsed": permission_card(
            action_name="write_file", summary="批量写入 8 个文件",
            targets=[f"**paths**　C:/dir/{i}/file.txt" for i in range(8)],
            session_label=SESSION_LABEL, request_id="req-selftest-2", allow_always=False,
            buttons_enabled=True,
        ),
        "permission_no_buttons": permission_card(
            action_name="external_directory", summary="访问项目外目录",
            targets=["**path**　C:/Windows/*"], session_label=SESSION_LABEL,
            request_id="req-selftest-3", allow_always=True, buttons_enabled=False,
            hint="未配置审批白名单（设置 → 飞书集成），请在桌面端处理本次授权。",
        ),
        "question": question_card(
            question="Which database would you like to use?", options=["SQLite", "PostgreSQL", "MySQL / MariaDB"],
            session_label=SESSION_LABEL, request_id="req-selftest-4", buttons_enabled=True,
        ),
        "panel": panel_card(),
        "status": status_card(
            session_label=SESSION_LABEL, session_name="session_a", workdir=WORKDIR,
            model_name="agnes-3.0-flash", connection_state="connected", approvers_count=1,
            pending_count=0, agent_name="星瑶",
        ),
        "session_list": session_list_card(sessions=SESSIONS, current_session="session_a", page=1),
        "model_list": model_list_card(
            models=[{"id": "agnes-3.0-flash", "name": "agnes-3.0-flash", "provider": "agnes"},
                    {"id": "gpt-4o", "name": "GPT-4o", "provider": "openai"}],
            active_model_id="agnes-3.0-flash", page=1,
        ),
        "inbox_empty": inbox_card(items=[], page=1),
        "inbox_items": inbox_card(items=[
            {"request_id": "req-1", "is_question": False, "title": "访问项目外目录 · C:/Windows/*", "session_label": SESSION_LABEL},
            {"request_id": "req-2", "is_question": True, "title": "Database Selection", "session_label": SESSION_LABEL},
        ], page=1),
        "stall": stall_card(session_label=SESSION_LABEL, last_text="重构 server/src 下的鉴权逻辑", minutes=12),
        "help": text_card("星瑶 · 帮助", "**星瑶 · 飞书指令**\n\n- `/help` 本帮助\n- `/whoami` 显示你的 open_id"),
        "agent": agent_card(agent_name="星瑶", model_name="agnes-3.0-flash"),
    }


def resolve_chat_id(explicit: str) -> str:
    if explicit:
        return explicit
    cfg = load_feishu_config()
    if cfg.notify_chat_id:
        return cfg.notify_chat_id
    path = os.path.join(config.YCODE_DIR, "feishu_state.json")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return str(data.get("last_chat_id") or "")
    except Exception:
        return ""


def main() -> int:
    parser = argparse.ArgumentParser(description="飞书卡片结构自检")
    parser.add_argument("--send", action="store_true", help="真正发送（默认只打印）")
    parser.add_argument("--dry-run", action="store_true", help="只打印卡片 JSON 与体积")
    parser.add_argument("--chat", default="", help="目标会话 chat_id（默认取配置/绑定状态）")
    parser.add_argument("--only", default="", help="只发指定卡片，逗号分隔")
    args = parser.parse_args()

    cards = build_cards()
    if args.only:
        wanted = {name.strip() for name in args.only.split(",") if name.strip()}
        unknown = wanted - set(cards)
        if unknown:
            print(f"❌ 未知卡片名：{sorted(unknown)}；可用：{sorted(cards)}")
            return 1
        cards = {name: card for name, card in cards.items() if name in wanted}

    if not args.send or args.dry_run:
        for name, card in cards.items():
            blob = json.dumps(card, ensure_ascii=False)
            print(f"{name:24s} {len(blob.encode('utf-8')):6d} 字节  schema={card.get('schema')}")
        print(f"\n共 {len(cards)} 张卡片；加 --send 会真实发送以验证飞书校验器")
        return 0

    cfg = load_feishu_config()
    if not cfg.configured:
        print("❌ 飞书未配置（需要 app_id/app_secret 且开关打开）")
        return 1
    chat_id = resolve_chat_id(args.chat)
    if not chat_id:
        print("❌ 拿不到目标会话 chat_id：请在飞书里先给机器人发一条消息，或用 --chat 指定")
        return 1

    client = FeishuApiClient(cfg.app_id, cfg.app_secret)
    print(f"发送到会话 {chat_id}（共 {len(cards)} 张）\n")
    failed = []
    sent_ids = {}
    for name, card in cards.items():
        try:
            resp = client.send_card(chat_id, card)
            sent_ids[name] = str(((resp or {}).get("data") or {}).get("message_id") or "")
            print(f"✅ {name}")
        except FeishuApiError as e:
            failed.append(name)
            print(f"❌ {name}: {e}")
        except Exception as e:  # pragma: no cover - 网络类异常
            failed.append(name)
            print(f"❌ {name}: {type(e).__name__}: {e}")

    # 原地回执卡（点按钮后返回的卡片，走 update_card 同一个校验器）：
    # 完成卡 + 一行状态是卡片表单追问后的实际返回形态，必须一并验
    completion_id = sent_ids.get("completion")
    if completion_id and "completion" in cards:
        try:
            client.update_card(completion_id, with_status(cards["completion"], "✅ 已发送 · 自检"))
            print("✅ completion+status（原地回执卡形态）")
        except FeishuApiError as e:
            failed.append("completion+status")
            print(f"❌ completion+status: {e}")

    print(f"\n结果：{len(cards) + (1 if completion_id else 0) - len(failed)}"
          f"/{len(cards) + (1 if completion_id else 0)} 张被飞书接受")
    if failed:
        print("被拒卡片：" + ", ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

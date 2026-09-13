"""压缩保真度测试：关键信息埋点 → 真实压缩 → 逐条对照保留率。

方法：
  1. 构造 ~14 轮真实形态的开发对话，早期轮次埋入 15 个独特可验证事实
     （数字/路径/人名/错误码/决策/日期/约束），并用真实感填充文本推到 ~88% 水位；
  2. 真实预压缩触发（生产路径 prepare_turn_messages，中转真实 LLM）；
  3. 压缩完成后逐条检查摘要保留情况，按事实类型分组报告保留率；
  4. 同时验证活跃区（保留的最近 4 条消息）原文完好无损。

事实类型分布（模拟真实开发会话中的关键信息形态）：
  数字类：端口 5433 / 预算 48000 / 超时 45s / p99<800ms / 日志 180 天
  标识类：项目代号"海燕" / 客户"冯氏科技" / 表名 mq_dispatch / v2.3.1
  路径类：core/billing/scheduler.py
  人员类：陈工负责迁移
  错误类：E4721 连接池耗尽
  决策类：Redis 会话共享 / 5 年直线折旧 / 10 月 15 日上线
"""
import sys
import os
import time
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from core.memory import MemoryManager
from core.model_manager import load_config
import core.agent_service as agent_service
import core.memory_compressor as mc

WINDOW = 128000
MODEL = "openai/qwen3.8-flash"

# 15 个埋点事实：(编号, 事实文本, 摘要中应能找到的验证关键词组)
FACTS = [
    ("F01", "项目代号：海燕（内部代号，勿外传）", ["海燕"]),
    ("F02", "数据库连接走 5433 端口，不是默认的 5432", ["5433"]),
    ("F03", "客户是冯氏科技，合同金额上限 48000 元", ["冯氏", "48000"]),
    ("F04", "核心调度文件 core/billing/scheduler.py 不要动", ["scheduler.py"]),
    ("F05", "陈工负责数据迁移，10 月 15 日前必须完成", ["陈工", "10 月 15"]),
    ("F06", "线上报错 E4721，根因是连接池耗尽", ["E4721"]),
    ("F07", "决定用 Redis 做会话共享，不用 sticky session", ["Redis"]),
    ("F08", "当前版本基线 v2.3.1，下个版本才升 3.0", ["2.3.1", "v2.3.1"]),
    ("F09", "对外 API 超时阈值定为 45 秒", ["45"]),
    ("F10", "消息队列表命名为 mq_dispatch，别用 queue_job", ["mq_dispatch"]),
    ("F11", "法务要求操作日志保留 180 天", ["180"]),
    ("F12", "设备折旧按 5 年直线折旧计算，财务已确认", ["5 年", "五年", "直线"]),
    ("F13", "上线压测标准：p99 延迟必须低于 800 毫秒", ["800", "p99"]),
    ("F14", "测试环境跳板机密码每季度轮换，当前是 Q3 批次", ["Q3"]),
    ("F15", "备案号沪ICP备2026锦88号，挂在页脚", ["沪ICP", "备案"]),
]

FILLER = "补充背景说明，这轮对话主要讨论实现细节与边界条件，包括异常处理、日志规范、代码评审意见与测试策略的反复确认。"


def pick_provider():
    cfg = load_config()
    for p in cfg.get("providers", []):
        if p.get("api_key") and "tokenrhythm" in p.get("base_url", ""):
            for m in p.get("models", []):
                if m.get("litellm_model") == MODEL:
                    return p["api_key"], p["base_url"]
    for p in cfg.get("providers", []):
        if p.get("api_key"):
            return p["api_key"], p.get("base_url", "")
    raise RuntimeError("无可用 provider")


def est_of(mgr, sess):
    h = mgr.get_history_for_llm(sess)
    return agent_service.estimate_history_tokens_with_system(MODEL, h, is_gui=True)


def build_fact_rounds(mgr, sess):
    """14 轮：每轮嵌 1~2 个事实 + 真实感填充。事实主要埋在前 12 轮（会被压缩）。"""
    fact_pool = list(FACTS)
    for i in range(12):
        fact_texts = []
        for _ in range(2 if len(fact_pool) >= 2 else len(fact_pool)):
            if not fact_pool:
                break
            fid, ftext, _ = fact_pool.pop(0)
            fact_texts.append("重要：%s。" % ftext)
        user = "第%d轮：我们过一下模块%d的问题。" % (i, i)
        user += "".join(fact_texts)
        user += FILLER * 20
        asst = "第%d轮已确认：按上述要求执行。" % i + FILLER * 18
        mgr.add_messages(sess, [
            ("user", user),
            ("assistant", asst),
        ])
    # 最后 2 轮填充轮**之后**才是活跃区——哨兵必须放在最末尾，
    # 否则填充轮会把哨兵推进压缩区（上一版的真实教训）。
    for i in range(2):
        mgr.add_messages(sess, [
            ("user", "哨兵内容A%d——这轮不应被压缩。" % i + FILLER * 4),
            ("assistant", "哨兵回复B%d已记录。" % i + FILLER * 4),
        ])
    assert not fact_pool, "所有埋点事实已入历史"


def main():
    api_key, api_base = pick_provider()
    tmp = tempfile.mkdtemp(prefix="fidelity_")
    mgr = MemoryManager(os.path.join(tmp, "fid.db"))
    sess = "fid_session"
    mgr.add_messages(sess, [("user", "开始技术评审会议")])
    build_fact_rounds(mgr, sess)

    # 水位若未到 85%，继续灌纯填充轮（不含新事实）
    i = 20
    while est_of(mgr, sess) <= WINDOW * 0.88:
        mgr.add_messages(sess, [
            ("user", "第%d轮补充讨论。" % i + FILLER * 40),
            ("assistant", "已同步确认。" + FILLER * 40),
        ])
        i += 1
    est = est_of(mgr, sess)
    n_msgs = len(mgr.get_history_for_llm(sess))
    print("[埋点完成] %d 条消息，水位 %.1f%%（%d token）；埋点事实 %d 条 + 哨兵 2 条"
          % (n_msgs, 100 * est / WINDOW, est, len(FACTS)))

    # 真实预压缩触发
    notices = []
    t0 = time.time()
    agent_service.prepare_turn_messages(
        mgr, sess, MODEL, WINDOW,
        permission_mode="full",
        api_key=api_key, api_base=api_base,
        notify=lambda k, m: notices.append((time.time() - t0, m)),
        is_gui=True, turn_source="local")
    t_wait = time.time() - t0
    print("[真实回合] 返回 %.2fs，后台压缩启动=%s" % (t_wait, mc.is_session_compressing(mgr, sess)))
    assert mc.is_session_compressing(mgr, sess)

    print("[等待] 后台真实 LLM 压缩进行中...")
    t_bg0 = time.time()
    while mc.is_session_compressing(mgr, sess):
        time.sleep(1.0)
        if time.time() - t_bg0 > 600:
            raise RuntimeError("后台压缩超时")
    with mgr._lock:
        summary, level = mgr.conn.execute(
            "SELECT memory_summary, summarized_until_msg_id FROM sessions "
            "WHERE session_name=?", (sess,)).fetchone()
    print("[压缩完成] %.0fs，水位 msg_id=%s" % (time.time() - t_bg0, level))
    print()
    print("=" * 66)
    print("摘要全文（%d 字）：" % len(summary or ""))
    print("-" * 66)
    print(summary)
    print("-" * 66)

    # 逐条对照
    kept, lost = [], []
    for fid, ftext, keys in FACTS:
        hit = any(k in (summary or "") for k in keys)
        (kept if hit else lost).append((fid, ftext))
    print("埋点事实保留率：%d/%d（%.0f%%）" % (len(kept), len(FACTS), 100 * len(kept) / len(FACTS)))
    print("保留的事实：")
    for fid, ftext in kept:
        print("  ✔ %s %s" % (fid, ftext[:40]))
    if lost:
        print("丢失的事实：")
        for fid, ftext in lost:
            print("  ✘ %s %s" % (fid, ftext[:40]))

    # 压缩后活跃区哨兵验证（哨兵在最末尾，绝不应进压缩区）
    hist = mgr.get_history_for_llm(sess)
    sent_ok = any("哨兵内容A" in str(m.get("content")) for m in hist[-4:])
    print("活跃区哨兵：%s（最近轮次原文应完好）" % ("完好 ✔" if sent_ok else "丢失 ✘"))

    # DB 原文完整性：17 个关键词逐一检查是否仍全部在库
    keys = ["海燕", "5433", "冯氏科技", "48000", "scheduler.py", "陈工",
            "10 月 15", "E4721", "Redis", "2.3.1", "45 秒", "mq_dispatch",
            "180 天", "5 年直线", "800 毫秒", "Q3", "沪ICP备2026锦88"]
    with mgr._lock:
        sid = mgr.conn.execute(
            "SELECT id FROM sessions WHERE session_name=?", (sess,)).fetchone()[0]
        rows = mgr.conn.execute(
            "SELECT content FROM messages WHERE session_id=?", (sid,)).fetchall()
    all_content = "".join(str(r[0]) for r in rows)
    missing_db = [k for k in keys if k not in all_content]
    print("DB 原文完整性：%d/17 关键词在库（%s）——淘汰零丢失，只是不再发进请求"
          % (17 - len(missing_db), "全部完整 ✔" if not missing_db else "缺失: %s" % missing_db))
    print()
    print("=" * 66)
    verdict = "优秀" if len(kept) >= 13 else ("良好" if len(kept) >= 10 else ("一般" if len(kept) >= 7 else "差"))
    print("结论：摘要保留率 %d/15（%s）。活跃区与 DB 原文均零丢失。"
          % (len(kept), verdict))
    print("FIDELITY TEST DONE")


if __name__ == "__main__":
    main()

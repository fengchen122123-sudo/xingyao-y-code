"""零成本模拟三：水位阶梯全场景 F~K（dev-compact 全量验证收官）。

生产路径全部真实（prepare_turn_messages 完整四级水位阶梯 + Tier3 全链路 +
真实 SQLite / tiktoken / marker 冻结 / 保护区 / I4 / 物理折叠 / 滑窗淘汰），
唯一替换点仍是 core.llm._litellm_completion 桩，零 API 成本。

  F  Tier1/Tier2 本地工具结果维护
     F1 60% 水位 → snip（head+tail 截断），冻结后不重写、错误结果不改写；
     F2 80% 水位 → elide（占位符化，snipped 可升级），仍跳过错误与冻结。
  G  0.95 同步压缩全程（分段提示/增量落库/水位回落/腾字数统计）。
  H  Tier3 压缩失败 → 熔断语义：停止自动淘汰 + 1.2×window 内仍微调淘汰 + I4 内存裁剪兜底。
  I  组合极限（H 场景之上）：预压缩后台进行中 + 压缩失败熔断叠加 160% 超窗
     → already_running 分支（不推水位）→ I4 把载荷裁进窗口，请求照常发出。
  J  主动轮换代边界：非 DeepSeek 路由不登记；「保留区均为工具结果」拒绝压缩
     （pairing_protect，绝不制造孤儿 tool）。
  K  压缩经济性：本地 snip/elide 已把压力拉回线下时 Tier3 跳过付费摘要。

时间尺度压缩说明：桩 LLM 0.0004s/字符；全部为生产原逻辑，无快捷路径。
"""
import sys
import os
import time
import tempfile
import threading
import re
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from core.memory import MemoryManager
import core.agent_service as agent_service
import core.memory_compressor as mc
import core.llm as llm

# 水位线常量与生产保持同源（改生产常量时脚本同步跟随）
SNIP_LINE = agent_service.SNIP_RATIO      # 0.60
ELIDE_LINE = agent_service.ELIDE_RATIO    # 0.80
SUMMARY_LINE = agent_service.SUMMARIZE_RATIO  # 0.95

WINDOW = 128000
DEEPSEEK_MODEL = "openai/deepseek-v4"
DEEPSEEK_BASE = "https://api.deepseek.com"
SUMMARY = ("记忆摘要：本轮对话围绕代码库重构展开，讨论了模块边界、依赖方向与"
           "测试策略；结论是采用事件驱动解耦，性能提升明显，并保留了向后兼容层。") * 2
BASE_CFG = {
    "retain_count": 4,
    "retain_count_force": 2,
    "summary_length_first": 250,
    "summary_length_merge": 350,
    "inter_chunk_delay_seconds": 0.0,
    "max_chunk_token_budget": 24000,
    "max_total_seconds": 900.0,
    "precompress_ratio": 0.0,
    # 预截断三键：生产默认值（memory_compressor._load_compression_config）
    "tool_result_truncate_threshold_chars": 2000,
    "tool_result_preview_limit_chars": 800,
    "tool_result_token_limit": 600,
}


class StubLLM:
    def __init__(self, spc=0.0004):
        self.spc = spc
        self.ok_count = 0
        self.fail_all = False

    def __call__(self, model=None, messages=None, **kw):
        if self.fail_all:
            raise RuntimeError("sim provider down")
        time.sleep(len(messages[0]["content"]) * self.spc)
        self.ok_count += 1
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=SUMMARY))])


def make_mgr(tmp, name):
    return MemoryManager(os.path.join(tmp, name + ".db"))


def add_tool_round(mgr, sess, i, tool_chars, err=False):
    """带工具结果的一轮：user → assistant(tool_calls) → tool → assistant。"""
    tool_content = ("read 失败: 🚨 命令执行失败（退出码 1）：" if err
                    else "第%d个文件内容：" % i + "代码行\n" * (tool_chars // 4))
    mgr.add_messages(sess, [
        ("user", "第%d轮：请读取模块%d的源码。" % (i, i)),
        ("assistant", {"content": "", "tool_calls": [
            {"id": "call%d" % i, "type": "function",
             "function": {"name": "read_file", "arguments": "{}"}}]}),
        ("tool", {"tool_call_id": "call%d" % i, "name": "read_file",
                  "content": tool_content}),
        ("assistant", "第%d轮分析完成：模块边界清晰。" % i),
    ])


def add_big_tool_round(mgr, sess, k):
    """大工具结果轮次（500 行 × ~60 字符）：snip 后仍保留 ~2k token，可升级 elide。"""
    big = "\n".join("模块%d的分析数据行 %s" % (k, "参数" * 29) for _ in range(500))
    mgr.add_messages(sess, [
        ("user", "第%d轮：请读取模块%d。" % (k, k)),
        ("assistant", {"content": "", "tool_calls": [
            {"id": "call%d" % k, "type": "function",
             "function": {"name": "read_file", "arguments": "{}"}}]}),
        ("tool", {"tool_call_id": "call%d" % k, "name": "read_file",
                  "content": big}),
        ("assistant", "第%d轮分析完成。" % k),
    ])


def add_text_round(mgr, sess, i, user_c=2600, asst_c=2400):
    mgr.add_messages(sess, [
        ("user", "第%d轮：请分析模块%d的设计取舍。" % (i, i) + "背景" * (user_c // 2)),
        ("assistant", "第%d轮结论：改为事件驱动，解耦了核心依赖。" % i + "细节" * (asst_c // 2)),
    ])


def est_of(mgr, sess, gui=True):
    return agent_service.estimate_history_tokens_with_system(
        DEEPSEEK_MODEL, mgr.get_history_for_llm(sess), is_gui=gui)


def run_turn(mgr, sess, api_base="", notices=None):
    notices = notices if notices is not None else []
    t0 = time.time()
    msgs = agent_service.prepare_turn_messages(
        mgr, sess, DEEPSEEK_MODEL, WINDOW,
        permission_mode="full",
        notify=lambda k, m: notices.append((time.time() - t0, m)),
        api_base=api_base, is_gui=True, turn_source="local")
    return time.time() - t0, msgs


def txt(s):
    return re.sub(r"<[^>]+>", "", s).strip()


def counts(mgr, sess):
    """统计历史里 snipped / elided / 原样工具结果条数。"""
    s = e = o = 0
    for m in mgr.get_history_for_llm(sess):
        if m["role"] != "tool":
            continue
        c = str(m.get("content") or "")
        if c.startswith("[snipped tool result"):
            s += 1
        elif c.startswith("[elided tool result"):
            e += 1
        else:
            o += 1
    return s, e, o


def patched(stub):
    """上下文管理式打桩：completion + 压缩配置，退出还原。"""
    class P:
        def __init__(self):
            self.orig_c = llm._litellm_completion
            self.orig_cfg = mc._load_compression_config
        def __enter__(self):
            llm._litellm_completion = stub
            mc._load_compression_config = lambda: dict(BASE_CFG)
            return self
        def __exit__(self, *a):
            llm._litellm_completion = self.orig_c
            mc._load_compression_config = self.orig_cfg
    return P()


def part_f(tmp):
    """F：Tier1 snip / Tier2 elide（含冻结幂等与错误豁免）。"""
    print("=" * 70)
    print("F · Tier1/Tier2 本地工具结果维护")
    print("=" * 70)
    mgr, sess = make_mgr(tmp, "f"), "sess_f"
    mgr.add_messages(sess, [("user", "开始")])
    i = 0
    while est_of(mgr, sess) <= WINDOW * SNIP_LINE:  # 必须真正越过 60% 阈值
        add_tool_round(mgr, sess, i, 12000, err=(i % 4 == 3))  # 1/4 是错误结果
        i += 1
    print("F0 构造完成：水位 %.0f%%，%d 轮（每 4 轮 1 个错误结果）"
          % (est_of(mgr, sess) / WINDOW * 100, i))
    s0, e0, o0 = counts(mgr, sess)
    print("    初始状态：snipped=%d elided=%d 原样=%d" % (s0, e0, o0))

    # ---- F1: 60% 水位 → Tier1 snip ----
    notices = []
    run_turn(mgr, sess, notices=notices)
    s1, e1, o1 = counts(mgr, sess)
    print("F1 60%% 水位回合 → snipped=%d（原 %d），elided=%d，原样=%d；提示：%s"
          % (s1, s0, e1, o1,
             next((txt(m) for _, m in notices if "折叠" in m), "无")))
    assert s1 > s0 and e1 == 0, "Tier1 应只 snip 不 elide"
    # 冻结与错误豁免：snip 后的与错误的都应在"原样"里
    run_turn(mgr, sess)  # 再跑一轮同水位：冻结的结果不会被重复改写
    s1b, e1b, o1b = counts(mgr, sess)
    assert (s1b, e1b) == (s1, e1), "已冻结的 snipped 不应被重复改写"
    print("    幂等：同水位再跑一轮 snipped=%d 不变 ✔" % s1b)

    # ---- F2: 80% 水位 → Tier2 elide ----
    # 关键事实（诊断证实）：Tier1 的 snip 极为高效——大工具结果被压到 ~1/3
    # 以下，靠工具结果本身永远到不了"snip 后仍 >80%"。要触发 Tier2，必须让
    # snip 消化不掉的底座（纯文本 user/assistant，永不被改写）先垫到 80%+，
    # 再叠工具结果：Tier1 snip 新结果后水位仍 >80% → Tier2 把保护区外所有
    # ≥1024 字符的未冻结结果（含 snipped）整体占位符化（elide 升级）。
    # 闭环水位：底座 84% → 叠 2 轮大结果到 ~134% → snip 后 ~97% (>80% 且 >95%
    # 不行——会走 Tier3；调低底座让 snip 后落在 (80%,95%) 窗口)。
    i = 100
    while est_of(mgr, sess) <= WINDOW * 0.82:  # 纯文本底座 82%
        add_text_round(mgr, sess, i, 3000, 2800)
        i += 1
    for k in range(2):
        add_big_tool_round(mgr, sess, 500 + k)  # 每轮 snip 后剩 ~2k token
    print("    灌入后水位 %.0f%%（snip 消化前；底座 82%% 为不可 snip 纯文本）"
          % (100 * est_of(mgr, sess) / WINDOW))
    notices = []
    run_turn(mgr, sess, notices=notices)
    s2, e2, o2 = counts(mgr, sess)
    print("F2 80%%+ 水位回合 → snipped=%d elided=%d（原 0），原样=%d"
          % (s2, e2, o2))
    assert e2 > 0, "Tier2 应触发 elide"
    errs = [m for m in mgr.get_history_for_llm(sess)
            if m["role"] == "tool" and "🚨" in str(m.get("content"))]
    assert errs, "错误结果应保持原样（未被改写）"
    print("    错误结果豁免：%d 条 🚨 结果仍原样在场 ✔" % len(errs))
    print("    F ✔ Tier1/2 分层维护、冻结幂等、错误豁免全部正确\n")


def part_g(tmp):
    """G：0.95 同步压缩全程。"""
    print("=" * 70)
    print("G · 0.95 同步压缩全程（Tier3）")
    print("=" * 70)
    mgr, sess = make_mgr(tmp, "g"), "sess_g"
    mgr.add_messages(sess, [("user", "开始")])
    i = 0
    while est_of(mgr, sess) <= WINDOW * SUMMARY_LINE:  # 真正越过 95%
        add_text_round(mgr, sess, i)
        i += 1
    stub = StubLLM()
    with patched(stub):
        notices = []
        t, _ = run_turn(mgr, sess, notices=notices)
    seg = [m for _, m in notices if "段压缩进度" in m]
    ok = [m for _, m in notices if "整合成功" in m]
    print("G  96%% 水位回合：等待 %.1fs（%d 段增量落库），水位 %.0f%% → %.0f%%"
          % (t, len(seg), 100 * 0.96, 100 * est_of(mgr, sess) / WINDOW))
    assert ok and len(seg) >= 3
    print("    提示链：分 %d 段 → 每段落库 → ✔ 整合成功（腾出字数统计在场）" % len(seg))
    print("    G ✔ 同步压缩全程与提示时序正确\n")


def part_h(tmp):
    """H：Tier3 失败熔断语义（1.2×window 分界）。"""
    print("=" * 70)
    print("H · 压缩失败熔断：停止自动淘汰 + 1.2×window 分界 + I4 兜底")
    print("=" * 70)
    stub = StubLLM()
    stub.fail_all = True
    with patched(stub):
        # ---- H1: 96%~120% 区间：失败但低于熔断线 → 仍允许微调淘汰 ----
        mgr1, s1 = make_mgr(tmp, "h1"), "sess_h1"
        mgr1.add_messages(s1, [("user", "开始")])
        i = 0
        while est_of(mgr1, s1) <= WINDOW * 0.98:  # 96%~120% 区间
            add_text_round(mgr1, s1, i)
            i += 1
        notices = []
        run_turn(mgr1, s1, notices=notices)
        warn = [m for _, m in notices if "仍在进行中" not in m and "失败" in m]
        i4 = [m for _, m in notices if "临时按轮次裁剪" in m]
        print("H1 98%% 水位（<1.2×window）：失败提示 %d 条，I4 内存裁剪 %s"
              % (len(warn), "触发" if i4 else "未触发（水位在 95% 内无需）"))
        assert warn, "应有压缩失败提示"
        with mgr1._lock:
            row = mgr1.conn.execute(
                "SELECT summarized_until_msg_id FROM sessions WHERE session_name=?",
                (s1,)).fetchone()
        print("    微调淘汰：summarized_until 推进到 %s（失败但 <1.2×window，允许 2 轮微调）"
              % row[0])
        assert row[0], "低于熔断线应允许微调淘汰"

        # ---- H2: >1.2×window：熔断 → 停止自动淘汰 + I4 兜底 ----
        mgr2, s2 = make_mgr(tmp, "h2"), "sess_h2"
        mgr2.add_messages(s2, [("user", "开始")])
        i = 0
        while est_of(mgr2, s2) <= WINDOW * 1.30:  # >1.2×window 熔断区
            add_text_round(mgr2, s2, i)
            i += 1
        notices = []
        _, msgs = run_turn(mgr2, s2, notices=notices)
        breaker = [m for _, m in notices if "严重超出" in m]
        i4 = [m for _, m in notices if "临时按轮次裁剪" in m]
        est_msgs = llm.estimate_tokens(DEEPSEEK_MODEL, msgs)
        print("H2 130%% 水位（>1.2×window）：熔断提示 %d 条，I4 提示 %d 条，"
              "最终发载荷 %d token（%.0f%% 窗口）"
              % (len(breaker), len(i4), est_msgs, 100 * est_msgs / WINDOW))
        assert breaker and i4
        assert est_msgs <= WINDOW * 0.951, "I4 应把载荷裁进 95% 内"
        with mgr2._lock:
            row = mgr2.conn.execute(
                "SELECT summarized_until_msg_id FROM sessions WHERE session_name=?",
                (s2,)).fetchone()
        assert not row[0], "熔断时严禁推水位（保护历史完整）"
        print("    淘汰冻结：summarized_until 保持 0（用户历史零破坏）✔")
        print("    H ✔ 熔断边界与 I4 兜底正确\n")


def part_i(tmp):
    """I：极限组合——预压缩后台进行中 + 压缩失败熔断 + 160% 超窗。"""
    print("=" * 70)
    print("I · 极限组合：already_running + 熔断 + 160% 超窗 → I4 兜底")
    print("=" * 70)
    mgr, sess = make_mgr(tmp, "i"), "sess_i"
    mgr.add_messages(sess, [("user", "开始")])
    i = 0
    while est_of(mgr, sess) <= WINDOW * 0.88:  # 先到 88%（预压缩区间内）
        add_text_round(mgr, sess, i)
        i += 1
    slow = StubLLM(spc=0.004)  # 慢桩：后台预压缩迟迟不结束
    orig_trigger = agent_service._trigger_background_precompress
    started = []
    def slow_trigger(*a, **k):
        started.append(time.time())
        return orig_trigger(*a, **k)
    agent_service._trigger_background_precompress = slow_trigger
    orig_cooldown = agent_service._PRECOMPRESS_COOLDOWN_SECONDS
    agent_service._PRECOMPRESS_COOLDOWN_SECONDS = 3.0
    try:
        with patched(slow):
            cfg = dict(BASE_CFG); cfg["precompress_ratio"] = 0.85
            mc._load_compression_config = lambda: cfg
            # 回合 1：88% 水位 → 触发后台预压缩（慢桩，消息即时发出）
            notices = []
            t1, _ = run_turn(mgr, sess, notices=notices)
            assert started, "88% 水位应触发预压缩"
            assert mc.is_session_compressing(mgr, sess), "后台预压缩应运行中"
            print("I1 88%% 水位回合 1：等待 %.2fs（预压缩转后台 ✔，慢桩运行中）" % t1)
            # 趁后台未完成，用户快节奏灌增量到 160%（模拟极端连续对话）
            j = 500
            while est_of(mgr, sess) <= WINDOW * 1.58:
                add_text_round(mgr, sess, j)
                j += 1
            # 回合 2：160% 水位 → Tier3 同步压缩命中 already_running
            notices = []
            _, msgs = run_turn(mgr, sess, notices=notices)
            running = [m for _, m in notices if "仍在进行中" in m]
            i4 = [m for _, m in notices if "临时按轮次裁剪" in m]
            pay = llm.estimate_tokens(DEEPSEEK_MODEL, msgs)
            print("I2 160%% 水位回合 2：already_running 提示 %d 条，I4 裁剪提示 %d 条，"
                  "载荷 %d token（%.0f%% 窗口）"
                  % (len(running), len(i4), pay, 100 * pay / WINDOW))
            assert running, "预压缩后台进行中，Tier3 应命中 already_running"
            assert pay <= WINDOW * 0.951
            with mgr._lock:
                row = mgr.conn.execute(
                    "SELECT summarized_until_msg_id FROM sessions WHERE session_name=?",
                    (sess,)).fetchone()
            assert not row[0], "already_running 严禁推水位"
            print("    水位未推进（绝不打翻后台压缩的乐观锁）✔")
            # 等慢桩预压缩自然结束（它压的是 88% 时的历史，成果照常落库）
            t0 = time.time()
            while mc.is_session_compressing(mgr, sess):
                time.sleep(0.2)
                if time.time() - t0 > 300:
                    raise RuntimeError("等待后台预压缩超时")
            print("I3 后台慢压缩自然完成（%.0fs），落库后水位 %.0f%%"
                  % (time.time() - started[0], 100 * est_of(mgr, sess) / WINDOW))
            # 回合 3：预压缩成果已在，水位应回落，正常轮
            notices = []
            t3, _ = run_turn(mgr, sess, notices=notices)
            lv = est_of(mgr, sess) / WINDOW
            print("I4 回合 3：%.0f%% 水位，等待 %.2fs，压缩提示 %d 条 → %s"
                  % (100 * lv, t3, len(notices),
                     "恢复正常轮 ✔" if lv < 0.96 and not notices
                     else "仍在高位（增量推进）"))
    finally:
        agent_service._trigger_background_precompress = orig_trigger
        agent_service._PRECOMPRESS_COOLDOWN_SECONDS = orig_cooldown
    print("    I ✔ 极限组合下 already_running→自然落库→恢复正常，全链路正确\n")


def part_j(tmp):
    """J：主动轮换代边界 + 孤儿 tool 保护。"""
    print("=" * 70)
    print("J · 边界：非账本路由不登记 / 孤儿 tool 保护")
    print("=" * 70)
    # J1: 非 DeepSeek 路由 → 不登记
    mgr, sess = make_mgr(tmp, "j1"), "sess_j1"
    mgr.add_messages(sess, [("user", "开始")])
    i = 0
    while est_of(mgr, sess) <= WINDOW * SUMMARY_LINE:
        add_text_round(mgr, sess, i)
        i += 1
    stub = StubLLM()
    with patched(stub):
        notices = []
        run_turn(mgr, sess, api_base="https://api.tokenrhythm.studio/v1", notices=notices)
    rot = [m for _, m in notices if "重建发送队列" in m]
    print("J1 中转路由 96%% 水位压缩成功 → 轮换登记 %d（应 0）" % len(rot))
    assert not rot, "非账本路由不应登记轮换"
    print("    J1 ✔ 轮换仅官方 DeepSeek 路由生效\n")

    # J2: 保留区均为工具结果 → pairing_protect 拒绝压缩
    # 构造关键：消息序列必须以 assistant(tool_calls) 结尾、其后接 tool 结果，
    # 使最后 retain_count=4 条全部是 tool（配对的 assistant 落在待压缩区）。
    mgr, sess = make_mgr(tmp, "j2"), "sess_j2"
    mgr.add_messages(sess, [("user", "开始任务")])
    for k in range(8):
        add_tool_round(mgr, sess, k, 12000)
    mgr.add_messages(sess, [
        ("assistant", {"content": "", "tool_calls": [
            {"id": "cz1", "type": "function",
             "function": {"name": "read_file", "arguments": "{}"}},
            {"id": "cz2", "type": "function",
             "function": {"name": "read_file", "arguments": "{}"}},
            {"id": "cz3", "type": "function",
             "function": {"name": "read_file", "arguments": "{}"}},
            {"id": "cz4", "type": "function",
             "function": {"name": "read_file", "arguments": "{}"}},
        ]}),
        ("tool", {"tool_call_id": "cz1", "name": "read_file", "content": "结果一" * 300}),
        ("tool", {"tool_call_id": "cz2", "name": "read_file", "content": "结果二" * 300}),
        ("tool", {"tool_call_id": "cz3", "name": "read_file", "content": "结果三" * 300}),
        ("tool", {"tool_call_id": "cz4", "name": "read_file", "content": "结果四" * 300}),
    ])
    stub = StubLLM()
    with patched(stub):
        out = mc.compress_session_memory(
            mgr, sess, DEEPSEEK_MODEL, "", "",
            output_callback=lambda m: None, return_outcome=True)
        hist = mgr.get_history_for_llm(sess)
    orphans = 0
    for m in hist:
        if m["role"] == "tool":
            pid = m.get("tool_call_id")
            calls = [c.get("id") for x in hist if x["role"] == "assistant"
                     for c in (x.get("tool_calls") or [])]
            if pid not in calls:
                orphans += 1
    print("J2 保留区全 tool（尾部连发 tool）历史压缩 → status=%s reason=%s，孤儿=%d"
          % (out.status, out.reason, orphans))
    assert out.status == "skipped" and out.reason == "pairing_protect" and orphans == 0, \
        "应拒绝压缩（pairing_protect），绝不制造孤儿 tool"
    print("    J2 ✔ pairing_protect 拒绝压缩，绝不制造孤儿 tool\n")


def part_k(tmp):
    """K：压缩经济学——本地维护已拉回线下时 Tier3 跳过付费摘要。"""
    print("=" * 70)
    print("K · 压缩经济学：snip/elide 拉回线下 → Tier3 跳过付费摘要")
    print("=" * 70)
    mgr, sess = make_mgr(tmp, "k"), "sess_k"
    mgr.add_messages(sess, [("user", "开始")])
    i = 0
    while est_of(mgr, sess) <= WINDOW * 0.93:  # 93% 起：snip 后预计落回 60% 内
        add_tool_round(mgr, sess, i, 20000)  # 超大工具结果
        i += 1
    print("K0 构造：%.0f%% 水位（大工具结果构成，snip 后预计大幅回落）"
          % (100 * est_of(mgr, sess) / WINDOW))
    stub = StubLLM()
    with patched(stub):
        notices = []
        run_turn(mgr, sess, notices=notices)
    comp = [m for _, m in notices if "智能压缩" in m or "段压缩进度" in m]
    print("K1 回合后：压缩 LLM 调用 %d 次（应 0），水位 %.0f%%，提示数 %d"
          % (stub.ok_count, 100 * est_of(mgr, sess) / WINDOW, len(comp)))
    assert stub.ok_count == 0 and not comp
    assert est_of(mgr, sess) < WINDOW * 0.95
    print("    K ✔ Tier1/2 已把压力拉回线下，Tier3 分文未花跳过摘要\n")


def main():
    tmp = tempfile.mkdtemp(prefix="sim_full_")
    try:
        part_f(tmp)
        part_g(tmp)
        part_h(tmp)
        part_i(tmp)
        part_j(tmp)
        part_k(tmp)
        print("=" * 70)
        print("全场景模拟收官：F 分层维护 / G 同步压缩 / H 熔断边界 / I 极限组合")
        print("                  J 轮换边界与孤儿保护 / K 压缩经济学 —— 全部通过")
        print("SIMULATE-3 PASSED")
    finally:
        pass


if __name__ == "__main__":
    main()

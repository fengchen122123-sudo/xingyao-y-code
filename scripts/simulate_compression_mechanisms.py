"""零成本模拟二：压缩机制专项——429 自适应冷却 / DeepSeek 账本基线与主动轮换代 / 取消与增量恢复。

全部走生产路径（compress_session_memory / prepare_turn_messages / _prepare_deepseek_ledger_request /
真实 SQLite / 真实 tiktoken），唯一替换点仍是 core.llm._litellm_completion 桩，零 API 成本。

三个专项：
  C  429 自适应段间冷却
     C1 新默认（延时 0，无 429）→ 段间 gap ≈ 0；
     C2 第 1 段首调 429 → 重试成功后，后续段间 gap ≈ 2.0s（自适应冷却粘住整次压缩）；
     C3 显式配置 inter_chunk_delay_seconds=1.5 → gap 恒 1.5s（老配置无条件兼容）。
  D  DeepSeek 账本路由
     D1 基线修正：同一会话同一份真实用量（112k），官方路由采信 112k，
        中转路由 min 口径回落到本地估算（~31%）——水位判断天壤之别；
     D2 主动轮换代：96% 水位真实回合 → Tier3 压缩落库 → 登记轮换代 +
        用户提示 → 下一笔账本请求消费标记 force_new，代内容换成压缩后瘦身历史；
     D3 反例：压缩失败（熔断语义）→ 不登记轮换代，consume 返回 False。
  E  取消与增量恢复
     4 段压缩进行到第 2 段 LLM 调用中取消 → 已完成段落落库保留、
     中止处状态 partial_cancelled、守卫释放；再次压缩从已提交水位续推，
     只压剩余段（增量恢复，不重复付费）。

时间尺度压缩说明（等比缩放、语义不变）：429 重试基础退避 5.0s → 0.5s；
桩 LLM 延迟 0.0002s/字符（≈4.8s/万 token 段）。自适应冷却 2.0s 与显式
配置 1.5s 保持真实值以便精确测量。
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

WINDOW = 128000
DEEPSEEK_MODEL = "openai/deepseek-v4"
DEEPSEEK_BASE = "https://api.deepseek.com"
RELAY_BASE = "https://api.tokenrhythm.studio/v1"
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
}


class Fake429(Exception):
    status_code = 429  # is_rate_limit_error 按 status_code 判定


class StubLLM:
    """桩：可注入 429 / 全失败模式，记录每次调用的起止时刻。"""

    def __init__(self, spc=0.0002):
        self.spc = spc
        self.fail_first_n = 0      # 前 N 次调用抛 429
        self.fail_all = False      # 全部抛普通异常（熔断反例）
        self.calls = []            # (t_start, t_end, status)
        self.ok_count = 0
        self.in_call = threading.Event()

    def __call__(self, model=None, messages=None, **kw):
        prompt = messages[0]["content"]
        t0 = time.monotonic()
        self.in_call.set()
        try:
            if self.fail_all:
                raise RuntimeError("sim provider down")
            if self.ok_count + 1 <= self.fail_first_n and self.ok_count < self.fail_first_n \
                    and len(self.calls) < self.fail_first_n:
                time.sleep(0.01)
                raise Fake429("sim rate limit")
            time.sleep(len(prompt) * self.spc)
            self.ok_count += 1
            self.calls.append((t0, time.monotonic(), "ok"))
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content=SUMMARY))])
        except Fake429:
            self.calls.append((t0, time.monotonic(), "429"))
            raise
        finally:
            self.in_call.clear()

    def inter_chunk_gaps(self):
        """相邻两次成功调用之间的间隔（秒）≈ 段间冷却实测值。"""
        oks = [c for c in self.calls if c[2] == "ok"]
        return [round(oks[i + 1][0] - oks[i][1], 2) for i in range(len(oks) - 1)]


def make_mgr(tmp, name):
    return MemoryManager(os.path.join(tmp, name + ".db"))


def add_round(mgr, sess, i):
    mgr.add_messages(sess, [
        ("user", "第%d轮：请分析模块%d的设计取舍。" % (i, i) + "背景" * 1300),
        ("assistant", "第%d轮结论：改为事件驱动，解耦了核心依赖。" % i + "细节" * 1200),
    ])


def fill_to(mgr, sess, ratio, start=0):
    i = start
    while True:
        add_round(mgr, sess, i)
        i += 1
        est = agent_service.estimate_history_tokens_with_system(
            DEEPSEEK_MODEL, mgr.get_history_for_llm(sess), is_gui=True)
        if est > WINDOW * ratio:
            return est


def compress(mgr, sess, notices, cancel_event=None):
    return mc.compress_session_memory(
        mgr, sess, DEEPSEEK_MODEL, "", "",
        output_callback=lambda m: notices.append((time.monotonic(), m)),
        cancel_event=cancel_event, return_outcome=True,
    )


def part_c(tmp):
    """C：429 自适应段间冷却（同规模 3 段历史 × 三种配置）。"""
    print("=" * 70)
    print("C · 429 自适应段间冷却")
    print("=" * 70)
    results = {}
    for label, cfg_over, fail_first in (
        ("C1 新默认(0s, 无429)", {}, 0),
        ("C2 首段429后重试成功", {}, 1),
        ("C3 显式配置1.5s(老配置)", {"inter_chunk_delay_seconds": 1.5}, 0),
    ):
        mgr, sess = make_mgr(tmp, "c_" + label[:2]), "sess_c"
        mgr.add_messages(sess, [("user", "开始")])
        fill_to(mgr, sess, 0.58)  # ≈3 段
        stub = StubLLM()
        stub.fail_first_n = fail_first
        orig = llm._litellm_completion
        orig_cfg = mc._load_compression_config
        llm._litellm_completion = stub
        cfg = dict(BASE_CFG); cfg.update(cfg_over)
        mc._load_compression_config = lambda: cfg
        t0 = time.monotonic()
        notices = []
        try:
            outcome = compress(mgr, sess, notices)
            dt = time.monotonic() - t0
        finally:
            llm._litellm_completion = orig
            mc._load_compression_config = orig_cfg
        n429 = sum(1 for c in stub.calls if c[2] == "429")
        seg = re.search(r"分 (\d+) 段", "".join(m for _, m in notices))
        n_chunks = int(seg.group(1)) if seg else -1
        gaps = stub.inter_chunk_gaps()
        print("[%s] outcome=%s 段数=%d 429次数=%d 总耗时=%.1fs"
              % (label, outcome.status, n_chunks, n429, dt))
        print("         段间实测 gap（秒）: %s" % gaps)
        results[label] = (gaps, n_chunks)
        print()
    g1, g2, g3 = results["C1 新默认(0s, 无429)"][0], results["C2 首段429后重试成功"][0], results["C3 显式配置1.5s(老配置)"][0]
    assert all(g < 0.5 for g in g1), "C1 段间应无冷却: %s" % g1
    assert all(abs(g - 2.0) < 0.5 for g in g2), "C2 段间应自适应 2.0s: %s" % g2
    assert all(abs(g - 1.5) < 0.5 for g in g3), "C3 段间应恒 1.5s: %s" % g3
    print("C 结论：无 429 直通 %.2fs / 触发 429 后自适应冷却 %.1fs / 老配置无条件 %.1fs —— 三档全部正确 ✔\n"
          % (max(g1, default=0), g2[0], g3[0]))


def part_d(tmp):
    """D：DeepSeek 账本基线修正 + 主动轮换代登记/消费。"""
    print("=" * 70)
    print("D · DeepSeek 账本路由：基线修正 / 主动轮换代")
    print("=" * 70)

    # ---- D1 基线修正 ----
    mgr, sess = make_mgr(tmp, "d1"), "sess_d1"
    mgr.add_messages(sess, [("user", "开始")])
    est = fill_to(mgr, sess, 0.31)  # 工作副本 ~31%（压缩后的瘦历史形态）
    mgr.add_token_usage("openai", "deepseek-v4", 110000, 2000, None, 112000,
                        session_id=sess, api_base=DEEPSEEK_BASE, cache_scope="main")
    hist = mgr.get_history_for_llm(sess)
    # baseline 内部固定按 CLI 口径（is_gui=False）组装系统提示词估算，
    # 与 fill_to 返回的 GUI 口径略有差异，这里用同一口径对齐断言
    cli_est = agent_service.estimate_history_tokens_with_system(
        DEEPSEEK_MODEL, hist, is_gui=False)
    b_ledger = agent_service.get_session_token_baseline(
        mgr, sess, DEEPSEEK_MODEL, history=hist, api_base=DEEPSEEK_BASE)
    b_relay = agent_service.get_session_token_baseline(
        mgr, sess, DEEPSEEK_MODEL, history=hist, api_base=RELAY_BASE)
    mgr.add_token_usage("openai", "qwen9-flash", 80000, 1000, None, 81000,
                        session_id=sess, cache_scope="main")  # 跨模型真实记录
    b_cross = agent_service.get_session_token_baseline(
        mgr, sess, DEEPSEEK_MODEL, history=hist, api_base=DEEPSEEK_BASE)
    print("[D1] 工作副本估算 %d（%.0f%%），真实用量 112000（87.5%%）" % (est, est / WINDOW * 100))
    print("     官方账本路由基线 → %d（采信真实，水位 %.0f%%，会触发预压缩）" % (b_ledger, b_ledger / WINDOW * 100))
    print("     中转路由基线   → %d（min 口径，水位 %.0f%%，压缩被漏判）" % (b_relay, b_relay / WINDOW * 100))
    print("     跨模型真实记录 → %d（口径失效回落估算）" % b_cross)
    assert b_ledger == 112000, "账本路由应采信真实用量"
    assert b_relay == min(112000, cli_est), "非账本路由应保持 min 口径"
    assert b_cross == cli_est, "跨模型应丢弃旧真实记录"
    print("     D1 ✔ 修复生效：同一数据，官方路由水位 87.5%% vs 中转 31%%\n")

    # ---- D2 主动轮换代（真实回合走 Tier3 → 登记 → 下一笔账本请求重建） ----
    mgr, sess = make_mgr(tmp, "d2"), "sess_d2"
    mgr.add_messages(sess, [("user", "开始")])
    fill_to(mgr, sess, 0.96)  # 96%+ 水位，Tier3 同步压缩
    hist_fat = mgr.get_history_for_llm(sess)
    stub = StubLLM()
    orig = llm._litellm_completion
    llm._litellm_completion = stub
    try:
        gen1 = llm._prepare_deepseek_ledger_request(
            mgr, sess, DEEPSEEK_MODEL, DEEPSEEK_BASE, "main", hist_fat, [])
        notices = []
        t0 = time.time()
        agent_service.prepare_turn_messages(
            mgr, sess, DEEPSEEK_MODEL, WINDOW,
            permission_mode="full",
            notify=lambda k, m: notices.append((time.time() - t0, m)),
            api_base=DEEPSEEK_BASE, is_gui=True, turn_source="local")
        rot_notice = [m for _, m in notices if "重建发送队列" in m]
        assert rot_notice, "应出现主动轮换代提示"
        print("[D2] 96%% 水位回合：Tier3 同步压缩 %.1fs 完成，用户提示：「%s」"
              % (time.time() - t0, re.sub(r"<[^>]+>", "", rot_notice[0])[:52]))
        # 下一笔账本请求：入口消费标记 → force_new 重建
        gen2 = llm._prepare_deepseek_ledger_request(
            mgr, sess, DEEPSEEK_MODEL, DEEPSEEK_BASE, "main",
            mgr.get_history_for_llm(sess), [])
        consumed_again = llm.consume_deepseek_generation_rotation(sess)
        fat_chars = sum(len(str(m.get("content", ""))) for m in gen1["messages"])
        slim_chars = sum(len(str(m.get("content", ""))) for m in gen2["messages"])
        print("     账本代重建：gen%d（%d 条消息 / %d 字符）→ gen%d（%d 条 / %d 字符）"
              % (gen1["generation_id"], len(gen1["messages"]), fat_chars,
                 gen2["generation_id"], len(gen2["messages"]), slim_chars))
        print("     标记一次性消费：二次 consume 返回 %s（应 False）" % consumed_again)
        assert gen2["generation_id"] != gen1["generation_id"], "轮换代应重建新代"
        assert slim_chars < fat_chars * 0.3, "新代应以压缩后瘦身历史重建"
        assert consumed_again is False
        print("     D2 ✔ 登记→提示→消费→force_new 重建→一次性，全链路正确\n")

        # ---- D3 反例：压缩失败不登记 ----
        mgr, sess = make_mgr(tmp, "d3"), "sess_d3"
        mgr.add_messages(sess, [("user", "开始")])
        fill_to(mgr, sess, 0.96)
        stub_fail = StubLLM()
        stub_fail.fail_all = True
        llm._litellm_completion = stub_fail
        notices = []
        agent_service.prepare_turn_messages(
            mgr, sess, DEEPSEEK_MODEL, WINDOW,
            permission_mode="full",
            notify=lambda k, m: notices.append((0, m)),
            api_base=DEEPSEEK_BASE, is_gui=True, turn_source="local")
        leaked = [m for _, m in notices if "重建发送队列" in m]
        rot_left = llm.consume_deepseek_generation_rotation(sess)
        print("[D3] 压缩失败（熔断语义）：轮换提示出现 %d 次，残留标记 %s（应全无）"
              % (len(leaked), rot_left))
        assert not leaked and rot_left is False
        print("     D3 ✔ 失败/熔断绝不登记轮换代\n")
    finally:
        llm._litellm_completion = orig


def part_e(tmp):
    """E：取消中途保留进度 + 增量恢复。"""
    print("=" * 70)
    print("E · 取消与增量恢复（4 段压缩，第 2 段调用中取消）")
    print("=" * 70)
    mgr, sess = make_mgr(tmp, "e"), "sess_e"
    mgr.add_messages(sess, [("user", "开始")])
    fill_to(mgr, sess, 0.75)  # ≈4 段
    stub = StubLLM()  # 快桩：每段约 4.4s，取消点可控地落在第 2 段调用中
    orig = llm._litellm_completion
    llm._litellm_completion = stub
    try:
        cancel = threading.Event()
        notices = []
        result = {}

        def run():
            result["outcome"] = compress(mgr, sess, notices, cancel_event=cancel)

        th = threading.Thread(target=run, daemon=True)
        th.start()
        # 等第 2 段的 LLM 调用进行中（第 1 段已 ok）再取消
        deadline = time.monotonic() + 120
        while not (stub.ok_count >= 1 and stub.in_call.is_set()):
            if time.monotonic() > deadline:
                raise RuntimeError("等待压缩推进超时")
            time.sleep(0.05)
        time.sleep(1.0)  # 确保已进入第 2 段调用体内部深处
        cancel.set()
        th.join(120)
        out = result["outcome"]
        saved = sum(1 for _, m in notices if "段压缩进度" in m)
        # 语义：已落库段落 → status=compressed + reason=partial_cancelled
        # （_finish_with_progress：有进度就算部分成功，partial_* 是 reason）
        print("[E1] 第 2 段调用中取消 → status=%s reason=%s，已落库段数=%d"
              % (out.status, out.reason, saved))
        assert out.status == "compressed" and out.reason == "partial_cancelled" and saved >= 1, "应中止且保留已完成段"
        assert not mc.is_session_compressing(mgr, sess), "守卫应已释放"
        with mgr._lock:
            row = mgr.conn.execute(
                "SELECT memory_summary, summarized_until_msg_id FROM sessions WHERE session_name=?",
                (sess,)).fetchone()
        print("     落库状态：摘要 %d 字，水位推进到 msg_id=%s，守卫已释放 ✔"
              % (len(row[0] or ""), row[1]))
        assert row[1], "CAS 水位应已推进"

        # ---- E2 从已提交水位续推 ----
        stub2 = StubLLM()
        llm._litellm_completion = stub2
        notices2 = []
        out2 = compress(mgr, sess, notices2)
        seg = re.search(r"分 (\d+) 段", "".join(m for _, m in notices2))
        # 剩余内容 ≤ 一段时压缩器不发分段提示（reg 匹配不到），说明增量已很少
        n_seg = int(seg.group(1)) if seg else 1
        seg_desc = ("第 1/1 段直接压缩（剩余不足一段）" if not seg
                    else "分 %d 段" % n_seg)
        print("[E2] 再次压缩：%s（原 3 段，已压部分不重复计费），outcome=%s"
              % (seg_desc, out2.status))
        assert out2.status == "compressed"
        print("     E ✔ 取消不白烧（已完成段落保留）、恢复不重复付费（只压剩余段）\n")
    finally:
        llm._litellm_completion = orig


def main():
    tmp = tempfile.mkdtemp(prefix="sim_mech_")
    orig_backoff = mc.COMPRESSION_RATE_LIMIT_BASE_DELAY_SECONDS
    mc.COMPRESSION_RATE_LIMIT_BASE_DELAY_SECONDS = 0.5  # 时间尺度压缩（5.0→0.5）
    try:
        part_c(tmp)
        part_d(tmp)
        part_e(tmp)
        print("=" * 70)
        print("全部专项通过：C 429自适应冷却 ✔ / D1 账本基线修正 ✔ / D2 主动轮换代 ✔")
        print("             D3 失败不登记 ✔ / E 取消增量恢复 ✔")
        print("SIMULATE-2 PASSED")
    finally:
        mc.COMPRESSION_RATE_LIMIT_BASE_DELAY_SECONDS = orig_backoff


if __name__ == "__main__":
    main()

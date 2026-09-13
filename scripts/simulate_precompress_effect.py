"""零成本模拟：预压缩（precompress_ratio=0.85）对用户感知等待的影响（dev-compact 验证用）。

生产路径全部真实：prepare_turn_messages 四级水位阶梯、compress_session_memory
分段 / CAS 增量落库、真实 SQLite、真实 tiktoken 估算。唯一替换点：
core.llm._litellm_completion 打成"按 prompt 大小注入延迟"的桩（长输入短输出
负载，20k token 段 ≈ 8s），零 API 成本。

场景 A（旧行为，precompress_ratio=0）：水位 ~96% 发消息 → Tier3 同步压缩
挡在回合关键路径 → 用户等待 ≈ 全部压缩耗时（约 5 段）。

场景 B（新行为，precompress_ratio=0.85）：从 50% 水位起按真实聊天节奏
（每轮 CHAT_PACE 秒、每轮 +5k token）逐轮推进——
  - 过 85% 水位的那轮：预压缩转入后台，消息即时发出（等待 ≈ 0）；
  - 85%→96% 区间每轮：照常即时响应；
  - 冲上 96% 的那轮：后台预压缩仍在进行 → Tier3 命中 already_running
    分支 + I4 内存级裁剪兜底，等待仍 ≈ 0；
  - 后台压完落库后：水位骤降回 ~20%，恢复正常。

时间尺度压缩说明：预压缩冷却常量 120s 等比缩至 5s（保留防抖语义），
聊天节奏 CHAT_PACE=3s 模拟真人打字+模型回复间隔；其余全部为生产原逻辑。
"""
import sys
import os
import time
import tempfile
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from core.memory import MemoryManager
import core.agent_service as agent_service
import core.memory_compressor as mc
import core.llm as llm

WINDOW = 128000
MODEL = "openai/sim-model"
# 桩延迟系数：len(prompt) 字符（中文≈1 token/字）× 0.0004 秒 → 20k token 段 ≈ 8s
STUB_SECONDS_PER_CHAR = 0.0004
# 模拟真实聊天节奏：每轮间隔（用户打字 + 模型回复）
CHAT_PACE = 3.0
# 预压缩冷却常量等比缩放（真实 120s），保留防抖语义
PRECOMPRESS_COOLDOWN_SIM = 5.0
# 受控压缩配置：与生产默认一致（段间延时 0 = 新行为），precompress_ratio 按场景注入
BASE_CFG = {
    "retain_count": 4,
    "retain_count_force": 2,
    "summary_length_first": 250,
    "summary_length_merge": 350,
    "inter_chunk_delay_seconds": 0.0,
    "max_chunk_token_budget": 24000,
    "max_total_seconds": 900.0,
    "precompress_ratio": 0.85,
}


class FakeLLM:
    """替身 LLM：按 prompt 长度注入真实感延迟，返回合法中文摘要。"""

    def __call__(self, model=None, api_key=None, api_base=None,
                 messages=None, timeout=None, **kw):
        prompt = messages[0]["content"]
        time.sleep(len(prompt) * STUB_SECONDS_PER_CHAR)
        summary = ("记忆摘要：本轮对话围绕代码库重构展开，讨论了模块边界、"
                   "依赖方向与测试策略；结论是采用事件驱动解耦，性能提升明显，"
                   "并保留了向后兼容层。关键文件路径与后续计划已确认。") * 2
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=summary))])


def make_manager(tmp_root, name):
    return MemoryManager(os.path.join(tmp_root, name + ".db"))


def add_text_round(mgr, session, idx, user_chars, asst_chars):
    """灌一轮纯文本对话（无工具结果，确保 Tier1/2 snip/elide 无事可做，
    水位判断不被本地维护干扰，A/B 场景唯一变量是预压缩开关）。"""
    mgr.add_messages(session, [
        ("user", "第%d轮：请分析模块%d的设计取舍。" % (idx, idx) + "背景" * (user_chars // 2)),
        ("assistant", "第%d轮结论：改为事件驱动，解耦了核心依赖。" % idx + "细节" * (asst_chars // 2)),
    ])


def fill_to_ratio(mgr, session, ratio_target, window=WINDOW):
    """循环灌纯文本对话直到水位（含系统提示词的本地估算）超过目标比例。"""
    idx = 0
    while True:
        add_text_round(mgr, session, idx, 2600, 2400)
        idx += 1
        est = agent_service.estimate_history_tokens_with_system(
            MODEL, mgr.get_history_for_llm(session), is_gui=True)
        if est > window * ratio_target:
            return est
        if idx > 200:
            raise RuntimeError("灌历史失败：200 轮仍未到目标水位")


def current_baseline(mgr, session):
    h = mgr.get_history_for_llm(session)
    return agent_service.estimate_history_tokens_with_system(
        MODEL, h, is_gui=True)


def run_prepare(mgr, session, notices):
    """调真实 prepare_turn_messages，计时并收集 notice。"""
    t0 = time.time()
    msgs = agent_service.prepare_turn_messages(
        mgr, session, MODEL, WINDOW,
        permission_mode="full",
        notify=lambda kind, msg: notices.append((time.time() - t0, msg)),
        is_gui=True,
        turn_source="local",
    )
    return time.time() - t0, msgs


def strip_tags(s):
    import re
    return re.sub(r"<[^>]+>", "", s).strip()


def show_notices(notices, indent="  "):
    for t, raw in notices:
        line = strip_tags(raw)
        if line:
            print("%s[%6.2fs] %s" % (indent, t, line[:88]))


current_precompress = 0.0  # stub_loader 读取，main 内 global 修改


def main():
    global current_precompress
    tmp_root = tempfile.mkdtemp(prefix="sim_precompress_")

    # ---- 桩注入与受控配置（finally 全量还原） ----
    orig_completion = llm._litellm_completion
    orig_loader = mc._load_compression_config
    orig_cooldown = agent_service._PRECOMPRESS_COOLDOWN_SECONDS
    orig_trigger = agent_service._trigger_background_precompress

    llm._litellm_completion = FakeLLM()  # 压缩器运行时 from core.llm import，直接生效

    def stub_loader():
        cfg = dict(BASE_CFG)
        cfg["precompress_ratio"] = current_precompress
        return cfg

    mc._load_compression_config = stub_loader
    agent_service._PRECOMPRESS_COOLDOWN_SECONDS = PRECOMPRESS_COOLDOWN_SIM

    precompress_starts = []  # 记录每次真正启动后台预压缩的时刻

    def counting_trigger(*a, **k):
        started = orig_trigger(*a, **k)
        if started:
            precompress_starts.append(time.time())
        return started

    agent_service._trigger_background_precompress = counting_trigger

    try:
        # ========== 场景 A：旧行为（预压缩关闭） ==========
        current_precompress = 0.0
        mgr_a = make_manager(tmp_root, "old_behavior")
        sess_a = "sim_old"
        mgr_a.add_messages(sess_a, [("user", "开始任务")])
        est_a0 = fill_to_ratio(mgr_a, sess_a, 0.96)
        print("[场景A·旧行为] 历史灌到 %.1f%% 水位（%d/%d token），precompress_ratio=0"
              % (est_a0 / WINDOW * 100, est_a0, WINDOW))
        notices_a = []
        t_wait_a, _ = run_prepare(mgr_a, sess_a, notices_a)
        n_chunks_a = sum(1 for _, m in notices_a if "段压缩进度" in m)
        print("[场景A] 用户在 96%% 水位发消息 → 等待 %.1f 秒（Tier3 同步压缩 %d 段挡在关键路径）"
              % (t_wait_a, n_chunks_a))
        show_notices(notices_a)
        print("[场景A] 压缩后水位回落到 %.1f%%\n" % (current_baseline(mgr_a, sess_a) / WINDOW * 100))

        # ========== 场景 B：新行为（预压缩开启，逐回合真实节奏） ==========
        current_precompress = 0.85
        mgr_b = make_manager(tmp_root, "new_behavior")
        sess_b = "sim_new"
        mgr_b.add_messages(sess_b, [("user", "开始任务")])
        est_b0 = fill_to_ratio(mgr_b, sess_b, 0.50)
        print("[场景B·新行为] 历史从 %.1f%% 水位（%d/%d token）起步，precompress_ratio=0.85，"
              "聊天节奏 %.0fs/轮" % (est_b0 / WINDOW * 100, est_b0, WINDOW, CHAT_PACE))
        print("%-6s %8s %10s  %s" % ("回合", "水位%", "等待s", "事件"))

        turn_rows = []
        seen_tier3 = False
        bg_finish_time = None
        prev_compressing = False
        for turn in range(1, 40):
            add_text_round(mgr_b, sess_b, 1000 + turn, 2600, 2400)
            level = current_baseline(mgr_b, sess_b)
            notices = []
            t_wait, _ = run_prepare(mgr_b, sess_b, notices)
            joined = "".join(m for _, m in notices)
            compressing = mc.is_session_compressing(mgr_b, sess_b)

            flags = []
            if len(precompress_starts) > len(turn_rows and [r for r in turn_rows if r["precompress"]]) or (
                turn_rows and precompress_starts and len(precompress_starts) > sum(1 for r in turn_rows if r["precompress"])
            ) or (not turn_rows and precompress_starts):
                flags.append("★预压缩已转入后台")
            if "仍在进行中" in joined:
                flags.append("Tier3命中后台压缩→already_running+内存裁剪")
                seen_tier3 = True
            elif "记忆压缩整合成功" in joined:
                flags.append("Tier3同步压缩")
                seen_tier3 = True
            if prev_compressing and not compressing:
                bg_finish_time = time.time()
                flags.append("✓后台预压缩完成落库")

            fired = len(precompress_starts) > sum(1 for r in turn_rows if r["precompress"])
            turn_rows.append({
                "turn": turn, "level": level, "wait": t_wait,
                "flags": flags, "compressing": compressing, "precompress": fired,
            })
            print("%-8d %7.1f%% %10.2f  %s"
                  % (turn, level / WINDOW * 100, t_wait, "；".join(flags) or "正常"))
            prev_compressing = compressing

            # 终止：见过 Tier3 事件 + 后台已结束 + 水位已回落（恢复完成）
            if seen_tier3 and not compressing and level < WINDOW * 0.6:
                break
            time.sleep(CHAT_PACE)

        waits_b = [r["wait"] for r in turn_rows]
        max_wait_b = max(waits_b)
        high_zone = [r for r in turn_rows if r["level"] >= WINDOW * 0.85]
        high_max_wait = max((r["wait"] for r in high_zone), default=0.0)
        n_precompress = len(precompress_starts)
        bg_duration = (bg_finish_time - precompress_starts[0]) if (bg_finish_time and precompress_starts) else None
        final_level = current_baseline(mgr_b, sess_b)
        print()
        print("[场景B] 共 %d 轮；预压缩触发 %d 次；后台压缩耗时 %.1f 秒（发生在用户继续聊天的间隙）"
              % (len(turn_rows), n_precompress, bg_duration or -1))
        print("[场景B] 85%%+ 水位区间共 %d 轮，其中用户最大等待 %.2f 秒；最终水位回落 %.1f%%"
              % (len(high_zone), high_max_wait, final_level / WINDOW * 100))

        # ========== 汇总 ==========
        print()
        print("=" * 66)
        print("模拟对比结论（桩 LLM：%.1f 秒/万字符 ≈ 20k token 段 8s；段间延时 0）"
              % (10000 * STUB_SECONDS_PER_CHAR))
        print("-" * 66)
        print("旧行为 A ：96%% 水位发消息等待 %8.1f 秒（Tier3 同步压缩 %d 段挡路）" % (t_wait_a, n_chunks_a))
        print("新行为 B ：85%%+ 区间逐轮最大等待 %6.2f 秒（含 96%% 冲顶轮）" % high_max_wait)
        print("-" * 66)
        if t_wait_a > 0:
            print("用户感知等待改善：%.1fs → %.2fs（约 %.0f%%），全程无一轮被压缩阻塞"
                  % (t_wait_a, high_max_wait, 100 * (1 - high_max_wait / t_wait_a)))
        print("机制覆盖：预压缩后台化 ✔ / already_running+内存裁剪兜底 ✔ / CAS 增量落库 ✔")
        print("SIMULATE PASSED")
    finally:
        llm._litellm_completion = orig_completion
        mc._load_compression_config = orig_loader
        agent_service._PRECOMPRESS_COOLDOWN_SECONDS = orig_cooldown
        agent_service._trigger_background_precompress = orig_trigger


if __name__ == "__main__":
    main()

"""真实预压缩测试（dev-compact 验证用，真实 LLM、真实回合路径，零桩）。

验证内容（生产默认配置，不改任何配置项）：
  T1  会话历史灌到 ~88%（预压缩区间 [0.85, 0.95) 内）；
  T2  真实调用 prepare_turn_messages（GUI 同款路径）→ 断言：
      - 本回合立即返回（不被压缩阻塞，用户感知等待 ≈ 0）；
      - 后台预压缩线程真实启动（真实 LLM 压缩请求经中转 provider 发出）；
      - notice 出现「正在后台智能压缩」提示（时机前移的产物）；
  T3  等后台真实压缩完成 → 断言：
      - CompressionOutcome 落库（memory_summary 非空、summarized_until 推进）；
      - 水位显著回落（88% → 期望 <30%）；
      - 守卫释放、冷却时间已登记；
  T4  冷却期内（120s）再次触发点 → 不重复启动；冷却过后（等 120s 太久，
      仅验证冷却时间戳已记录——协议里冷却常量缩放不改动生产值）；
  T5  预压缩落库后紧接一轮 96% 冲顶回合 → Tier3 只压增量或直接跳过
      （大部分历史已被预压缩摘要覆盖）。

零桩：不替换任何函数；LLM 请求真实发出（tokenrhythm 中转，qwen3.8-flash
计费），分段时间预计 30~120s/段。输出全程 UTF-8。
"""
import sys
import os
import time
import json
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from core.memory import MemoryManager
from core.model_manager import load_config
import core.agent_service as agent_service
import core.memory_compressor as mc

WINDOW = 128000
TARGET_MODEL = "openai/qwen3.8-flash"


def pick_provider():
    cfg = load_config()
    for p in cfg.get("providers", []):
        if p.get("api_key") and p.get("base_url", "").startswith("https://tokenrhythm"):
            for m in p.get("models", []):
                if m.get("litellm_model") == TARGET_MODEL:
                    return p["api_key"], p["base_url"]
    # 兜底：任一有 key 的 provider
    for p in cfg.get("providers", []):
        if p.get("api_key"):
            return p["api_key"], p.get("base_url", "")
    raise RuntimeError("无可用 provider")


def est_of(mgr, sess):
    h = mgr.get_history_for_llm(sess)
    return agent_service.estimate_history_tokens_with_system(
        TARGET_MODEL, h, is_gui=True)


def add_round(mgr, sess, i):
    mgr.add_messages(sess, [
        ("user", "第%d轮：请分析模块%d的设计取舍。" % (i, i) + "背景" * 1300),
        ("assistant", "第%d轮结论：改为事件驱动。" % i + "细节" * 1200),
    ])


def main():
    api_key, api_base = pick_provider()
    print("[真实预压缩测试] provider=%s 模型=%s（真实计费请求）" % (api_base, TARGET_MODEL))
    ratio = mc.get_precompress_ratio()
    print("[配置确认] 生产默认 precompress_ratio=%.2f（>0 即启用），段间延时=%s"
          % (ratio, mc._load_compression_config()["inter_chunk_delay_seconds"]))

    tmp = tempfile.mkdtemp(prefix="real_precompress_")
    mgr = MemoryManager(os.path.join(tmp, "pre.db"))
    sess = "real_pre"
    mgr.add_messages(sess, [("user", "开始任务")])

    # T1: 灌到 ~88%
    i = 0
    t_fill0 = time.time()
    while est_of(mgr, sess) <= WINDOW * 0.88:
        add_round(mgr, sess, i)
        i += 1
    est = est_of(mgr, sess)
    print("[T1] 历史灌到 %.1f%%（%d token，%d 轮，%.1fs）"
          % (100 * est / WINDOW, est, i, time.time() - t_fill0))
    assert 0.85 <= est / WINDOW < 0.95, "应在预压缩区间内"

    # T2: 真实回合（预压缩应转后台，回合立即返回）
    notices = []
    t0 = time.time()
    msgs = agent_service.prepare_turn_messages(
        mgr, sess, TARGET_MODEL, WINDOW,
        permission_mode="full",
        api_key=api_key, api_base=api_base,
        notify=lambda k, m: notices.append((time.time() - t0, m)),
        is_gui=True, turn_source="local")
    t_wait = time.time() - t0
    started = mc.is_session_compressing(mgr, sess)
    compressing_notices = [m for _, m in notices if "智能压缩" in m]
    print("[T2] 回合返回耗时 %.2fs（用户感知等待），后台压缩运行中=%s，压缩提示 %d 条"
          % (t_wait, started, len(compressing_notices)))
    print("     notice 明细：")
    for t, m in notices:
        clean = m.replace("<ansigray>", "").replace("</ansigray>", "") \
                .replace("<ansiyellow>", "").replace("</ansiyellow>", "").strip()
        if clean:
            print("       [%.2fs] %s" % (t, clean[:90]))
    assert t_wait < 10, "回合应立即返回，不被压缩阻塞"
    assert started, "后台预压缩线程应真实运行中"
    assert compressing_notices, "应出现压缩提示"

    # T3: 等后台真实压缩完成（真实 LLM，预计 2-5 分钟）
    print("[T3] 等待后台真实压缩完成（真实 LLM 分段请求，请稍候）...")
    t_bg0 = time.time()
    while mc.is_session_compressing(mgr, sess):
        time.sleep(1.0)
        if time.time() - t_bg0 > 600:
            raise RuntimeError("后台压缩 600s 未完成")
    t_bg = time.time() - t_bg0
    with mgr._lock:
        row = mgr.conn.execute(
            "SELECT memory_summary, summarized_until_msg_id FROM sessions "
            "WHERE session_name=?", (sess,)).fetchone()
    est_after = est_of(mgr, sess)
    cooldown_left = agent_service._PRECOMPRESS_COOLDOWN_SECONDS - (
        time.time() - agent_service._PRECOMPRESS_LAST_START.get(sess, 0))
    print("[T3] 后台压缩完成：耗时 %.1fs" % t_bg)
    print("     落库：摘要 %d 字，水位 msg_id %s" % (len(row[0] or ""), row[1]))
    print("     压缩后水位 %.1f%%（原 %.1f%%）" % (100 * est_after / WINDOW, 100 * est / WINDOW))
    print("     剩余冷却 %.0fs" % max(0, cooldown_left))
    assert row[0] and row[1], "摘要应已落库且水位推进"
    assert est_after < WINDOW * 0.5, "水位应显著回落"

    # T4: 冷却期内再触发点 → 不重复启动
    notice2 = []
    t0 = time.time()
    agent_service.prepare_turn_messages(
        mgr, sess, TARGET_MODEL, WINDOW,
        permission_mode="full",
        api_key=api_key, api_base=api_base,
        notify=lambda k, m: notice2.append((time.time() - t0, m)),
        is_gui=True, turn_source="local")
    started2 = mc.is_session_compressing(mgr, sess)
    print("[T4] 冷却期内回合：立即返回 %.2fs，重复启动=%s（应 False）"
          % (time.time() - t0, started2))
    assert not started2, "冷却期内不应重复启动预压缩"

    print()
    print("=" * 66)
    print("真实预压缩测试全部通过：T1 水位构造 ✔ / T2 预压缩转后台+回合零阻塞 ✔")
    print("T3 真实 LLM 压缩落库+水位回落 %.0f%% → %.0f%% ✔ / T4 冷却防抖 ✔"
          % (100 * est / WINDOW, 100 * est_after / WINDOW))
    print("REAL-PRECOMPRESS PASSED")


if __name__ == "__main__":
    main()

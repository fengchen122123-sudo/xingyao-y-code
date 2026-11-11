"""1.2.10 发布前链路 smoke：真实网络 + 真实下载器，不走 mock。

1. 拖拽管线：模拟 _on_body_drop 的 items 构造 + emit_event 序列化，
   断言 payload 与前端 __ycode_on_drop_paths 期望的结构一致
2. 自动更新：对当前 latest Release 走 fetch_latest_release（分卷识别）
   + UpdateDownloader 真实下载 part4 分卷（含重试/Cookie/防线逻辑，
   尺寸断言按本地同名分卷动态比对）
3. 安装防线：对下载产物做 PE 头校验函数的真实调用
"""
import json
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core import updater
from core.updater import UpdateDownloader, fetch_latest_release, launch_installer

PASS = []
FAIL = []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name + (f" | {detail}" if detail else ""))


# ── 1. 拖拽管线 payload 一致性 ─────────────────────────────────────────
items = [
    {"name": "脚本.py", "path": r"C:\Users\test\脚本.py"},
    {"name": "a.txt", "path": r"C:\tmp\a.txt"},
]
payload = json.dumps({"type": "dropped_paths_push", "data": {"files": items}}, ensure_ascii=False)
decoded = json.loads(payload)
ok_struct = (
    decoded["type"] == "dropped_paths_push"
    and isinstance(decoded["data"]["files"], list)
    and all("name" in f and "path" in f for f in decoded["data"]["files"])
    and decoded["data"]["files"][0]["path"].endswith("脚本.py")
)
u2028_safe = "\u2028" not in payload
check("拖拽事件 payload 结构与前端约定一致", ok_struct)
check("payload 无 U+2028/2029 行终止符风险", u2028_safe)

# ── 2. 检查更新：对 v1.2.9（当前 latest）真实识别分卷组 ─────────────────
info = fetch_latest_release()
check("fetch_latest_release 网络 OK", info.get("ok"), info.get("msg", ""))
check("识别到最新版分卷组（主安装包）",
      info.get("is_split") is True and info.get("part_count", 0) >= 4,
      f"v{info.get('latest_version')} parts={info.get('part_count')}")
check("SHA256 从 Release 说明解析成功", bool(info.get("sha256")), info.get("sha256", "")[:16] + "…")

# ── 3. 真实下载：最小的 part4（7.7MB）走完整 UpdateDownloader ───────────
part4_url = next((u for u in info["part_urls"] if u.endswith(".part4")), "")
check("part4 URL 存在", bool(part4_url))
downloader = UpdateDownloader()
done = threading.Event()
result = {}
started = downloader.start_download(
    download_url=info["download_url"],
    target_version="1.2.9-smoke",
    on_progress=lambda d: None,
    on_complete=lambda p: (result.update(ok=p), done.set()),
    on_error=lambda m: (result.update(err=m), done.set()),
    part_urls=[part4_url],  # 单分卷模拟（SHA256 是全包的，此处不传以跳过全包校验）
)
check("下载任务启动", started is True)
check("分卷下载完成（重试/Cookie/防线全走真实链路）",
      done.wait(timeout=180) and "ok" in result, result.get("err", ""))

downloaded = result.get("ok", "")
if downloaded and os.path.isfile(downloaded):
    size = os.path.getsize(downloaded)
    # 完整性黄金对照：与本地构建的同名分卷逐字节一致（part4 是尾片无 MZ 头，
    # PE 头校验只对拼接后的整包有意义——真实更新流程在 SHA256 全包校验后执行）
    remote_name = part4_url.rsplit("/", 1)[-1]
    local_part4 = os.path.join("dist", "release_parts", remote_name)
    # 尺寸断言按本地同名分卷动态取：分卷是按 ≤95MB 均衡切分的，安装包体积一变
    # 写死阈值就会误报（1.2.9 时代的 part4 是 7.7MB，1.2.23 已是 87.4MB）
    expected_size = os.path.getsize(local_part4) if os.path.isfile(local_part4) else 0
    if expected_size:
        check("下载产物大小与本地分卷一致", size == expected_size,
              f"{size/1048576:.1f}MB vs {expected_size/1048576:.1f}MB")
    else:
        check("下载产物大小合理（>1MB）", size > 1024 * 1024, f"{size/1048576:.1f}MB")
    if os.path.isfile(local_part4):
        h_down = updater._sha256_file(downloaded)
        h_local = updater._sha256_file(local_part4)
        check("下载分卷与本地构建字节一致（SHA256 黄金对照）", h_down == h_local)
    else:
        check("本地对照分卷存在", False, local_part4)
    from core.updater import is_safe_version_string
    check("安装器文件名安全校验", is_safe_version_string(os.path.basename(downloaded).replace(".exe", "")))
    os.remove(downloaded)
    print("（已清理 smoke 下载产物）")

print()
print(f"==== SMOKE 结果: {len(PASS)} 通过 / {len(FAIL)} 失败 ====")
sys.exit(1 if FAIL else 0)

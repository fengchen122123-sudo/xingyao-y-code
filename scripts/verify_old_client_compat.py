"""发布后校验：低版本客户端能否发现并正确选中当前版本的主安装包。

低版本（1.2.6 起）与当前版本有几处行为差异，是「老用户能不能更新」的关键，
必须逐条用**老版本的真实代码**验证：

  A. 老版本用 /releases/latest 取最新版（新版本改用列表接口），
     Gitee 的 latest 指针不会因 API 建 Release 自动刷新，需要发布侧 PATCH 触碰。
  B. 选包有两条路径：附件按分卷上传时走 _collect_split_parts +
     _pick_main_installer_group；否则回退 _pick_main_installer_exe 直接挑整包 exe。
     当前 SPLIT_PART_BYTES=95MB、安装包约 60MB，实际走整包兜底路径，
     因此两种形态都要校验，不能只看分卷组。
  C. 老版本的 _pick_main_installer_group 只认 Y-code-{版本}.exe 前缀，
     认不出新的 XingYaoCode-{版本}.exe，会兜底「取分卷组字典里的第一组」——
     因此主安装包必须排在 CLI 组之前。

校验项：
  1. /releases/latest 与列表接口首位都指向 v{version}
  2. Release 描述含 SHA256 校验行（分卷拼接后强校验依赖它）
  3. 附件顺序：主安装包（分卷组或整包 exe）排在 CLI 组之前
  4. 对每个老版本 tag，用其真实 updater 代码对真实附件做选包判定：
     分卷形态须选中主安装包组且分卷直链齐全；整包形态须选中主安装包 exe
     且直链可用；版本比较判定为「有更新」

用法：venv\\Scripts\\python.exe scripts\\verify_old_client_compat.py
退出码 0 = 全部通过。
"""
import importlib.util
import json
import os
import re
import subprocess
import sys
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_DIR)

GITEE_REPO = "feng-chenhao/xingyao-y-code"
API_BASE = f"https://gitee.com/api/v5/repos/{GITEE_REPO}"
# 覆盖分卷机制上线以来各代老客户端（1.2.6 最早带分卷支持）+ 仍在用的 1.3.x
OLD_TAGS = ["v1.2.6", "v1.2.10", "v1.2.15", "v1.2.20", "v1.2.24", "v1.2.28",
            "v1.3.0", "v1.3.9"]
CACHE_DIR = os.path.join(PROJECT_DIR, "build", "_old_updater_cache")

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name + (f" | {detail}" if detail else ""))


def read_version():
    path = os.path.join(PROJECT_DIR, "core", "version.py")
    with open(path, encoding="utf-8") as f:
        m = re.search(r'__version__\s*=\s*["\']([^"\']+)', f.read())
    return m.group(1) if m else ""


def api(path, timeout=20):
    req = urllib.request.Request(
        f"{API_BASE}{path}",
        headers={"User-Agent": "ycode-release-verify", "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def load_old_updater(old_tag):
    """从 git 取出该版本的真实 core/updater.py 并作为模块加载。"""
    proc = subprocess.run(
        ["git", "show", f"{old_tag}:core/updater.py"],
        cwd=PROJECT_DIR, capture_output=True,
    )
    if proc.returncode != 0:
        return None, proc.stderr.decode("utf-8", "replace").strip()
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, f"updater_{old_tag.replace('.', '_')}.py")
    with open(path, "w", encoding="utf-8") as f:
        f.write(proc.stdout.decode("utf-8"))
    spec = importlib.util.spec_from_file_location(f"old_updater_{old_tag}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod, ""


def main():
    version = read_version()
    if not version:
        print("FAIL 无法从 core/version.py 读取版本号")
        sys.exit(1)
    tag = f"v{version}"
    print(f"== 低版本客户端兼容性校验 (目标 {tag}) ==")

    # 1. 两种接口都要指向新版本
    try:
        latest = api("/releases/latest")
    except Exception as e:
        print(f"FAIL 读取 /releases/latest 失败: {e}")
        sys.exit(1)
    check(f"/releases/latest 指向 {tag}（1.2.6~1.2.27 走这个接口）",
          latest.get("tag_name") == tag, f"实际 {latest.get('tag_name')}")

    try:
        listing = api("/releases?per_page=5&direction=desc")
    except Exception as e:
        print(f"FAIL 读取 releases 列表失败: {e}")
        sys.exit(1)
    first = next(
        (r for r in listing
         if isinstance(r, dict) and not r.get("prerelease") and str(r.get("tag_name") or "").strip()),
        None,
    )
    check("列表接口首位为最新版（当前版本客户端走这个接口）",
          bool(first) and first.get("tag_name") == tag,
          f"实际 {first.get('tag_name') if first else 'None'}")

    body = str(latest.get("body") or "")
    m = re.search(r"SHA256\s*[:：]\s*([0-9a-fA-F]{64})", body)
    check("Release 描述含 SHA256 校验行", bool(m), (m.group(1)[:16] + "…") if m else "缺失")

    assets = latest.get("assets") or []
    names = [str(a.get("name") or "") for a in assets]
    print(f"ℹ Release 附件 {len(names)} 个: {names}")

    # 2. 附件顺序：主安装包必须排在 CLI 之前。
    #    分卷形态看分卷组首组（老版本兜底取字典第一组）；整包形态看 exe 列表
    #    （老版本兜底取第一个 exe）。当前安装包约 60MB < 95MB 阈值，实际是整包。
    stems = []
    for name in names:
        if re.search(r"\.part\d+$", name):
            stem = re.sub(r"\.part\d+$", "", name)
            if stem not in stems:
                stems.append(stem)
    if stems:
        main_first = (
            "cli" not in stems[0].lower()
            and stems[0].lower().startswith(("xingyaocode", "y-code"))
        )
        check("主安装包分卷组先于 CLI 组上传（老版本兜底取第一组）",
              main_first, f"顺序={stems}")
    else:
        exe_order = [n for n in names if n.lower().endswith(".exe")]
        main_idx = next(
            (i for i, n in enumerate(exe_order)
             if "cli" not in n.lower()
             and n.lower().startswith(("xingyaocode", "y-code"))),
            None,
        )
        cli_idx = next(
            (i for i, n in enumerate(exe_order) if "cli" in n.lower()), None
        )
        check("主安装包先于 CLI 安装包上传（老版本兜底取第一个 exe）",
              main_idx is not None and (cli_idx is None or main_idx < cli_idx),
              f"exe 顺序={exe_order}")

    # 3. 用各老版本真实代码做选包判定：分卷形态优先，整包形态走 exe 兜底
    for old_tag in OLD_TAGS:
        old_ver = old_tag.lstrip("vV")
        try:
            mod, err = load_old_updater(old_tag)
        except Exception as e:
            mod, err = None, str(e)
        if mod is None:
            check(f"{old_tag} 加载其 updater 源码", False, err)
            continue

        complete = {}
        if hasattr(mod, "_collect_split_parts"):
            complete = mod._collect_split_parts(assets)
        picked = None
        if complete and hasattr(mod, "_pick_main_installer_group"):
            picked = mod._pick_main_installer_group(complete, version)

        if picked:
            stem, parts = picked
            ok_main = ("cli" not in stem.lower()
                       and bool(re.match(r"(?i)(xingyaocode|y-code)", stem)))
            check(f"{old_tag} 选包 -> 主安装包组", ok_main,
                  f"选中 {stem}（{len(parts)} 片）")
            urls_ok = all(
                str(p.get("browser_download_url") or "").startswith("http")
                for p in parts
            )
            check(f"{old_tag} 分卷直链齐全", urls_ok, f"{len(parts)} 片")
        elif not hasattr(mod, "_pick_main_installer_exe"):
            check(f"{old_tag} 选包 -> 主安装包", False,
                  "既无完整分卷组，也没有整包兜底函数")
        else:
            try:
                asset = mod._pick_main_installer_exe(assets, version)
                call_err = ""
            except Exception as e:
                asset, call_err = None, f"调用异常 {type(e).__name__}: {e}"
            picked_name = str((asset or {}).get("name") or "")
            ok_main = bool(re.match(r"(?i)(xingyaocode|y-code)", picked_name)) \
                and "cli" not in picked_name.lower()
            check(f"{old_tag} 选包 -> 主安装包（整包兜底）", ok_main,
                  call_err or f"选中 {picked_name or '无'}")
            url = str((asset or {}).get("browser_download_url") or "")
            check(f"{old_tag} 主安装包直链可用", url.startswith("http"),
                  url[:80] or "缺下载地址")

        check(f"{old_tag} 版本比较判定为「有更新」",
              mod.compare_versions(version, old_ver) > 0,
              f"{old_ver} -> {version}")

    print()
    print(f"==== 兼容性校验结果: {len(PASS)} 通过 / {len(FAIL)} 失败 ====")
    for name in FAIL:
        print(f"  ✗ {name}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()

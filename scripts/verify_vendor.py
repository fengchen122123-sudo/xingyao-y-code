"""scripts/verify_vendor.py
==========================
vendor/ 随包第三方资源（ripgrep / mermaid 等）的来源与完整性校验脚本。

背景（系统审查报告-2026-10-10 B8-03）：vendor/ 下的二进制与前端资源此前
既无来源记录、也无哈希校验——供应链攻击面与不可复现构建两个问题并存。
本脚本配合 vendor/VENDOR_MANIFEST.json（来源 URL + 版本 + SHA256 记录）把关：

  1. 记录文件必须存在且 JSON 结构完整；
  2. 记录中的每个文件必须真实存在；
  3. 实际文件哈希必须与记录一致（不一致 = 篡改/漂移，直接失败）；
  4. vendor/ 下不允许出现记录之外的文件（新增资源必须先登记来源再入库）。

hash_mode 说明：
  - binary  ：逐字节 SHA256（二进制文件）；
  - text_lf ：先做 CRLF→LF 归一化再算 SHA256。用于文本类资源（如
    mermaid min.js），规避 git core.autocrlf=true 检出时改变的换行
    风格导致误报；真实的单字节内容篡改仍然会被检出。

用法（仓库根目录）：
  venv\\Scripts\\python.exe scripts/verify_vendor.py                # 校验
  venv\\Scripts\\python.exe scripts/verify_vendor.py --print-hashes # 打印实际哈希（供回填记录）
  venv\\Scripts\\python.exe scripts/verify_vendor.py --root <目录>   # 指定根目录（测试用）

退出码：0 = 全部通过；1 = 存在不一致 / 缺失 / 未登记文件。

约束：仅标准库、不新增依赖；绝不自动改写记录文件——哈希不一致时必须先人工
核查来源与是否存在篡改，确认后才更新 vendor/VENDOR_MANIFEST.json。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys

MANIFEST_REL = "vendor/VENDOR_MANIFEST.json"
VENDOR_REL = "vendor"
_HASH_MODES = ("binary", "text_lf")
_HEX_DIGITS = set("0123456789abcdef")


def _default_root() -> str:
    """默认根目录 = 本脚本所在 scripts/ 的上一级（仓库根）。"""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def hash_file(path: str, mode: str = "binary"):
    """计算文件哈希，返回 (sha256 十六进制, 规范化后字节数)。"""
    with open(path, "rb") as f:
        data = f.read()
    if mode == "text_lf":
        # CRLF→LF 归一化：同一内容在不同 autocrlf 设置下检出也能比对
        data = data.replace(b"\r\n", b"\n")
    return hashlib.sha256(data).hexdigest(), len(data)


def load_manifest(manifest_path: str):
    """读取并粗校验记录文件；返回 (records, problems)。

    records: {相对路径: {"sha256": ..., "hash_mode": ...}}；读取失败时 records 为空。
    """
    problems: list[str] = []
    if not os.path.isfile(manifest_path):
        return {}, [f"记录文件不存在: {manifest_path}"]
    try:
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)
    except (OSError, ValueError) as e:
        return {}, [f"记录文件无法解析: {e}"]
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        return {}, ["记录文件缺少非空的 files 列表"]
    records: dict[str, dict] = {}
    for i, item in enumerate(files):
        if not isinstance(item, dict):
            problems.append(f"files[{i}] 不是对象")
            continue
        rel = str(item.get("path") or "").replace("\\", "/").strip()
        digest = str(item.get("sha256") or "").strip().lower()
        mode = str(item.get("hash_mode") or "binary")
        if not rel:
            problems.append(f"files[{i}] 缺少 path")
            continue
        if not rel.startswith(VENDOR_REL + "/"):
            problems.append(f"{rel}: 记录路径必须在 {VENDOR_REL}/ 目录内")
            continue
        if ".." in rel.split("/"):
            problems.append(f"{rel}: 记录路径不允许包含 ..")
            continue
        if len(digest) != 64 or any(c not in _HEX_DIGITS for c in digest):
            problems.append(f"{rel}: sha256 记录不是合法的 64 位十六进制")
            continue
        if mode not in _HASH_MODES:
            problems.append(
                f"{rel}: 未知 hash_mode: {mode}（允许 {' / '.join(_HASH_MODES)}）"
            )
            continue
        if rel in records:
            problems.append(f"{rel}: 记录重复")
            continue
        records[rel] = {"sha256": digest, "hash_mode": mode}
    return records, problems


def _iter_vendor_files(root: str):
    """列出 vendor/ 下全部实际文件（相对路径，"\\" 已归一化为 "/"）。

    含子目录、含隐藏文件；不含记录文件自身（无法自哈希）。
    """
    vendor_dir = os.path.join(root, *VENDOR_REL.split("/"))
    found: list[str] = []
    if not os.path.isdir(vendor_dir):
        return found
    for dirpath, _dirnames, filenames in os.walk(vendor_dir):
        for name in filenames:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root).replace("\\", "/")
            if rel == MANIFEST_REL:
                continue
            found.append(rel)
    return sorted(found)


def verify_vendor(root: str | None = None, manifest_path: str | None = None,
                  printer=print):
    """核心校验：返回问题列表（空列表 = 通过）。printer 可注入以静音输出。"""
    root = os.path.abspath(root or _default_root())
    manifest_path = manifest_path or os.path.join(root, *MANIFEST_REL.split("/"))
    problems: list[str] = []
    records, load_problems = load_manifest(manifest_path)
    problems.extend(load_problems)
    actual = _iter_vendor_files(root)

    for rel, rec in sorted(records.items()):
        full = os.path.join(root, *rel.split("/"))
        if not os.path.isfile(full):
            problems.append(f"{rel}: 记录存在但文件缺失")
            continue
        try:
            digest, size = hash_file(full, rec["hash_mode"])
        except OSError as e:
            problems.append(f"{rel}: 读取失败 {e}")
            continue
        if digest != rec["sha256"]:
            problems.append(
                f"{rel}: SHA256 不一致（记录 {rec['sha256'][:16]}…，"
                f"实际 {digest[:16]}…，模式 {rec['hash_mode']}）"
            )
        else:
            printer(f"[OK] {rel}  [{rec['hash_mode']}]  {digest[:16]}…  {size} 字节")

    listed = set(records)
    for rel in actual:
        if rel not in listed:
            problems.append(f"{rel}: vendor/ 下的文件未登记来源/哈希（先补记录再入库）")
    return problems


def print_hashes(root: str | None = None) -> int:
    """打印 vendor/ 下每个文件的 binary / text_lf 两种实际哈希，供人工回填记录。"""
    root = os.path.abspath(root or _default_root())
    files = _iter_vendor_files(root)
    if not files:
        print(f"（{root} 下没有 vendor/ 文件）")
        return 0
    for rel in files:
        full = os.path.join(root, *rel.split("/"))
        binary, size_b = hash_file(full, "binary")
        text_lf, size_t = hash_file(full, "text_lf")
        print(f"# {rel}")
        print(f"#   实际字节数: {size_b}")
        print(f"#   sha256(binary) : {binary}")
        print(f"#   sha256(text_lf): {text_lf}  （LF 归一化后 {size_t} 字节）")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="校验 vendor/ 随包资源与 VENDOR_MANIFEST.json 记录的来源/哈希是否一致"
    )
    parser.add_argument("--root", default=None, help="仓库根目录（默认取本脚本上一级）")
    parser.add_argument("--manifest", default=None,
                        help="记录文件路径（默认 vendor/VENDOR_MANIFEST.json）")
    parser.add_argument("--print-hashes", action="store_true",
                        help="只打印 vendor/ 下每个文件的实际哈希（供回填记录），不校验")
    parser.add_argument("--quiet", action="store_true", help="只输出结论")
    args = parser.parse_args(argv)

    root = os.path.abspath(args.root or _default_root())
    if args.print_hashes:
        return print_hashes(root)

    printer = (lambda *a, **k: None) if args.quiet else print
    problems = verify_vendor(root, args.manifest, printer=printer)
    if problems:
        print(f"[FAIL] vendor 资源校验失败（{len(problems)} 项问题）：")
        for p in problems:
            print(f"   - {p}")
        print("   处理：先核查来源是否可信、文件是否被篡改，确认正常后再更新")
        print(f"   记录文件 {MANIFEST_REL} 并说明原因；来源不明时不得直接回填哈希。")
        return 1
    print("[OK] vendor 资源校验通过：记录与实际文件一致。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
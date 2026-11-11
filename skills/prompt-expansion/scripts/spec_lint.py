#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
spec_lint.py — 执行提示词「复现级」自检器

用法：
    python spec_lint.py <文档路径> [--verbose]

输出：结构检查 + 量化密度 + 模糊词 + 验收项统计 + 10 项评分 + 是否通过（门槛 16/20）。
退出码：0 通过 / 1 未通过 / 2 参数或读取错误
"""
import os
import re
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

FUZZY = ["建议", "可以考虑", "视情况", "按需", "酌情", "尽量", "大约", "适当",
         "若干", "等等", "之类的", "差不多", "必要的话", "如有需要", "或者", "大概", "合理"]

SECTION_RULES = {
    "交付形态": [r"交付形态", r"交付物", r"交付清单"],
    "技术规格": [r"技术规格", r"核心参数", r"参数表"],
    "边界处理": [r"边界", r"异常", r"失败处理", r"降级"],
    "验证清单": [r"验证", r"验收"],
    "假设清单": [r"假设"],
}

HARD_GATES = {
    "G3边界": [r"边界", r"异常", r"失败", r"降级", r"冲突"],
    "G4验收": [r"验证", r"验收", r"检查方法"],
    "G5假设": [r"假设"],
}


def read_doc(path):
    for enc in ("utf-8-sig", "utf-8", "gbk"):
        try:
            with open(path, "r", encoding=enc) as f:
                return f.read()
        except UnicodeDecodeError:
            continue
    raise RuntimeError("无法解码文件，请确认是 UTF-8 编码")


def count_chinese(text):
    return len(re.findall(r"[\u4e00-\u9fff]", text))


def analyze(text):
    text = re.sub(chr(96) * 3 + ".*?" + chr(96) * 3, "", text, flags=re.S)
    text = re.sub(r"^\s*>.*$", "", text, flags=re.M)
    cn = count_chinese(text) or 1
    numbers = re.findall(r"\d+(?:\.\d+)?", text)
    num_ratio = len(numbers) / (cn / 100.0)   # 每百中文字的数值数

    fuzzy_hits = {}
    for w in FUZZY:
        n = text.count(w)
        if n:
            fuzzy_hits[w] = n
    fuzzy_total = sum(fuzzy_hits.values())

    checkboxes = re.findall(r"^\s*-\s*\[[ xX]\]\s*(.+)$", text, re.M)
    checkable = [c for c in checkboxes if re.search(r"(→|->|检查|控制台|命令|运行|打开|执行|查看|对比|确认)", c)]

    table_rows = len(re.findall(r"^\s*\|.*\|\s*$", text, re.M))
    sections = {}
    for name, pats in SECTION_RULES.items():
        sections[name] = any(re.search(p, text) for p in pats)

    hyp_rows = 0
    m = re.search(r"##[^\n]*假设[^\n]*\n(.+?)(?=\n##|\Z)", text, re.S)
    if m:
        hyp_rows = len([l for l in m.group(1).splitlines() if l.strip().startswith("|") and "---" not in l])
        hyp_rows = max(0, hyp_rows - 1)
    gate_fail = [g for g, pats in HARD_GATES.items() if not any(re.search(p, text) for p in pats)]
    if any(text.count(w) for w in FUZZY[:6]): gate_fail.append("G1")
    return {
        "cn": cn, "numbers": len(numbers), "num_ratio": num_ratio,
        "fuzzy": fuzzy_hits, "fuzzy_total": fuzzy_total,
        "checkboxes": len(checkboxes), "checkable": len(checkable),
        "table_rows": table_rows, "sections": sections,
        "hyp_rows": hyp_rows, "gate_fail": gate_fail,
    }


def score(a):
    s = {}
    s[1] = 2 if (a["sections"]["交付形态"] and re.search(r"\.(html|py|ps1|js|ts|md|go|java|rs)", a.get("_text", ""))) else (1 if a["sections"]["交付形态"] else 0)
    s[2] = 2 if a["fuzzy_total"] == 0 else (1 if a["fuzzy_total"] <= 2 else 0)
    s[3] = 2 if a["num_ratio"] >= 1.0 else (1 if a["num_ratio"] >= 0.4 else 0)
    s[4] = 2 if a["sections"]["边界处理"] else 0
    s[5] = 2 if a["table_rows"] >= 8 else (1 if a["table_rows"] >= 4 else 0)
    s[6] = 2 if a["checkboxes"] >= 6 and a["checkable"] >= max(4, a["checkboxes"] * 0.6) else (1 if a["checkboxes"] >= 3 else 0)
    s[7] = 2 if a["hyp_rows"] >= 2 else (1 if a["hyp_rows"] >= 1 else 0)
    s[8] = 2 if re.search(r"坑|风险|规避|注意", a.get("_text", "")) else 0
    s[9] = 2 if re.search(r"未验证|如实标注|不得默认通过", a.get("_text", "")) else 0
    s[10] = 2 if (s[1] >= 1 and s[3] >= 1 and s[6] >= 1 and s[7] >= 1) else (1 if s[1] >= 1 else 0)
    return s


NAMES = {1: "交付物唯一确定", 2: "选型唯一(G1)", 3: "量化密度(G2)", 4: "边界覆盖(G3)",
         5: "交互/参数矩阵", 6: "验收可勾选(G4)", 7: "假设显式(G5)", 8: "坑位预防",
         9: "未验证规则", 10: "可移植性"}


def main():
    if len(sys.argv) < 2:
        print("用法: python spec_lint.py <文档路径> [--verbose]")
        return 2
    path = sys.argv[1]
    if not os.path.isfile(path):
        print(f"[错误] 文件不存在: {path}")
        return 2
    text = read_doc(path)
    a = analyze(text)
    a["_text"] = text
    s = score(a)
    total = sum(s.values())

    print(f"== spec_lint: {os.path.basename(path)} ==")
    print(f"中文 {a['cn']} 字 | 数值 {a['numbers']} 个 | 量化密度 {a['num_ratio']:.2f} 个/百字 | 表格行 {a['table_rows']}")
    print("-- 结构 --")
    for k, v in a["sections"].items():
        print(f"  [{'OK' if v else 'MISS'}] {k}")
    print(f"-- 验收项 -- 可勾选 {a['checkboxes']} 条，其中含检查手段 {a['checkable']} 条")
    print(f"-- 假设 -- {a['hyp_rows']} 条")
    if a["fuzzy"]:
        print("-- 模糊词 -- " + "、".join(f"{k}×{v}" for k, v in sorted(a["fuzzy"].items(), key=lambda x: -x[1])))
    else:
        print("-- 模糊词 -- 无")
    if a["gate_fail"]:
        print("-- 硬门禁未过 -- " + "、".join(a["gate_fail"]))
    print("-- 评分 --")
    for i in sorted(s):
        print(f"  {i:2d}. {NAMES[i]:<16} {s[i]}/2")
    passed = total >= 16 and not a["gate_fail"]
    print(f"== 合计 {total}/20（门槛 16） → {'通过' if passed else '未通过'} ==")
    if not passed:
        print("按未达项补齐后重跑；0 分项必须修正，不因总分够而跳过。")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())

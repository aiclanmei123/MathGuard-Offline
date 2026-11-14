#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_math_guard.py —— math_guard 自身的自测（仿照数论工作台 §13.7 纪律）。

设计原则（来自 数论工作台 docs/AGENTS.md §13.7）：
    「一个永远报'通过'的防幻觉工具，本身就是幻觉。」
    因此每个用例都必须有一个**明确的、可被推翻的预期**，
    并且要包含「工具历史上真实犯过的错」作为回归用例。

用法：
    python test_math_guard.py          # 全部用例
    python test_math_guard.py -v       # 显示每条判定

退出码：0 全过 / 1 有用例失败
"""
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import math_guard as m

V = "-v" in sys.argv

# ============================================================
# 用例格式：(描述, LaTeX, 预期状态)
# 状态含义：ok=已验证通过 / error=验证失败 / parsed=仅可解析 / skip=无法验证
# ============================================================
CASES = [
    # ---- A. 真错误必须抓到（否则工具毫无价值）----
    ("A1 加法错误", "2 + 2 = 5", "error"),
    ("A2 分数错误", r"\frac{1}{2}+\frac{1}{3}=\frac{2}{5}", "error"),
    ("A3 乘法错误", r"3 \times 4 = 11", "error"),
    ("A4 除法错误", r"100 \div 4 = 30", "error"),
    ("A5 朴素错误", "1 = 2", "error"),

    # ---- B. 真正确必须通过（否则全是噪音）----
    ("B1 加法正确", "2 + 2 = 4", "ok"),
    ("B2 分数正确", r"\frac{1}{2}+\frac{1}{3}=\frac{5}{6}", "ok"),
    ("B3 幂正确", "2^{10} = 1024", "ok"),
    ("B4 减法正确", "7 - 5 = 2", "ok"),
    ("B5 不等式正确", r"2 \le 3", "ok"),

    # ---- C. 历史上真实误报过的（回归用例，必须不再报错）----
    ("C1 勒让德符号", r"\left(\frac{5}{11}\right)=1", "parsed"),
    ("C2 导数记号", "{}'=d/d" + "\\" + "tau", "parsed"),
    ("C3 不等式续行", r"\le p|f(z)|^{p-1}", "skip"),
    ("C4 幂后跟减号", r"(5\cdot 2^{2}-1)\cdot 1^2 - 2\cdot 3 = 19 - 6 = 13.", "ok"),
    ("C5 同余式", r"a \equiv b \pmod{p}", "parsed"),
    ("C6 表格数据", "64", "parsed"),
    # 2026-11-14 第二轮全盘复测发现的（259 份真实文档）
    ("C7 符号代用 =2e-4", r"=2\times10^{-4}", "skip"),
    ("C8 符号代用 ge5", r"\ge5", "skip"),
    ("C9 符号代用 gt1000", ">1000", "skip"),
    ("C10 符号代用 lt10-6", r"<10^{-6}", "skip"),
    ("C11 悬空关系符", r"\displaystyle\Sigma_{4}(m)\equiv", "skip"),
    ("C12 悬空关系符2", "p^{2}=", "skip"),
    ("C13 渐近符号", r"\sim10^{-14}", "skip"),
    ("C14 渐近符号2", r"\sim0.11^{m}", "skip"),
    ("C15 中文标签不等式", r"\text{教育价值} \ge P_{75}^{edu}", "parsed"),
    ("C16 char 残渣", r"{\CC W{\char34} Rb 5=}", "skip"),
    ("C17 真笔误必须报", "x = ", "error"),
    ("C18 真笔误必须报2", "y \\ge ", "error"),

    # ---- D. 超能力范围必须承认（不能装懂）----
    ("D1 求和", r"\sum_{k=0}^{n} k", "skip"),
    ("D2 纯符号", r"a_n = b_n", "parsed"),
    ("D3 空公式", "", "skip"),
]

# Python 3.13 的 ast.literal_eval 不再支持二元运算——曾导致 "2+2=5" 被判"通过"
REGRESSION_LITERAL_EVAL = [
    ("R1 二元运算求值", "2+2", 4),
    ("R2 减法求值", "10-3", 7),
    ("R3 乘法求值", "6*7", 42),
    ("R4 除法求值", "10/4", 2.5),
    ("R5 乘方求值", "2**10", 1024),
]


def main() -> int:
    passed = failed = 0
    fails = []

    print("=" * 66)
    print("math_guard 自测")
    print("=" * 66)

    print("\n[公式判定]")
    for desc, src, expect in CASES:
        d = m.verify_with_sympy(m.MathItem(kind="display", line=1, latex=src, original=src))
        ok = d.status == expect
        passed += ok
        failed += not ok
        mark = "PASS" if ok else "FAIL"
        print(f"  {mark}  {desc:16} 预期={expect:7} 实际={d.status:7}")
        if V or not ok:
            print(f"        ├ {src[:60]}")
            if d.reason:
                print(f"        └ {d.reason[:70]}")
        if not ok:
            fails.append(f"{desc}: 预期 {expect}，实际 {d.status}")

    print("\n[安全求值]（Python 3.13 literal_eval 不行，必须用自带的）")
    for desc, expr, expect in REGRESSION_LITERAL_EVAL:
        got = m._safe_eval_num(expr)
        ok = got == expect
        passed += ok
        failed += not ok
        print(f"  {'PASS' if ok else 'FAIL'}  {desc:16} {expr:10} -> {got}")
        if not ok:
            fails.append(f"{desc}: 预期 {expect}，实际 {got}")

    print("\n[编码识别]")
    enc_cases = [
        ("UTF-8 中文", "中文测试".encode("utf-8"), "utf-8-sig"),
        ("GBK 中文", "中文测试".encode("gb18030"), "gb18030"),
        ("纯 ASCII", b"hello", "utf-8-sig"),
    ]
    for desc, raw, expect in enc_cases:
        text, enc = m._decode_bytes(raw, "x")
        ok = enc == expect and "中文" in text or (desc == "纯 ASCII" and text == "hello")
        passed += ok
        failed += not ok
        print(f"  {'PASS' if ok else 'FAIL'}  {desc:16} -> {enc}")
        if not ok:
            fails.append(f"{desc}: 编码识别为 {enc}")

    print("\n[宏展开]")
    macros = {"lp": (0, r"\Big(\frac{-1}{p}\Big)"), "uu": (1, "u_{#1}(j)")}
    mcases = [
        ("零参宏", r"\lp b", r"\Big(\frac{-1}{p}\Big) b"),
        ("带参宏", r"\uu{k}", "u_{k}(j)"),
    ]
    for desc, src, expect in mcases:
        got = m.expand_macros(src, macros)
        ok = got == expect
        passed += ok
        failed += not ok
        print(f"  {'PASS' if ok else 'FAIL'}  {desc:16} {src:8} -> {got}")
        if not ok:
            fails.append(f"{desc}: 预期 {expect}，实际 {got}")

    print("\n[PDF 拒绝]")
    tmp = tempfile.mkdtemp()
    pdf = os.path.join(tmp, "x.pdf")
    open(pdf, "wb").write(b"%PDF-1.4\nfake\n")
    r = subprocess.run([sys.executable, os.path.join(HERE, "math_guard.py"), pdf, "--no-color"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    ok = r.returncode == 2 and "不支持 PDF" in r.stdout
    passed += ok
    failed += not ok
    print(f"  {'PASS' if ok else 'FAIL'}  PDF 必须被拒绝（exit=2）-> exit={r.returncode}")
    if not ok:
        fails.append(f"PDF 拒绝失败: exit={r.returncode}")

    print("\n[退出码分级]")
    txt = os.path.join(tmp, "a.tex")
    open(txt, "w", encoding="utf-8").write("\\documentclass{article}\\begin{document}$2+2=5$\\end{document}")
    r = subprocess.run([sys.executable, os.path.join(HERE, "math_guard.py"), txt, "--no-color"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                       env=dict(os.environ, MATH_GUARD_PARSUB_MODE="off"))
    ok = r.returncode == 3
    passed += ok
    failed += not ok
    print(f"  {'PASS' if ok else 'FAIL'}  数学错误 -> exit=3（实际 {r.returncode}）")
    if not ok:
        fails.append(f"退出码分级: 预期 3，实际 {r.returncode}")

    print("\n[语法检查：环境与括号配对]")
    BS = "\\"
    syn_cases = [
        ("正常文档", "x=1", 0),
        ("缺 \\end", BS + "begin{proof} ok", 1),
        ("环境名不符", BS + "begin{equation} x " + BS + "end{align}", 1),
        ("少右花括号", BS + "frac{1}{2", 1),
        ("缺 \\right", BS + "left(x", 1),
        ("正常 \\left\\right", BS + "left(x" + BS + "right)", 0),
        ("转义花括号不算错", BS + "{" + BS + "}", 0),
        ("宏定义里的括号不算错", BS + "def" + BS + "x{" + BS + "left(}", 0),
        ("CJK 壳环境不误报", BS + "begin{CJK*}{GBK}{song} " + BS + "begin{proof}x"
         + BS + "end{proof} " + BS + "end{CJK*}", 0),
    ]
    for desc, body, expect_n in syn_cases:
        text = "\\documentclass{article}\n\\begin{document}\n" + body + "\n\\end{document}\n"
        tmpf = os.path.join(tempfile.gettempdir(), "_mg_syn_test.tex")
        open(tmpf, "w", encoding="utf-8").write(text)
        d = m.read_tex(tmpf)
        errs, _ = m.syntax_check(d)
        # document 壳环境不参与配对检查，所以"缺 end"类用例只统计非 document 的问题
        real = [e for e in errs if "document" not in (e.message or "")]
        ok = len(real) == expect_n
        passed += ok
        failed += not ok
        print(f"  {'PASS' if ok else 'FAIL'}  {desc:20} 预期={expect_n} 实际={len(real)}")
        if not ok:
            fails.append(f"{desc}: 预期 {expect_n} 个语法问题，实际 {len(real)}")
            for e in real[:3]:
                print(f"        L{e.line} {e.message[:60]}")
        try:
            os.remove(tmpf)
        except OSError:
            pass

    print("\n[模块六：公式标点检查]")
    punct_cases = [
        ("有句号", BS + "begin{equation} x=1. " + BS + "end{equation}", 0),
        ("有逗号", BS + "begin{equation} x=1, " + BS + "end{equation}", 0),
        ("中文句号", BS + "begin{equation} x=1" + BS + "text{。} " + BS + "end{equation}", 0),
        ("无标点", BS + "begin{equation} x=1 " + BS + "end{equation}", 1),
        ("带label无标点", BS + "begin{equation}" + BS + "label{e} x=1 " + BS + "end{equation}", 1),
        ("逗号+换行", BS + "begin{equation} x," + BS + BS + " " + BS + "end{equation}", 0),
    ]
    for desc, body, expect_n in punct_cases:
        text = "\\documentclass{article}\n\\begin{document}\n" + body + "\n\\end{document}\n"
        tmpf = os.path.join(tempfile.gettempdir(), "_mg_punct_test.tex")
        open(tmpf, "w", encoding="utf-8").write(text)
        d = m.read_tex(tmpf)
        _, ws = m.syntax_check(d)
        pw = [w for w in ws if w.kind == "punct"]
        ok = len(pw) == expect_n
        passed += ok
        failed += not ok
        print(f"  {'PASS' if ok else 'FAIL'}  {desc:14} 预期={expect_n} 实际={len(pw)}")
        if not ok:
            fails.append(f"{desc}: 预期 {expect_n} 条标点警告，实际 {len(pw)}")
        try:
            os.remove(tmpf)
        except OSError:
            pass

    import shutil
    shutil.rmtree(tmp, ignore_errors=True)

    print()
    print("=" * 66)
    print(f"===== {passed} passed, {failed} failed =====")
    if fails:
        print("\n失败项：")
        for f in fails:
            print("  -", f)
    print("=" * 66)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

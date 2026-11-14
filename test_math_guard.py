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
        _, ws = m.syntax_check(d, lint=True)
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

    print("\n[空参数检查：\\frac{a}{} 之类]")
    empty_cases = [
        ("空分母", BS + "frac{a}{}", 1),
        ("空分子", BS + "frac{}{b}", 1),
        ("都空", BS + "frac{}{}", 2),
        ("正常分数", BS + "frac{a}{b}", 0),
        ("空 sqrt", BS + "sqrt{}", 1),
        ("正常 sqrt", BS + "sqrt{x}", 0),
        ("可选参数不误报", BS + "sqrt[3]{x}", 0),
        ("空 text", BS + "text{}", 1),
        ("占位残稿", BS + "frac{" + BS + "cdot}{}", 2),
        ("正常 cdot", BS + "frac{a" + BS + "cdot b}{c}", 0),
    ]
    for desc, body, expect_n in empty_cases:
        text = ("\\documentclass{article}\n\\begin{document}\n$" + body
                + "$\n\\end{document}\n")
        tmpf = os.path.join(tempfile.gettempdir(), "_mg_empty_test.tex")
        open(tmpf, "w", encoding="utf-8").write(text)
        d = m.read_tex(tmpf)
        es, _ = m.syntax_check(d)
        real = [x for x in es if x.kind in ("empty-arg", "placeholder-arg")]
        ok = len(real) == expect_n
        passed += ok
        failed += not ok
        print(f"  {'PASS' if ok else 'FAIL'}  {desc:16} 预期={expect_n} 实际={len(real)}")
        if not ok:
            fails.append(f"{desc}: 预期 {expect_n} 处，实际 {len(real)}")
        try:
            os.remove(tmpf)
        except OSError:
            pass

    print("\n[实战回归：Apery 论文定稿 PDF 里真实印出来的空分母]")
    # 这一段是从 Apery_midpoint_congruences_final.pdf 第 38 页公式 (7.16) 抄回来的。
    # 该 PDF 已定稿，但公式右侧的分式分母是空的，渲染出来是一道空横线。
    # 原始 LaTeX（sec7_Sn.tex:145）：
    #   {}_{2}F_{1}\Bigl(\hf,\hf;1;4e^{x}(1-e^{x})\Bigr)
    #     =(2e^{x}-1)^{-1}{}_{2}F_{1}\Bigl(\hf,\hf;1;\frac{-(e^{-x}-1)^{2}}{}\Bigr)
    real_world = [
        ("PDF 第38页空分母",
         "{}_{2}F_{1}" + BS + "Bigl(" + BS + "hf," + BS + "hf;1;4e^{x}(1-e^{x})" + BS + "Bigr)"
         "=(2e^{x}-1)^{-1}{}_{2}F_{1}" + BS + "Bigl(" + BS + "hf," + BS + "hf;1;"
         + BS + "frac{-(e^{-x}-1)^{2}}{}" + BS + "Bigr)", 1),
        ("sec5_exp 空二项式", BS + "binom{}{}=0", 2),
        ("sec9_tower 占位残骸", BS + "frac{" + BS + "cdot}{}", 2),
        ("zagier 占位残骸", BS + "int" + BS + "frac{" + BS + "cdot}{1-x}", 1),
        ("zagier 待填草稿", BS + "frac{" + BS + "ldots}{m-a}", 1),
    ]
    for desc, body, expect_n in real_world:
        text = "\\documentclass{article}\n\\begin{document}\n$" + body + "$\n\\end{document}\n"
        tmpf = os.path.join(tempfile.gettempdir(), "_mg_real_test.tex")
        open(tmpf, "w", encoding="utf-8").write(text)
        d = m.read_tex(tmpf)
        es, _ = m.syntax_check(d)
        real = [x for x in es if x.kind in ("empty-arg", "placeholder-arg")]
        ok = len(real) == expect_n
        passed += ok
        failed += not ok
        print(f"  {'PASS' if ok else 'FAIL'}  {desc:20} 预期={expect_n} 实际={len(real)}")
        if not ok:
            fails.append(f"{desc}: 预期 {expect_n}，实际 {len(real)}")
        try:
            os.remove(tmpf)
        except OSError:
            pass

    print("\n[\\def 宏与单 token 参数（arXiv:2408.09776 实战）]")
    BS2 = "\\"
    def_src = (
        BS2 + "def" + BS2 + "f{" + BS2 + "frac}\n"
        + BS2 + "def" + BS2 + "sls#1#2{(" + BS2 + "f{#1}{#2})}\n"
        + BS2 + "def" + BS2 + "qp#1{q_p(#1)}\n"
    )
    mac = m.collect_macros(def_src)
    macro_cases = [
        ("收集 \\def 宏", len(mac) == 3),
        ("收集到 f", "f" in mac and mac["f"] == (0, BS2 + "frac")),
        ("收集到 sls(2参)", mac.get("sls", (0, ""))[0] == 2),
        ("无参宏展开", m.expand_macros(BS2 + "f 1{256}", mac) == BS2 + "frac 1{256}"),
        ("单 token 参数", m.expand_macros(BS2 + "sls{1+" + BS2 + "sqrt{-7}}2^{24}", mac)
         == "(" + BS2 + "frac{1+" + BS2 + "sqrt{-7}}{2})^{24}"),
        ("花括号参数", m.expand_macros(BS2 + "qp{2}", mac) == "q_p(2)"),
    ]
    for desc, ok in macro_cases:
        passed += ok
        failed += not ok
        print(f"  {'PASS' if ok else 'FAIL'}  {desc:18}")
        if not ok:
            fails.append(f"宏用例失败：{desc}")

    print("\n[跨领域灰度测试暴露的缺陷（arXiv 几何/PDE）]")
    BS3 = "\\"
    # 缺陷 A：\varnothing / \emptyset 曾被映射成空串
    #   arXiv:2611.15157 `TB_p(r)\cap\Sigma=\varnothing`
    #   → sanitize 成 `A=` → 误判"关系符缺操作数"
    # 缺陷 B：check_environments 没剥离注释
    #   arXiv:2611.15796 注释里有 `%\begin{equation}` 和 `%end{equation}`（缺反斜杠）
    #   → 误报"环境嵌套错序"
    cross_cases = [
        ("varnothing 不误报", "A=" + BS3 + "varnothing", "parsed"),
        ("emptyset 不误报", "A=" + BS3 + "emptyset", "parsed"),
        ("cap 与空集", "TB_p(r)" + BS3 + "cap" + BS3 + "Sigma=" + BS3 + "varnothing", "parsed"),
    ]
    for desc, latex, want in cross_cases:
        d = m.verify_with_sympy(m.MathItem(kind="inline", line=1, latex=latex, original=latex))
        ok = d.status == want
        passed += ok
        failed += not ok
        print(f"  {'PASS' if ok else 'FAIL'}  {desc:18} 预期={want} 实际={d.status}")
        if not ok:
            fails.append(f"{desc}: 预期 {want}，实际 {d.status}")

    # 注释里的环境不应参与配对检查
    comment_tex = (
        "\\documentclass{article}\n\\begin{document}\n"
        "\\begin{proposition}\n"
        "文本\n"
        "%\\begin{equation}\\label{eq:x}\n"
        "%   x = 1\n"
        "%end{equation}\n"
        "\\end{proposition}\n"
        "\\end{document}\n"
    )
    tmpc = os.path.join(tempfile.gettempdir(), "_mg_comment_test.tex")
    open(tmpc, "w", encoding="utf-8").write(comment_tex)
    dc = m.read_tex(tmpc)
    ec, _ = m.syntax_check(dc)
    realc = [e for e in ec if "document" not in (e.message or "")]
    ok = len(realc) == 0
    passed += ok
    failed += not ok
    print(f"  {'PASS' if ok else 'FAIL'}  {'注释里的环境不误报':16} 预期=0 实际={len(realc)}")
    if not ok:
        fails.append(f"注释环境: 预期 0，实际 {len(realc)}")
        for e in realc[:2]:
            print(f"        L{e.line} {e.message[:60]}")
    try:
        os.remove(tmpc)
    except OSError:
        pass

    print("\n[概率论论文暴露的缺陷（arXiv math.PR）]")
    BS4 = "\\"
    # 缺陷 C：字体包装 \mathbb{X} 曾把参数内容丢掉
    #   arXiv:2611.16739 `\mathbb F=\mathbb R` → `F=` → 误报缺操作数
    # 缺陷 D：\providecommand{\C}{} 与 \renewcommand{\C}{\mathbb{C}} 并存时，
    #   "先定义先得"保留了空定义
    # 缺陷 E：\overset{d}{=}（依分布相等）作为独立公式被误判
    font_cases = [
        ("mathbb 单 token", BS4 + "mathbb F=" + BS4 + "mathbb R", "parsed"),
        ("mathbb 花括号", BS4 + "mathbb{F}=" + BS4 + "mathbb{C}", "parsed"),
        ("mathcal 单 token", BS4 + "mathcal M", "parsed"),
        ("overset 关系符", BS4 + "overset{d}{=}", "skip"),
        ("stackrel 关系符", BS4 + "stackrel{d}{=}", "skip"),
    ]
    for desc, latex, want in font_cases:
        d = m.verify_with_sympy(m.MathItem(kind="inline", line=1, latex=latex, original=latex))
        ok = d.status == want
        passed += ok
        failed += not ok
        print(f"  {'PASS' if ok else 'FAIL'}  {desc:18} 预期={want} 实际={d.status}")
        if not ok:
            fails.append(f"{desc}: 预期 {want}，实际 {d.status}")

    # renewcommand 必须覆盖 providecommand 的空定义
    combo = (BS4 + "providecommand{" + BS4 + "C}{}\n"
             + BS4 + "renewcommand{" + BS4 + "C}{" + BS4 + "mathbb{C}}")
    mac2 = m.collect_macros(combo)
    ok = mac2.get("C") == (0, BS4 + "mathbb{C}")
    passed += ok
    failed += not ok
    print(f"  {'PASS' if ok else 'FAIL'}  {'renewcommand 覆盖':18} {'OK' if ok else mac2}")
    if not ok:
        fails.append(f"renewcommand 覆盖失败: {mac2.get('C')}")

    print("\n[中文 LaTeX 边界（ctexart，226 份中文文档实测）]")
    cjk_tpl = ("\\documentclass{ctexart}\n\\usepackage{amsmath}\n"
               "\\begin{document}\n%s\n\\end{document}\n")
    # 这些**全部是合法写法**，必须 exit=0、0 语法问题、0 验证失败
    cjk_legal = [
        ("中文逗号夹公式", "由定理可知，$x=1$，因此成立。"),
        ("公式后中文句号", "即 $x=1$。"),
        ("中文括号包公式", "即（$x=1$）成立。"),
        ("公式内全角逗号", "$x=\\frac{1}{2}，y=\\frac{1}{3}$"),
        ("公式内全角括号", "$f（x）=x^{2}$"),
        ("中文与公式无空格", "设$f(x)$为连续函数"),
        ("全角空格", "$x\u3000=\u30001$"),
        ("中文标签不等式", "$\\text{教育价值}\\ge P_{75}$"),
        ("行内中文 text", "$\\text{其中}x=1$"),
        ("equation 内中文", "\\begin{equation}\nx=1\\quad\\text{当}n\\ge1\\text{时}\n\\end{equation}"),
        ("中文引号包公式", "所谓“$x=1$”成立"),
        ("中文冒号后公式", "定义：$f(x)=x^2$"),
        ("顿号分隔", "$x$、$y$、$z$"),
    ]
    for desc, body in cjk_legal:
        tmpf = os.path.join(tempfile.gettempdir(), "_mg_cjk_test.tex")
        open(tmpf, "w", encoding="utf-8").write(cjk_tpl % body)
        r = subprocess.run([sys.executable, os.path.join(HERE, "math_guard.py"),
                            tmpf, "--no-color"],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", env=dict(os.environ, MATH_GUARD_PARSUB_MODE="off"))
        ok = r.returncode == 0 and "0 处（环境与括号均配对）" in r.stdout
        passed += ok
        failed += not ok
        print(f"  {'PASS' if ok else 'FAIL'}  {desc:18} exit={r.returncode}")
        if not ok:
            fails.append(f"中文用例 {desc}: exit={r.returncode}")
        try:
            os.remove(tmpf)
        except OSError:
            pass

    print("\n[批处理模式 --batch]")
    # 造一个目录，放 3 个文件：一个干净、一个结构问题、一个数学错误
    bdir = os.path.join(tempfile.gettempdir(), "_mg_batch_test")
    os.makedirs(bdir, exist_ok=True)
    cases = {
        "clean.tex": "\\documentclass{article}\n\\begin{document}\n$2+2=4$\n\\end{document}\n",
        "struct.tex": "\\documentclass{article}\n\\begin{document}\n\\label{orphan}\n\\end{document}\n",
        "wrong.tex": "\\documentclass{article}\n\\begin{document}\n$2+2=5$\n\\end{document}\n",
    }
    for fn, body in cases.items():
        open(os.path.join(bdir, fn), "w", encoding="utf-8").write(body)
    r = subprocess.run([sys.executable, os.path.join(HERE, "math_guard.py"),
                        "--batch", bdir, "-q", "--no-color"],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", env=dict(os.environ, MATH_GUARD_PARSUB_MODE="off"))
    out = r.stdout
    checks = [
        ("识别 3 个文件", "3 个文件" in out),
        ("汇总干净 1", "干净       1" in out),
        ("汇总结构 1", "仅结构问题 1" in out),
        ("汇总错误 1", "数学错误   1" in out),
        ("退出码 3(有数学错误)", r.returncode == 3),
    ]
    for desc, ok in checks:
        passed += ok
        failed += not ok
        print(f"  {'PASS' if ok else 'FAIL'}  {desc}")
        if not ok:
            fails.append(f"批处理 {desc}: exit={r.returncode}")
    import shutil as _sh
    _sh.rmtree(bdir, ignore_errors=True)

    print("\n[数论论文暴露的缺陷（arXiv math.NT 24 篇）]")
    BS5 = "\\"
    ntl_cases = [
        # 勒让德符号：\bigl(...\bigr) 形式（此前只认 \left...\right）
        ("bigl 勒让德符号", BS5 + "bigl(" + BS5 + "tfrac{3}{7}" + BS5 + "bigr)=-1", "parsed"),
        ("left 勒让德符号", BS5 + "left(" + BS5 + "frac{5}{11}" + BS5 + "right)=1", "parsed"),
        # 上标表达式：2^{4-1} 曾是 2**4-1 = 15
        ("上标含表达式", "2^{4-1}=8", "ok"),
        ("上标表达式+外减", "2^{2}-1=3", "ok"),
        ("上标表达式错误", "2^{4-1}=9", "error"),
        # 装饰上标：(-)^{\sim} 的 \sim 被当关系符
        ("装饰上标 sim", "(-)^{" + BS5 + "sim}", "parsed"),
        ("上标 prime", "A^{" + BS5 + "prime}", "parsed"),
    ]
    for desc, latex, want in ntl_cases:
        d = m.verify_with_sympy(m.MathItem(kind="inline", line=1, latex=latex, original=latex))
        ok = d.status == want
        passed += ok
        failed += not ok
        print(f"  {'PASS' if ok else 'FAIL'}  {desc:18} 预期={want} 实际={d.status}")
        if not ok:
            fails.append(f"{desc}: 预期 {want}，实际 {d.status}")

    # \newenvironment 定义体里的 \begin/\end 不应参与配对检查
    ne_tex = (
        "\\documentclass{article}\n\\begin{document}\n"
        "\\newenvironment{psmallmatrix}{\\left(\\begin{smallmatrix}}"
        "{\\end{smallmatrix}\\right)}\n"
        "\\newenvironment{obs}[1][]{\\begin{obswr}x\\end{obswr}}\n"
        "\\begin{equation}a=b\\end{equation}\n"
        "\\end{document}\n"
    )
    tmpn = os.path.join(tempfile.gettempdir(), "_mg_newenv_test.tex")
    open(tmpn, "w", encoding="utf-8").write(ne_tex)
    dn = m.read_tex(tmpn)
    en, _ = m.syntax_check(dn)
    realn = [e for e in en if "document" not in (e.message or "")]
    ok = len(realn) == 0
    passed += ok
    failed += not ok
    print(f"  {'PASS' if ok else 'FAIL'}  {'newenvironment 不误报':16} 预期=0 实际={len(realn)}")
    if not ok:
        fails.append(f"newenvironment: 预期 0，实际 {len(realn)}")
        for e in realn[:2]:
            print(f"        L{e.line} {e.message[:60]}")
    try:
        os.remove(tmpn)
    except OSError:
        pass

    print("\n[变异测试暴露的缺陷：链式等式里的数字段]")
    # 实证（变异测试 B01）：r(4)=16-6=11 被判成"只含符号"，漏掉了 16-6=10≠11
    # 实证（arXiv:2611.17124）：(7/r)=(r/7)=(3/7)=-1 是勒让德符号链
    chain_cases = [
        ("链式等式真错", "r(4)=16-6=11", "error"),
        ("链式等式正确", "r(4)=16-6=10", "ok"),
        ("符号=数字=错", "x=2+2=5", "error"),
        ("符号=数字=对", "x=2+2=4", "ok"),
        ("勒让德符号链", "(7/r)=(r/7)=(3/7)=-1", "parsed"),
        ("单个勒让德", "(3/7)=-1", "parsed"),
        ("真分数运算不受影响", "1/2+1/3=5/6", "ok"),
    ]
    for desc, latex, want in chain_cases:
        d = m.verify_with_sympy(m.MathItem(kind="inline", line=1, latex=latex, original=latex))
        ok = d.status == want
        passed += ok
        failed += not ok
        print(f"  {'PASS' if ok else 'FAIL'}  {desc:18} 预期={want} 实际={d.status}")
        if not ok:
            fails.append(f"{desc}: 预期 {want}，实际 {d.status}")

    print("\n[84 篇大规模 arXiv 测试暴露的缺陷]")
    BS6 = "\\"
    big_cases = [
        # 两个本来正确的式子曾因浮点/括号误判为错
        ("分数等于小数", "1/200=0.005", "ok"),
        ("百分比", "26/500=5.2" + BS6 + "%", "ok"),
        ("括号幂表达式", "5(5^{2}-1)=120", "ok"),
        # 各种「关系符不构成数值断言」的记号
        ("远大于", "n >> 0", "parsed"),
        ("商集", "T(H)/{" + BS6 + "sim}", "parsed"),
        ("循环小数", "3122=" + BS6 + "overline{1322}", "parsed"),
        ("概率记号", BS6 + "pr{" + BS6 + "mathcal R=1}=0", "parsed"),
        ("自定义宏", BS6 + "lt B", "skip"),
        ("以 sim 结尾", "X_1, " + BS6 + "dots, X_n " + BS6 + "sim", "skip"),
    ]
    for desc, latex, want in big_cases:
        d = m.verify_with_sympy(m.MathItem(kind="inline", line=1, latex=latex, original=latex))
        ok = d.status == want
        passed += ok
        failed += not ok
        print(f"  {'PASS' if ok else 'FAIL'}  {desc:16} 预期={want} 实际={d.status}")
        if not ok:
            fails.append(f"{desc}: 预期 {want}，实际 {d.status}")

    # TikZ 图形标签不应被当作数学断言
    tikz_tex = (
        "\\documentclass{article}\n\\usepackage{tikz}\n\\begin{document}\n"
        "\\begin{tikzpicture}\n"
        "  \\node (v0) at (0,0) {$v_0$};\n"
        "  \\node[anchor=east] at ([xshift=-5pt]v0.west) {$w =$};\n"
        "\\end{tikzpicture}\n"
        "\\end{document}\n"
    )
    tmpt = os.path.join(tempfile.gettempdir(), "_mg_tikz_test.tex")
    open(tmpt, "w", encoding="utf-8").write(tikz_tex)
    dt = m.read_tex(tmpt)
    dts = [m.verify_with_sympy(it) for it in m.gather_math_items(dt)]
    badt = [x for x in dts if x.status == "error"]
    ok = len(badt) == 0
    passed += ok
    failed += not ok
    print(f"  {'PASS' if ok else 'FAIL'}  {'TikZ 标签不误报':16} 预期=0 实际={len(badt)}")
    if not ok:
        fails.append(f"TikZ 标签: {len(badt)} 个被误判")
    try:
        os.remove(tmpt)
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

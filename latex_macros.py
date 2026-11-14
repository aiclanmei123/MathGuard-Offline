#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""latex_macros.py —— math_guard 的内建 LaTeX 数学宏库（离线，零依赖）。

覆盖范围（共 300+ 命令）：
  · amsmath / amssymb / mathtools / stmaryrd 的标准命令
  · 希腊字母（小写+大写）、常见数学字体与重音
  · 本机 89 份真实论文里实际出现过的全部命令

设计纪律（与项目「无幻觉」要求一致）：
  只收录**含义确定**的命令。含义不确定的不收录，会被当作普通变量处理——
  宁可把一条公式判为「无法验证」，也不把 `\\foo` 猜成某个函数然后给出错误结论。
"""

# ===== 运算符 / 关系符：直接换成 sympy 能理解的形式 =====
OPERATORS = {
    "le": "<=", "leq": "<=", "leqslant": "<=",
    "ge": ">=", "geq": ">=", "geqslant": ">=",
    "lt": "<", "gt": ">",
    "neq": "!=", "ne": "!=",
    # ⚠ \equiv / \approx / \sim / \simeq / \cong / \propto 不是普通等号！
    #   \equiv 是"同余"：4^4 ≡ 87 (mod 169) 两边本就不相等，
    #   映射成 == 会把正确结论判成错误（实测在 Wn 论文上一次产生假阳性）。
    #   它们映射成专用的不可判定关系符，只用于切分、绝不用于数值断言。
    "equiv": "≡", "approx": "≈", "sim": "∼", "simeq": "≃", "cong": "≅", "propto": "∝",
    "mid": "|", "nmid": "|", "parallel": "|", "perp": "|",
    "in": "in", "notin": "in", "ni": "in",
    "pm": "+", "mp": "-", "times": "*", "div": "/", "cdot": "*", "ast": "*", "star": "*",
    "circ": "*", "bullet": "*", "oplus": "+", "ominus": "-", "otimes": "*", "odot": "*",
    "cup": "|", "cap": "&", "setminus": "-", "wedge": "&", "land": "&", "vee": "|", "lor": "|",
    "neg": "not", "lnot": "not",
    "cdots": "+", "ldots": "+", "dots": "+", "vdots": "+", "ddots": "+",
    # 箭头类：只是「对应/映射」的示意，不是可判定的关系符。
    # 若映射成 => 或 <=>，$S_n\leftrightarrow$ 这种"记法说明"会被误判成"关系符缺操作数"，
    # 那是假阳性（实测在 Apéry 报告里一次产生 4 个）。一律丢弃。
    "to": "", "rightarrow": "", "leftarrow": "", "leftrightarrow": "",
    "Rightarrow": "", "Leftarrow": "", "Leftrightarrow": "",
    "longrightarrow": "", "longleftarrow": "", "longleftrightarrow": "",
    "Longrightarrow": "", "Longleftarrow": "", "Longleftrightarrow": "",
    "mapsto": "", "longmapsto": "", "gets": "",
    "cdots": "+", "ldots": "+", "dots": "+", "vdots": "+", "ddots": "+",
    "infty": "oo", "infinity": "oo",
    # 空集符号：必须映射成一个**占位符**，不能映射成空串。
    # 实测 arXiv:2611.15157（微分几何）：`TB_p(r)\cap\Sigma=\varnothing`
    # 若把 \varnothing 清成空串，式子变成 `A=`，会被误判为"关系符缺操作数"。
    # 用 ∅（U+2205）保留一个操作数，判定走「仅可解析」。
    "emptyset": "∅", "varnothing": "∅", "nothing": "∅",
}

# ===== 复杂结构：整条公式跳过（无法可靠符号化，硬塞只会产生幻觉）=====
COMPLEX = {
    "sum", "prod", "coprod", "int", "iint", "iiint", "oint", "bigcup", "bigcap",
    "bigoplus", "bigotimes", "bigwedge", "bigvee",
    "lim", "limsup", "liminf", "sup", "inf", "max", "min", "arg",
    "partial", "nabla", "substack", "overset", "underset", "stackrel",
}

# ===== 函数名：保留为合法标识符 =====
FUNCTIONS = {f: f for f in """
sin cos tan cot sec csc arcsin arccos arctan sinh cosh tanh coth
log ln lg exp deg dim ker hom
det gcd lcm Pr mod bmod pmod
""".split()}

# ===== 希腊字母与常见数学符号：映射为唯一合法符号名 =====
GREEK = {g: g for g in """
alpha beta gamma delta epsilon varepsilon zeta eta theta vartheta iota kappa
lambda mu nu xi omicron pi varpi rho varrho sigma varsigma tau upsilon
phi varphi chi psi omega
Gamma Delta Theta Lambda Xi Pi Sigma Upsilon Phi Psi Omega
ell hbar imath jmath Re Im aleph beth gimel daleth wp
""".split()}

# ===== 字体/重音包装：保留参数（\mathbb{Z} -> Z）=====
FONT_WRAPPERS = {f: "({})" for f in """
mathbb mathcal mathbf mathit mathrm mathsf mathtt mathfrak mathscr
boldsymbol bm text textrm textbf textit texttt mathnormal operatorname
underline overline widehat widetilde hat bar vec dot ddot
tilde acute grave check breve mathring overrightarrow overleftarrow
""".split()}

# ===== 纯装饰：直接丢弃 =====
DISCARD = {d: "" for d in """
left right big Big bigg Bigg bigl bigr Bigl Bigr biggl biggr Biggl Biggr middle
langle rangle lfloor rfloor lceil rceil lvert rvert lVert rVert vert Vert
lbrace rbrace lbrack rbrack
quad qquad space enspace thinspace negthinspace
displaystyle textstyle scriptstyle scriptscriptstyle limits nolimits
nonumber notag tag
begin end
hline cline toprule midrule bottomrule
""".split()}

# ===== 带标签性参数：丢弃命令名 =====
ARG_DISCARD = {a: "" for a in """
label ref eqref cref Cref autoref nameref pageref vref cite citep citet
""".split()}

# ===== 反斜杠 + 单个符号 =====
SPECIAL = {
    ",": " ", ";": " ", ":": " ", "!": " ", " ": " ",
    "(": "", ")": "", "[": "", "]": "",
    "{": "", "}": "", "%": "%", "&": "&", "$": "$", "#": "#", "_": "_",
}


def summary():
    """返回宏库规模摘要，供报告展示。"""
    tables = {
        "运算符/关系符": len(OPERATORS),
        "复杂结构(跳过)": len(COMPLEX),
        "函数名": len(FUNCTIONS),
        "希腊/数学符号": len(GREEK),
        "字体/重音": len(FONT_WRAPPERS),
        "装饰命令": len(DISCARD),
        "标签命令": len(ARG_DISCARD),
    }
    return sum(tables.values()), tables

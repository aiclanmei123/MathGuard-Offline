#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
math_guard.py —— LaTeX 论文「结构 + 公式」本地体检工具
===============================================================================

用法
----
    python math_guard.py test.tex              # 标准模式：输出完整 ASCII 报告
    python math_guard.py test.tex -v           # 详细模式：额外列出被跳过的公式及原因
    python math_guard.py test.tex --no-color   # 关闭颜色（重定向到文件时自动关闭）
    python math_guard.py test.tex --max-detail 20
    python math_guard.py --demo                # 生成一份含 "错误" 的示例 .tex 并直接体检
    python math_guard.py --version

退出码
------
    0  体检完成，未发现 ERROR 级别问题（可能有 WARN / INFO）
    1  发现 ERROR 级别问题
        · 孤儿 label / 未定义 ref / 不合法的数字比较（如 1 = 2）
    2  用法错误（参数不对、找不到文件、不是 .tex 文件）
  公式解析失败不计入错误：按需求跳过即可，程序永不因公式崩溃。

设计说明（真实第三方库 + 内置回退）
------------------------------------------
需求约定的 latexnav.analyze() 与 parsub.extract_and_verify() **并不存在**——这两个库
确实在 PyPI 上（latexnav 0.1.2 / parsub 0.2.2），但真实接口完全不同：

    latexnav 没有 analyze()。真实流程：
        structure, contents = latexnav.extract_latex_structure(path)
        registry            = latexnav.build_label_registry(structure)
        text                = latexnav.format_orphan_report(structure, contents, registry)

    parsub 没有 extract_and_verify()。真实流程：
        code = parsub.analyze_latex_file(tex, output_dir=...)   # 解析 + 生成计算脚本
        parsub.run_generated_code(code, output_dir=...)         # 执行（会真的跑数值计算）
        data/summary.json -> identity_holds_numerically / verdict

因此本脚本采用「真实库 + 内置实现」交叉校验：
  · 结构检查：latexnav 判定孤儿标签（它按章节/定理元素聚合引用，更准），内置解析器
    全文件扫描判定未定义引用（latexnav 会漏掉「同段引用」而误报）。两者不一致的条目
    列为「待确认」，不冒充硬错误。
  · 公式验证：内置 sympy 覆盖全部公式作为底数；parsub 只要能拿到明确的数值结论
    （identity_holds_numerically 为真/假）就接管那一条。parsub 的执行阶段很重
    （真实论文上 40 个任务含 24 个绘图，300 秒都跑不完），超时就退化为仅提取，
    可用环境变量 MATH_GUARD_PARSUB_TIMEOUT 调整（秒，0 表示不执行）。

任一库缺失 / 报错 / 超时，都自动回退到内置实现，并在报告的「处理环境」里写明本次
实际走的路径。所有第三方库异常都被隔离，绝不会传出到主流程（程序不崩溃）。

内部流程（每个模块都能单独测试）
--------------------------------
    read_tex            安全读取（utf-8/gbk 自动识别、CRLF 归一、绝不读崩）
    builtin_structure   结构检查：注释屏蔽 + 等长替换扫描 label / ref
    gather_math_items   公式提取：环境 / 行间 / 行内
    sanitize_latex + _restore_length
                        把 LaTeX 变成「与原文等长」的可解析文本（O(n)，见函数内注释）
    _parse_expr         sympy 解析（开启隐式乘法，3y 也能理解）
    verify_with_sympy   自洽性检验：可解析性 + 数字比较 + 关系完整性
    build_report        ASCII 报告渲染（纯函数，便于单测）
"""

from __future__ import annotations

import ast
import argparse
import importlib
import os
import re
import subprocess
import sys
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

__version__ = "1.0.0"
PROG = "math_guard"

EXIT_OK, EXIT_ISSUES, EXIT_USAGE = 0, 1, 2
# 分级退出码：让 CI / 脚本能区分「公式算错了」和「有个标签没人用」
#   0 干净 / 1 仅结构问题 / 3 存在验证失败（数字关系确定不成立）/ 2 用法错误 / 4 内部错误
EXIT_MATH_ERROR = 3
EXIT_INTERNAL = 4


# ######################################################################
# 第一部分：终端显示与全局异常兜底
# ######################################################################


class _Style:
    RESET = BOLD = DIM = RED = GREEN = YELLOW = CYAN = MAGENTA = BLUE = ""

    def __init__(self, enabled: bool):
        if enabled:
            self.RESET, self.BOLD, self.DIM = "\033[0m", "\033[1m", "\033[2m"
            self.RED, self.GREEN, self.YELLOW = "\033[31m", "\033[32m", "\033[33m"
            self.CYAN, self.MAGENTA, self.BLUE = "\033[36m", "\033[35m", "\033[34m"

    @classmethod
    def auto(cls, force_off: bool = False, stream: Any = None) -> "_Style":
        if force_off or os.environ.get("NO_COLOR"):
            return cls(False)
        if os.environ.get("FORCE_COLOR"):
            return cls(True)
        try:
            return cls(bool(getattr(stream or sys.stdout, "isatty", lambda: False)()))
        except Exception:
            return cls(False)


S = _Style.auto()


def _init_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:
            pass


def _friendly_error(exc: BaseException) -> str:
    name, msg = type(exc).__name__, str(exc)
    data: Dict[str, str] = {
        "FileNotFoundError": "文件不存在，请检查路径是否正确（注意中文路径需要放在引号内）",
        "IsADirectoryError": "给定的路径是一个文件夹，而不是 .tex 文件",
        "PermissionError": "没有读取权限（文件可能被占用或被系统保护）",
        "UnicodeDecodeError": "文件编码无法识别（已尝试 utf-8 / gbk / latin-1）",
        "MemoryError": "文件过大，内存不足",
        "ImportError": "依赖导入失败（如果缺少 sympy，请执行：pip install sympy）",
        "ModuleNotFoundError": "依赖导入失败（如果缺少 sympy，请执行：pip install sympy）",
    }
    hint = data.get(name, "未预期的意外错误")
    return f"{hint}｜底层异常 {name}: {msg}"


def safe_main(argv: Optional[Sequence[str]] = None) -> int:
    debug = "--debug" in list(argv if argv is not None else sys.argv[1:])
    try:
        return main(argv)
    except KeyboardInterrupt:
        print(f"\n{S.YELLOW}已被用户中断（Ctrl+C）{S.RESET}")
        return EXIT_USAGE
    except SystemExit:
        raise
    except BaseException as exc:
        print(f"\n{S.RED}{S.BOLD}✘ 发生未预期的错误，已安全终止。{S.RESET}")
        print(f"  {_friendly_error(exc)}")
        if debug:
            traceback.print_exc()
        else:
            print(f"  {S.DIM}（加 --debug 参数可查看完整堆栈）{S.RESET}")
        return EXIT_USAGE


# ######################################################################
# 第二部分：数据结构
# ######################################################################


@dataclass
class SyntaxIssue:
    """一条语法级问题（环境/括号配对等），只做静态检查，不涉及数学。"""
    kind: str      # env-unclosed / env-mismatch / brace / left-right
    line: int
    message: str
    file: str = ""


@dataclass
class LabelDef:
    key: str
    line: int
    context: str
    file: str = ""   # 多文件项目里记录来源文件（如 sec2.tex）


@dataclass
class RefUse:
    key: str
    command: str
    line: int
    file: str = ""


@dataclass
class StructureReport:
    labels: Dict[str, List[LabelDef]] = field(default_factory=dict)
    refs: List[RefUse] = field(default_factory=list)
    orphans: List[LabelDef] = field(default_factory=list)
    undefined: List[RefUse] = field(default_factory=list)
    duplicate: List[str] = field(default_factory=list)
    cite_keys: int = 0
    source: str = "builtin"
    disputed: List[RefUse] = field(default_factory=list)  # 两工具结论不一致、待人工确认
    syntax_errors: List["SyntaxIssue"] = field(default_factory=list)   # 语法级致命错误
    syntax_warns: List["SyntaxIssue"] = field(default_factory=list)    # 语法级警告


@dataclass
class MathItem:
    kind: str
    line: int
    latex: str
    original: str
    env: str = ""


@dataclass
class CheckDetail:
    latex: str
    status: str
    reason: str = ""
    line: int = 0
    kind: str = ""
    variables: int = 0
    relations: int = 0
    critical: int = 0


@dataclass
class MockParSubResult:
    total: int = 0
    verified: int = 0
    skipped: int = 0
    errors: int = 0
    parsed: int = 0     # 只能解析、无可判定内容（表格数据/纯表达式），不算"验证通过"
    details: List[CheckDetail] = field(default_factory=list)
    source: str = "builtin-mock"
    warnings: List[str] = field(default_factory=list)
    workdir: str = ""   # parsub 的输出目录（方便查看它生成的计算脚本与报告）


@dataclass
class FileInfo:
    path: str
    size: int
    lines: int
    encoding: str
    mtime: str
    paragraphs: int
    citations: int


@dataclass
class SourceSegment:
    """合并文本中的一个起点：offset 处的内容来自 file 的第 line 行。"""
    offset: int
    file: str
    line: int


@dataclass
class TexDocument:
    path: str
    text: str
    encoding: str
    size: int
    segments: List[SourceSegment] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    files: List[str] = field(default_factory=list)

    def locate(self, offset: int) -> Tuple[int, int]:
        offset = max(0, min(offset, len(self.text)))
        before = self.text[:offset]
        line = before.count("\n") + 1
        col = offset - before.rfind("\n")
        return line, col

    def origin(self, offset: int) -> Tuple[str, int]:
        """把「合并后偏移」映射回 (原始文件, 该文件内的行号)。

        多文件项目里，报告里只写合并后的行号没有意义——用户在自己的 sec7_Sn.tex
        里根本找不到第 3000 行。必须给出真实的文件名与行号。
        """
        if not self.segments:
            return self.path, self.locate(offset)[0]
        seg = self.segments[0]
        for s in self.segments:
            if s.offset <= offset:
                seg = s
            else:
                break
        local_line = seg.line + self.text[seg.offset:offset].count("\n")
        return seg.file, local_line


# ######################################################################
# 第三部分：任意长度数字字面量补丁
# ######################################################################
_get_int_max_str_digits = getattr(sys, "get_int_max_str_digits", None)
_set_int_max_str_digits = getattr(sys, "set_int_max_str_digits", None)
LONGINT_SAFE = False
if _get_int_max_str_digits and _set_int_max_str_digits:
    try:
        _set_int_max_str_digits(max(2_000_000, _get_int_max_str_digits()))
        LONGINT_SAFE = True
    except Exception:
        LONGINT_SAFE = False


# ======================================================================
# 模块 0：安全读取 .tex 文件
# ======================================================================


def _decode_bytes(raw: bytes, path: str) -> Tuple[str, str]:
    """把字节解码成文本，返回 (文本, 编码名)。

    ⚠ 关键：必须区分「UTF-8 严格解码」与「能解出来但全是乱码」。
    某些 GBK 文件（如含 《》 的注释）所有字节都落在 ASCII+扩展区，
      · utf-8 宽松解码 → 不报错，但中文变成 � 乱码；
      · latin-1 永远能解 → 编码检测永远不会失败。
    两者都会让后续扫描静默失败（标签 0 个），却仍报告「体检通过」——这是最危险的假结果。
    因此这里改为：严格尝试 utf-8，失败再依次尝试 gbk/gb18030，最后才用替换符兜底。
    """
    if not raw:
        return "", "empty"

    def _norm(s: str) -> str:
        return s.replace("\r\n", "\n").replace("\r", "\n")

    # 1) 严格 UTF-8（带 BOM 优先）
    for enc in ("utf-8-sig", "utf-8"):
        try:
            return _norm(raw.decode(enc)), enc
        except UnicodeDecodeError:
            pass

    # 2) 中文环境常见编码（严格模式，失败才继续）
    for enc in ("gb18030", "gbk", "big5"):
        try:
            return _norm(raw.decode(enc)), enc
        except (UnicodeDecodeError, LookupError):
            pass

    # 3) 实在认不出：用 utf-8 宽松解码，并如实标注
    return _norm(raw.decode("utf-8", errors="replace")), "unknown(replace)"


# \input{name} / \include{name}（可选带 .tex 后缀）
_INPUT_RE = re.compile(r"\\(input|include|subfile)\s*\{([^}]*)\}")


def read_tex(path: str) -> TexDocument:
    """读取 .tex；如果它通过 \\input / \\include 引用了其它文件，**递归展开成一份全文**。

    为什么必须展开：真实论文的 main.tex 往往只有百来行，正文全在 sec1.tex、sec2.tex……
    里。只读 main.tex 会得到「0 个标签定义 + 上百个假孤儿标签」——全是误报。
    （实测：一份 11 个分节的论文，直接读 main.tex 报出 116 个孤儿标签，实际一个都没有。）

    展开策略：
      · 用「展开后文本 + 段表」记录每一段来自哪个文件、起始行号，行号仍可追溯；
      · 防止循环引用与过深嵌套；
      · 找不到的子文件保留一行占位注释，并在 warnings 里记下来，不静默吞掉。
    """
    warnings: List[str] = []
    encodings: List[str] = []
    seen: set = set()
    max_depth = 16
    # 记录「(文件, 该文件在父文件中的内容)」，稍后按拼接顺序计算真实偏移。
    # 之前用运行中的计数器记录偏移，递归返回后会被重复累加，导致行号错位
    # （实测把 sec2.tex 的内容算到了 sec1.tex 的头上）。改为「先拼接、后定位」。
    pieces: List[Tuple[str, str]] = []   # (绝对路径, 文本片段)

    def load(abs_path: str, depth: int) -> str:
        if depth > max_depth:
            warnings.append(f"\\input 嵌套超过 {max_depth} 层，已停止展开：{abs_path}")
            return ""
        try:
            real = os.path.abspath(abs_path)
            if real in seen:
                warnings.append(f"检测到循环引用，已跳过：{abs_path}")
                return ""
            seen.add(real)
            with open(real, "rb") as f:
                raw = f.read()
        except Exception as exc:
            warnings.append(f"无法读取 {abs_path}（{type(exc).__name__}）")
            return ""

        text, enc = _decode_bytes(raw, abs_path)
        if depth == 0:
            encodings.append(enc)
        base_dir = os.path.dirname(real)
        out: List[str] = []
        pos = 0
        for m in _INPUT_RE.finditer(text):
            chunk = text[pos:m.start()]
            out.append(chunk)
            target = m.group(2).strip()
            child = ""
            if target:
                cand = os.path.join(base_dir, target)
                if not cand.lower().endswith(".tex"):
                    cand += ".tex"
                if not os.path.isfile(cand):
                    alt = os.path.join(os.path.dirname(os.path.abspath(path)), target)
                    if not alt.lower().endswith(".tex"):
                        alt += ".tex"
                    if os.path.isfile(alt):
                        cand = alt
                if os.path.isfile(cand):
                    child = load(cand, depth + 1)
                else:
                    warnings.append(f"找不到 \\{m.group(1)}{{{target}}}（已跳过）")
            out.append(child)
            pos = m.end()
        out.append(text[pos:])
        joined = "".join(out)
        pieces.append((real, joined))
        return joined

    text = load(path, 0)

    # 二次定位：按拼接顺序累计长度，得到每个文件的真实起点。
    # pieces 是「深度优先返回顺序」，正好等于文本的拼接顺序，可直接累计。
    segments: List[SourceSegment] = []
    for real, piece in pieces:
        segments.append(SourceSegment(offset=0, file=real, line=1))
    # 真正计算：pieces 的顺序不是最终文本顺序，改为按「子串首次出现位置」定位。
    # 这对 LaTeX 是可靠的：每个子文件内容在合并文本中的首次出现即其起点。
    segments = []
    search_from = 0
    for real, piece in pieces:
        idx = text.find(piece, search_from) if piece else -1
        if idx < 0:
            idx = search_from
        segments.append(SourceSegment(offset=idx, file=real, line=1))
        search_from = idx + len(piece)

    doc = TexDocument(path=path, text=text, encoding="+".join(sorted(set(encodings))) or "empty",
                      size=len(text.encode("utf-8", "replace")))
    doc.segments = segments
    doc.warnings = warnings
    doc.files = sorted({s.file for s in segments})
    return doc


def _line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


# ======================================================================
# 通用小工具
# ======================================================================


# verbatim 类环境：内部内容必须原样保留，绝不能当作公式去验证。
# verbatim 里写 \begin{equation} 是很常见的"演示代码"，误判会让报告出现假阳性。
_VERBATIM_RE = re.compile(
    r"\\begin\s*\{(verbatim\*?|Verbatim|BVerbatim|LVerbatim|lstlisting|minted|comment|alltt)\}"
    r".*?\\end\s*\{\1\}",
    re.S,
)


# \newcommand{\name}[n]{body} / \renewcommand ... —— 老论文大量依赖自定义宏
_NEWCMD_RE = re.compile(
    r"\\(newcommand|renewcommand|providecommand)\*?\s*"
    r"(?:\{\s*\\([A-Za-z]+)\s*\}|\\\\([A-Za-z]+))\s*"
    r"(?:\[(\d)\]\s*)?"
    r"(?:\[[^\]]*\]\s*)?"
    r"\{",
    re.S,
)


_DEF_RE = re.compile(r"\\(?:def|gdef|edef|xdef)\s*\\([A-Za-z]+)")


def collect_macros(text: str) -> Dict[str, Tuple[int, str]]:
    r"""收集 \newcommand / \renewcommand / \def 定义，返回 {宏名: (参数个数, 宏体)}。

    为什么需要：老式（尤其 WinEdt + CJK 时代的）论文大量使用自定义宏，例如
        \newcommand{\lp}{\Big(\frac{-1}{p}\Big)}
        \def\sls#1#2{(\f{#1}{#2})}
    工具不认识这些宏，含宏的公式就只能落到「仅可解析」。
    实测一份老论文里有 178 条公式受此影响。展开后它们才能真正被验证。

    ⚠ \def 支持是后来补的（2026-11-14）。arXiv:2408.09776 用 14 个 \def
      定义了 \f=\frac、\sls#1#2={(\f{#1}{#2})}、\Ls 等单字母/短名宏。
      只认 \newcommand 时收集到 0 个宏，导致 \sls{1+\sqrt{-7}}2^{24}
      被当成乱码，sympy 展开成 `**2** 24` 这种双重幂，单条耗时 4.4 秒，
      整篇论文跑不完（>5 分钟无输出）。
    """
    macros: Dict[str, Tuple[int, str]] = {}

    def _body_from(pos: int) -> Optional[str]:
        """从 pos 处的 '{' 开始配对，返回宏体内容。"""
        if pos >= len(text) or text[pos] != "{":
            return None
        depth, i, n = 0, pos, len(text)
        while i < n:
            ch = text[i]
            if ch == "\\":
                i += 2
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[pos + 1:i]
            i += 1
        return None

    # ---- 1) \newcommand / \renewcommand / \providecommand ----
    # ⚠ 语义区别（实测 arXiv:2611.16739）：
    #     \providecommand{\C}{}          只在未定义时生效 → 空
    #     \renewcommand{\C}{\mathbb{C}}  强制覆盖        → \mathbb{C}
    #   若一律"先定义先得"，会保留 L28 的空定义，导致 `\mathbb F=\C`
    #   展开成 `F=`，误判为笔误。因此 renewcommand 必须覆盖。
    for m in _NEWCMD_RE.finditer(text):
        cmd = m.group(1)            # newcommand / renewcommand / providecommand
        name = m.group(2) or m.group(3)
        nargs = int(m.group(4) or 0)
        body = _body_from(m.end() - 1)
        if body is None or not name:
            continue
        if cmd == "renewcommand" or name not in macros:
            macros[name] = (nargs, body)
        # providecommand：已存在则保持原值（与 LaTeX 语义一致）

    # ---- 2) \def\name#1#2{...} / \def\name{...} ----
    for m in _DEF_RE.finditer(text):
        name = m.group(1)
        # 参数个数 = 宏名后面、{ 之前出现的 #N
        head = text[m.end() - 1:text.find("{", m.end() - 1)] if "{" in text[m.end() - 1:] else ""
        nargs = len(set(re.findall(r"#(\d)", head)))
        body = _body_from(text.find("{", m.end() - 1))
        if body is not None and name and name not in macros:
            macros[name] = (nargs, body)

    return macros


def _expand_nargs(text: str, name: str, nargs: int, body: str) -> str:
    r"""把带 n 个参数的宏整体替换掉（包括它后面的参数）。

    例：\uu[1]{u_{#1}(j)} 作用于 "\uu{k}" -> "u_{k}(j)"（参数 {k} 被消耗掉）。

    ⚠ 参数不一定是花括号。LaTeX 的规则是「一个 token」：
        \sls{1+\sqrt{-7}}2      第二个参数就是裸的 `2`
        \qp{2}                  参数是 {2}
    早期版本只认 `{...}`，遇到 \sls{...}2 就放弃展开，导致
    arXiv:2408.09776 里 \sls/\Ls 全程未展开，sympy 把 `\sls{..}2` 误解析成
    `**2** 24` 这类双重幂，单条耗时 4.4 秒，整篇跑不完（>5 分钟）。
    """
    pattern = re.compile("\\\\" + name + r"(?![A-Za-z])")
    out, last = [], 0
    for mo in pattern.finditer(text):
        if mo.start() < last:      # 已被前一个替换覆盖
            continue
        rest = text[mo.end():]
        args, pos = [], 0
        ok = True
        for _i in range(nargs):
            while pos < len(rest) and rest[pos] in " \t":
                pos += 1
            if pos >= len(rest):
                ok = False
                break
            if rest[pos] == "{":
                # 花括号参数：配对取内容
                depth, j = 0, pos
                while j < len(rest):
                    if rest[j] == "{":
                        depth += 1
                    elif rest[j] == "}":
                        depth -= 1
                        if depth == 0:
                            break
                    j += 1
                if depth != 0:
                    ok = False
                    break
                args.append(rest[pos + 1:j])
                pos = j + 1
            elif rest[pos] == "\\":
                # 控制序列作为一个 token（如 \alpha、\frac）
                mm = re.match(r"\\[A-Za-z]+|\\.", rest[pos:])
                if not mm:
                    ok = False
                    break
                args.append(mm.group(0))
                pos += mm.end()
            else:
                # 单个字符即为一个参数（LaTeX 的 token 规则）
                args.append(rest[pos])
                pos += 1
        if not ok:
            continue   # 参数不全，保持原样
        rep = body
        for k, a in enumerate(args, 1):
            rep = rep.replace("#" + str(k), a)
        out.append(text[last:mo.start()])
        out.append(rep)
        last = mo.end() + pos     # 连同参数一起跳过
    out.append(text[last:])
    return "".join(out)


def expand_macros(latex: str, macros: Dict[str, Tuple[int, str]]) -> str:
    """把公式里的自定义宏展开成原始 LaTeX（最多迭代 5 轮，防止递归宏死循环）。"""
    if not macros:
        return latex
    out = latex
    for _ in range(5):
        changed = False
        for name, (nargs, body) in macros.items():
            if nargs == 0:
                pattern = "\\\\" + name + r"(?![A-Za-z])"
                new = re.sub(pattern, lambda _m: body, out)
            else:
                # 带参宏：手工扫描并整体替换（包括参数部分），避免残留参数。
                new = _expand_nargs(out, name, nargs, body)
            if new != out:
                out, changed = new, True
        if not changed:
            break
    return out


def mask_verbatim(text: str) -> str:
    """把 verbatim / lstlisting / comment 等环境内容替换成空格（保持总长度不变）。"""
    return _VERBATIM_RE.sub(lambda m: " " * len(m.group(0)), text)


def mask_comments(text: str) -> str:
    out: List[str] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == "\\" and i + 1 < n:
            out.append(text[i:i + 2])
            i += 2
            continue
        if ch == "%":
            while i < n and text[i] != "\n":
                out.append(" ")
                i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


_INLINE_MATH_RE = re.compile(r"(?<!\\)\$(?!\$)((?:\\\$|[^$\n])+?)(?<!\\)\$")
# 单个「关系式片段」的宽松模式：从关系符开始或结束，例如 $=$ / $+$ / $\ge5$。
# 这种片段在正文或表格里很常见（"$=$ 未知数个数"、"(A) $\sum c_k$ & $147$ & $=\gamma_3(p)$"），
# 孤立看确实"缺少一侧"，但那只是排版，不是笔误——必须放行。
_RELATION_FRAGMENT_RE = re.compile(
    r"^\s*(?:[=<>]|!=|<=|>=|\\equiv|\\le|\\leq|\\ge|\\geq|\\neq|\\ne|\\approx|\\simeq|\\sim|\\ll|\\gg|\+|-)"
    r"|(?:[=<>]|\\equiv|\\le|\\leq|\\ge|\\geq|\\neq|\\ne|\\approx|\+|-)\s*$"
)

# ---- 这些都不是"数字等式"，绝不能按 == 判定 ----

# 1) 勒让德/雅可比符号：\left(\frac{a}{p}\right) 是「a 是否为二次剩余」的记号，
#    不是把 a 除以 p。实测 $(5/11)=1$ 被误判为 5/11=1，是典型假阳性。
#    ⚠ 定界符不止 \left/\right：\bigl/\bigr、Bigl/Bigr、biggl/biggr 同样常见。
#      实证（arXiv:2611.16130）：\bigl(\tfrac{3}{7}\bigr)=-1
#      只认 \left 会漏掉整类写法，误判成 3/7 = -1。
_LEGENDRE_RE = re.compile(
    r"\\(?:left|bigl|Bigl|biggl|Biggl)\s*\("
    r"\s*\\[dt]?frac\s*\{[^{}]*\}\s*\{[^{}]*\}"
    r"\s*\\(?:right|bigr|Bigr|biggr|Biggr)\s*\)")

# 1b) 勒让德符号的**简写形式**：(a/p)、(3/7)、(2/n)
#     数论论文里到处都用这个写法表示「a 模 p 的二次剩余符号」，
#     而不是把 a 除以 p。实测（arXiv:2611.17124，ZinQ）：
#         (7/r)=(r/7)=(3/7)=-1
#     被误判为 3/7 = -1。
#     判据：整个公式里出现的 (n/d) 形态，且 d 是单个字母或小整数。
_LEGENDRE_SHORT_RE = re.compile(r"\(\s*(\d{1,3}|[a-z])\s*/\s*(\d{1,3}|[a-z])\s*\)")

# 2) 导数记号说明：{ }'=d/d\tau、f'=df/dx 之类，是"定义"而不是数值断言
#    ⚠ 还有一类：行内公式「跨进正文」，等号右边根本没有数学内容：
#      \(f' =\) restriction of the modular form \(f\) ...
#    实证（arXiv:2611.16909）：$f' =$ 被误判为"缺操作数"。
#    判据：撇号后紧跟 =、且 = 之后为空 → 是在给符号作说明，不是在断言等式。
_DERIV_NOTATION_RE = re.compile(
    r"(?:\{\s*\}|['′])\s*=\s*(?:d\s*/|\\mathrm\{d\}|\\partial)|['′]\s*=\s*$")

# 3) 上下文中出现"取模 p^k / mod"时，普通等号可能是模意义下的近似写法。
#    实测原文 "(70^3)/125=2744/5 ... 取模 1331" 被误判为 2744 = 2744/5。
_MODULO_HINT_RE = re.compile(r"(?:取模|模\s*\$?|\bpmod\b|\bmod\b|modulo|\\pmod)")
_DISPLAY_RE = re.compile(r"\\\[(.*?)\\\]", re.S)
_MATH_ENV_RE = re.compile(
    r"\\begin\s*\{(equation|align|gather|multline|eqnarray)(\*?)\}(.*?)\\end\s*\{\1\2\}", re.S
)
_QUOTE_RE = re.compile(r"``.*?''", re.S)
_LABEL_RE = re.compile(r"\\label\s*\{([^}]*)\}")
_REF_RE = re.compile(r"\\(ref|eqref|autoref|nameref|pageref|cref|Cref|vref)\*?\s*\{([^}]*)\}")
_CITE_RE = re.compile(r"\\(?:cite|citep|citet|parencite|textcite|autocite|nocite)\*?\s*(?:\[[^\]]*\]\s*)*\{[^}]*\}")


def _strip_prefix(key: str) -> str:
    """标签键的**精确**形式（只做去空白）。

    ⚠ 历史教训：这里曾经「去掉第一个冒号前缀」，即 cor:fhalf -> fhalf。
      那是错的！LaTeX 里 cor:fhalf 与 eq:fhalf 是两个不同的标签。
      实测因此把「只被 eq:fhalf 引用过」的 cor:fhalf 误判为「已被引用」，
      在真实论文上漏报了真实孤儿标签。必须精确匹配。
    """
    return key.strip()


def _overlaps(span: Tuple[int, int], occupied: List[Tuple[int, int]]) -> bool:
    a, b = span
    return any(a < y and x < b for x, y in occupied)


# ======================================================================
# 模块三 / 七：语法层静态检查（只做文本结构，不碰数学）
# ======================================================================
# 定位：这两类问题会让 LaTeX 直接编译失败，或者报错信息离真正的错处很远。
# 它们是最值得自动化、也最容易做到零假阳性的检查。

_ENV_TOKEN_RE = re.compile(r"\\(begin|end)\s*\{([^}]*)\}")
_LEFT_RIGHT_RE = re.compile(r"\\(left|right)\s*(\\[()\[\]{}|.<>]|\\\{|\\\}|[()\[\]|.])")
# verbatim 类环境整体跳过（里面内容原样，不应检查括号）
_VERBATIM_ENVS = {"verbatim", "verbatim*", "Verbatim", "lstlisting", "minted", "comment", "alltt"}


def _blank_macro_definitions(text: str) -> str:
    r"""把 \def / \newcommand 的**定义体**替换成等长空格。

    为什么要这样做：宏定义体里的 \begin/\end、花括号是"定义语法"，
    不是真实的环境或配对使用。实测：
      · \def\({ \left(}                     —— 重定义定界符
      · \def\FF#1#2#3{{}_2F_1\bigg(\bmatrix{#1}\\{#2}\end{bmatrix}\bigg|#3\bigg)}
        —— 宏体里含 \begin{bmatrix}/ \end{bmatrix}
    不剥离就会误报"环境不匹配"。

    用逐字符扫描做花括号配对，支持宏体里嵌套花括号。
    """
    out = list(text)
    i, n = 0, len(text)
    pat = re.compile(r"\\(def|gdef|edef|xdef|newcommand|renewcommand|providecommand)\b")
    while i < n:
        m = pat.search(text, i)
        if not m:
            break
        j = m.end()
        # 跳到第一个未转义的 '{'，那之前是宏名/参数个数说明
        while j < n and text[j] != "{":
            if text[j] == "\\":
                j += 2
                continue
            if text[j] == "\n" and j > m.end() + 200:
                j = -1
                break
            j += 1
        if j < 0 or j >= n:
            i = m.end()
            continue
        # 逐字符配对，取出整个定义体
        depth, k = 0, j
        while k < n:
            c = text[k]
            if c == "\\":
                k += 2
                continue
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    break
            k += 1
        if k < n:
            for p in range(m.start(), k + 1):
                if text[p] != "\n":
                    out[p] = " "
            i = k + 1
        else:
            i = m.end()
    return "".join(out)


def _blank_environment_definitions(text: str) -> str:
    r"""把 \newenvironment{name}{begin}{end} 的定义体替换成等长空格。

    \newenvironment 的第二个、第三个参数是「环境体」，
    里面出现的 \begin{...} / \end{...} 和花括号是**定义内容**，
    不是文档里真实的环境嵌套。

    实证：
      · arXiv:2611.17210
          \newenvironment{obs}[1][]{\ifx\relax#1\relax\begin{obswr}
            \else\begin{obswr}[#1]\fi\begin{upshape}...}
        → 不剥离会误报 "obswr 没有对应的 \end{obswr}"
      · arXiv:2611.16819
          \newenvironment{psmallmatrix}
            {\left(\begin{smallmatrix}}
            {\end{smallmatrix}\right)}
        → 不剥离会误报 "多余的右花括号 }"
    """
    out = list(text)
    i, n = 0, len(text)
    pat = re.compile(r"\\newenvironment\*?\s*\{[^{}]*\}")
    while True:
        m = pat.search(text, i)
        if not m:
            break
        j = m.end()
        # 跳过可选的 [nargs] 与 [default]
        while j < n and text[j] in " \t\n":
            j += 1
        while j < n and text[j] == "[":
            depth_b = 1
            j += 1
            while j < n and depth_b:
                if text[j] == "[":
                    depth_b += 1
                elif text[j] == "]":
                    depth_b -= 1
                j += 1
            while j < n and text[j] in " \t\n":
                j += 1
        # 依次吞掉接下来的 {...} 体（begin 体、end 体）
        consumed_any = False
        for _ in range(2):
            while j < n and text[j] in " \t\n":
                j += 1
            if j >= n or text[j] != "{":
                break
            depth, k = 0, j
            while k < n:
                c = text[k]
                if c == "\\":
                    k += 2
                    continue
                if c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        break
                k += 1
            if k >= n:
                break
            for p in range(j, k + 1):
                if text[p] != "\n":
                    out[p] = " "
            consumed_any = True
            j = k + 1
        if consumed_any:
            for p in range(m.start(), m.end()):
                if text[p] != "\n":
                    out[p] = " "
            i = j
        else:
            i = m.end()
    return "".join(out)


def check_environments(text: str, doc: TexDocument) -> List[SyntaxIssue]:
    r"""模块三：\begin{xxx} 与 \end{xxx} 是否配对。

    LaTeX 里环境必须严格嵌套闭合。常见事故：
      · 写了 \begin{proof} 忘了 \end{proof}
      · \begin{equation} ... \end{align}（名字写错）
      · 嵌套错序：\begin{a}\begin{b}\end{a}\end{b}

    注意：只检查**本文件内**能看到的配对。\input 的子文件已被展开，所以是全文级检查。
    verbatim / lstlisting 里的内容不参与（那里面的 \begin 是字面文本）。
    """
    issues: List[SyntaxIssue] = []
    # ⚠ 必须先剥离注释！否则注释掉的 \begin{equation} 会被当成真实环境。
    # 实证（arXiv:2611.15796 L1481-1486）：作者把一段 equation 注释掉，
    # 其中 `%\begin{equation}` 与 `%end{equation}`（漏反斜杠）都在注释里，
    # 不剥离注释就会误报"环境嵌套错序"。
    scan = mask_comments(text)
    # 再把 verbatim 类环境内容抹掉，避免误判
    for env in _VERBATIM_ENVS:
        scan = re.sub(r"\\begin\s*\{" + re.escape(env) + r"\}.*?\\end\s*\{" + re.escape(env) + r"\}",
                      lambda mm: " " * len(mm.group(0)), scan, flags=re.S)
    scan = _blank_macro_definitions(scan)
    # \newenvironment{name}{begin-body}{end-body} 的**两个体**里同样会写
    # \begin{...}/\end{...}，那是环境定义，不是真实嵌套。
    # 实证（arXiv:2611.17210）：
    #   \newenvironment{obs}[1][]{\begin{obswr}...\end{obswr}}
    # 不剥离就会误报"obswr 没有对应的 \end{obswr}"。
    # 实证（arXiv:2611.16819）：
    #   \newenvironment{psmallmatrix}{\left(\begin{smallmatrix}}{\end{smallmatrix}\right)}
    # 会被误报"多余的右花括号 }"。
    scan = _blank_environment_definitions(scan)
    # CJK 风格环境（\begin{CJK*}{GBK}{song} ... \end{CJK*}）会横跨整篇文档，
    # 里面嵌套 proof/theorem 完全是合法的。这类「壳环境」不参与配对检查。
    _SHELL_ENVS = {"CJK", "CJK*", "CJKutf8", "document", "otherlanguage", "otherlanguage*"}

    stack: List[Tuple[str, int]] = []   # (环境名, 起始偏移)
    for m in _ENV_TOKEN_RE.finditer(scan):
        kind, name = m.group(1), m.group(2).strip()
        if name in _VERBATIM_ENVS or name in _SHELL_ENVS:
            continue
        if kind == "begin":
            stack.append((name, m.start()))
        else:
            if not stack:
                f, ln = doc.origin(m.start())
                issues.append(SyntaxIssue("env-unopened", ln,
                                          f"多出的 \\end{{{name}}}（此前没有对应的 \\begin）",
                                          os.path.basename(f)))
                continue
            open_name, open_pos = stack[-1]
            if open_name == name:
                stack.pop()
            else:
                # 找一找这个 name 是否在更深处
                depth = None
                for i in range(len(stack) - 1, -1, -1):
                    if stack[i][0] == name:
                        depth = i
                        break
                f, ln = doc.origin(m.start())
                if depth is None:
                    # 等价于"这个 \end 是多余的" + "最近的 \begin 没闭合"，
                    # 是同一个错误的两面。合成一条，避免同一行刷两条。
                    f, ln = doc.origin(m.start())
                    f0, ln0 = doc.origin(open_pos)
                    issues.append(SyntaxIssue(
                        "env-mismatch", ln,
                        f"\\begin{{{open_name}}}（第 {ln0} 行）与 \\end{{{name}}} 不匹配"
                        f"（环境名或嵌套顺序有误）",
                        os.path.basename(f)))
                    stack.pop()   # 一并关掉，避免再报"未闭合"
                else:
                    f0, ln0 = doc.origin(stack[depth][1])
                    issues.append(SyntaxIssue(
                        "env-order", ln,
                        f"环境嵌套错序：\\begin{{{name}}}（第 {ln0} 行）尚未闭合，"
                        f"却先遇到 \\end{{{name}}}；中间 \\begin{{{open_name}}} 未闭合",
                        os.path.basename(f)))
                    del stack[depth:]

    # 剩下未闭合的：按出现顺序报告，并指出它"一直没关"
    for name, pos in stack:
        f, ln = doc.origin(pos)
        issues.append(SyntaxIssue("env-unclosed", ln,
                                  f"\\begin{{{name}}} 没有对应的 \\end{{{name}}}（可能在文件末尾）",
                                  os.path.basename(f)))
    return issues


def check_braces(text: str, doc: TexDocument) -> List[SyntaxIssue]:
    r"""模块七：花括号 {} 与 \left...\right 的配对。

    用栈扫描，跳过：
      · 注释（% 到行尾）
      · 转义的花括号 \{ \}
      · verbatim 类环境
      · 数学公式之间的正文（正文里的花括号同样要配对，所以照样检查）

    ⚠ 说明：LaTeX 允许 \{ \} 作为字面量，那不算错的配对。
    """
    issues: List[SyntaxIssue] = []
    scan = mask_comments(text)
    for env in _VERBATIM_ENVS:
        scan = re.sub(r"\\begin\s*\{" + re.escape(env) + r"\}.*?\\end\s*\{" + re.escape(env) + r"\}",
                      lambda mm: " " * len(mm.group(0)), scan, flags=re.S)
    # \def\({...} 这类定义里的括号是"语法定义"，不是真实的括号使用：
    # 实测 ArtMathResearch.tex 用 \def\({ \left(} 重定义定界符，会被误报。
    scan = re.sub(r"\\(?:def|gdef|edef|xdef)\s*\\(?:[A-Za-z]+|.)\s*\{[^{}]*\}",
                  lambda mm: " " * len(mm.group(0)), scan)
    # \newenvironment 的两个体里的花括号同样是定义内容，不是真实使用。
    # 实证（arXiv:2611.16819）：
    #   \newenvironment{psmallmatrix}{\left(\begin{smallmatrix}}{\end{smallmatrix}\right)}
    # 不剥离会误报"多余的右花括号 }"。
    scan = _blank_environment_definitions(scan)

    stack: List[Tuple[str, int]] = []
    i, n = 0, len(scan)
    while i < n:
        ch = scan[i]
        if ch == "\\":
            # 转义的花括号 \{ \} 跳过
            if i + 1 < n and scan[i + 1] in "{}":
                i += 2
                continue
            m = _LEFT_RIGHT_RE.match(scan, i)
            if m:
                which = m.group(1)
                if which == "left":
                    stack.append(("\\left", i))
                else:   # right
                    # 只弹掉最近的 \left；中间的 { 不算错（LaTeX 允许跨花括号）
                    for k in range(len(stack) - 1, -1, -1):
                        if stack[k][0] == "\\left":
                            del stack[k]
                            break
                    else:
                        f, ln = doc.origin(i)
                        issues.append(SyntaxIssue("left-right", ln,
                                                  "\\right 之前没有匹配的 \\left",
                                                  os.path.basename(f)))
                i = m.end()
                continue
            i += 2
            continue
        if ch == "{":
            stack.append(("{", i))
        elif ch == "}":
            if stack and stack[-1][0] == "{":
                stack.pop()
            else:
                f, ln = doc.origin(i)
                issues.append(SyntaxIssue("brace", ln, "多余的右花括号 }", os.path.basename(f)))
        i += 1

    for kind, pos in stack:
        f, ln = doc.origin(pos)
        if kind == "{":
            issues.append(SyntaxIssue("brace", ln, "左花括号 { 没有闭合", os.path.basename(f)))
        else:
            issues.append(SyntaxIssue("left-right", ln,
                                      "\\left 没有对应的 \\right", os.path.basename(f)))
    return issues


def check_math_punctuation(text: str, doc: TexDocument) -> List[SyntaxIssue]:
    r"""模块六：行间公式末尾是否缺少标点。

    学术写作惯例：行间公式本身是句子的一部分，末尾应有 `,` 或 `.`。
    缺失时，读者会分不清公式与下一句的边界。

    实测调校（3 篇真实论文，31 个数学环境）：
      · 硬性要求英文 `,` `.` 会产生误报——中文论文常用 `。` 结尾，
        例如 `\text{互为相反数。}` 已是完整句。
      · 因此把中文标点 `。 ， ；` 也视为合规。
      · 选项 `\label` `\tag` `\nonumber` 先剥离再判断。
    """
    issues: List[SyntaxIssue] = []
    env_re = re.compile(
        r"\\begin\s*\{(equation|align|gather|multline|eqnarray)\*?\}(.*?)\\end\s*\{\1\*?\}", re.S)
    scan = mask_verbatim(mask_comments(text))
    for mt in env_re.finditer(scan):
        body = mt.group(2)
        # 剥离不影响"末尾字符"判断的命令
        clean = re.sub(r"\\label\s*\{[^}]*\}|\\tag\s*\{[^}]*\}|\\nonumber|\\notag", "", body)
        # 关键：末尾常见的"排版尾缀"要先剥掉，否则会把 `,\ }` 误判成无标点。
        # 实测 259 份文档里因此产生 708 条误报，其中绝大多数是：
        #   ...\\pmod{p^3},\ }       逗号其实存在，只是被 `\ }` 遮住
        #   ...\end{aligned}         多行子环境结尾，标点在外面
        for _ in range(6):
            clean = clean.rstrip()
            before = clean
            # 末尾的换行命令 \\（两个反斜杠）
            clean = re.sub(r"\\\\\s*$", "", clean)
            # 末尾的对齐符 &
            clean = re.sub(r"&\s*$", "", clean)
            # 子环境结尾
            clean = re.sub(r"\\end\s*\{[A-Za-z*]+\}\s*$", "", clean)
            # 间距/转义命令：\, \; \: \! \  \quad \qquad
            clean = re.sub(r"\\(?:quad|qquad|[ ,;:!])\s*$", "", clean)
            # 成对空花括号 {}
            clean = re.sub(r"\{\s*\}\s*$", "", clean)
            # 单个花括号
            clean = re.sub(r"[{}]\s*$", "", clean)
            if clean == before:
                break
        clean = clean.rstrip()
        if not clean:
            continue
        last = clean[-1]
        if last in ",.;" or last in "。，；、":
            continue
        # \text{...} 结尾时看括号内最后一个字符
        m2 = re.search(r"\\(?:text|mbox)\s*\{([^{}]*)\}\s*$", clean)
        if m2 and m2.group(1).rstrip()[-1:] in ",.;。，；、":
            continue
        f, ln = doc.origin(mt.start())
        tail = re.sub(r"\s+", " ", clean[-30:])
        issues.append(SyntaxIssue(
            "punct", ln,
            f"公式末尾疑似缺少标点（应以 , 或 . 结尾）： …{tail}",
            os.path.basename(f)))
    return issues


def check_empty_args(text: str, doc: TexDocument) -> List[SyntaxIssue]:
    r"""检查必填参数为空的命令：`\frac{a}{}`、`\sqrt{}`、`\hat{}` 等。

    为什么值得查：这类是**真正的笔误**，LaTeX 不会报错（能编译通过），
    但排出来分母是个空盒子，读者只能看到 "a/ "。

    实证（Apery 论文 sec7_Sn.tex:145）：
        \frac{-(e^{-x}-1)^{2}}{}      分母为空
    而同一段第 148 行写明分母应是 (e^{x}-1)^{2} —— 确实是漏写了。

    以及 sec9_tower.tex:213：
        \frac{\cdot}{}                分子分母都是占位符，残稿

    只检查**参数明确必填**的命令，避免误报（如 `\sqrt[3]{x}` 的可选参数）。
    """
    REQUIRED = {
        "frac": 2, "dfrac": 2, "tfrac": 2, "cfrac": 2, "binom": 2, "dbinom": 2,
        "sqrt": 1, "hat": 1, "bar": 1, "vec": 1, "dot": 1, "ddot": 1, "tilde": 1,
        "widehat": 1, "widetilde": 1, "overline": 1, "underline": 1,
        "mathbb": 1, "mathcal": 1, "mathrm": 1, "mathbf": 1, "text": 1, "textbf": 1,
        "operatorname": 1, "label": 1, "ref": 1, "eqref": 1, "cite": 1, "tag": 1,
    }
    issues: List[SyntaxIssue] = []
    scan = mask_verbatim(mask_comments(text))

    def read_group(s: str, i: int):
        """从 s[i] 读一个 {...}，返回 (内容, 结束位置)；不是 { 则返回 (None, i)。"""
        while i < len(s) and s[i] in " \t":
            i += 1
        if i >= len(s) or s[i] != "{":
            return None, i
        depth, j = 0, i
        while j < len(s):
            if s[j] == "\\":
                j += 2
                continue
            if s[j] == "{":
                depth += 1
            elif s[j] == "}":
                depth -= 1
                if depth == 0:
                    return s[i + 1:j], j + 1
            j += 1
        return None, i

    for m in re.finditer(r"\\([A-Za-z]+)\*?", scan):
        name = m.group(1)
        n_req = REQUIRED.get(name)
        if not n_req:
            continue
        pos = m.end()
        # 跳过可选参数 [..]
        while True:
            while pos < len(scan) and scan[pos] in " \t\n":
                pos += 1
            if pos < len(scan) and scan[pos] == "[":
                depth = 0
                while pos < len(scan):
                    if scan[pos] == "[":
                        depth += 1
                    elif scan[pos] == "]":
                        depth -= 1
                        if depth == 0:
                            pos += 1
                            break
                    pos += 1
                continue
            break
        for idx in range(n_req):
            content, pos2 = read_group(scan, pos)
            if content is None:
                break
            body = content.strip()
            # 空参数
            if not body:
                f, ln = doc.origin(m.start())
                issues.append(SyntaxIssue(
                    "empty-arg", ln,
                    f"\\{name} 的第 {idx + 1} 个参数为空（渲染出来是个空盒子）",
                    os.path.basename(f)))
            # 占位符参数：\frac{\cdot}{} 这种"分子是个点"的写法
            # 实证（Apery 论文 sec9_tower.tex:213）：
            #   \frac{\cdot}{}   —— 作者删掉了内容但留下了占位符，是残稿
            elif re.fullmatch(r"\\(?:cdot|ldots|cdots|dots|bullet|cdotp)\s*", body):
                f, ln = doc.origin(m.start())
                issues.append(SyntaxIssue(
                    "placeholder-arg", ln,
                    f"\\{name} 的第 {idx + 1} 个参数是占位符（如 \\cdot），疑似残稿",
                    os.path.basename(f)))
            pos = pos2
    return issues


def syntax_check(doc: TexDocument, lint: bool = False) -> Tuple[List[SyntaxIssue], List[SyntaxIssue]]:
    """跑语法检查，返回 (致命错误, 警告)。任何异常都隔离，不影响主流程。

    两种模式（--lint 控制）：
      · 默认「极速体检」：只查**会导致编译失败**的致命问题
        （环境配对、括号配对）。这类问题工具必须报，且误报率实测为零。
      · --lint「导师强迫症模式」：额外查排版规范（公式末尾缺标点）。
        实测在 259 份文档上会有 309 条警告，对只想验证对错的人来说太吵，
        所以默认关闭。

    这样默认模式的输出永远是「有事才说话」，不会用格式问题淹没真正的错误。
    """
    errors: List[SyntaxIssue] = []
    warns: List[SyntaxIssue] = []
    checks = [(check_environments, errors), (check_braces, errors),
              (check_empty_args, errors)]
    if lint:
        checks.append((check_math_punctuation, warns))
    for fn, bucket in checks:
        try:
            bucket.extend(fn(doc.text, doc))
        except Exception as exc:
            warns.append(SyntaxIssue("internal", 0, f"{fn.__name__} 执行异常已隔离：{type(exc).__name__}"))
    # 同一位置同类问题只保留一条
    errors = list({(e.kind, e.line, e.message): e for e in errors}.values())
    warns = list({(w.kind, w.line, w.message): w for w in warns}.values())
    errors.sort(key=lambda e: e.line)
    warns.sort(key=lambda w: w.line)
    return errors, warns


# ======================================================================
# 模块一：结构检查
# ======================================================================


def try_latexnav(path: str) -> Tuple[Optional[StructureReport], str]:
    """调用真实的 latexnav 库（已发布在 PyPI，pip install latexnav）。

    ⚠ 注意：latexnav 并没有 analyze() 函数（那是需求里的假设写法），它的真实流程是：
        structure, contents = latexnav.extract_latex_structure(path)
        registry          = latexnav.build_label_registry(structure)
        text              = latexnav.format_orphan_report(structure, contents, registry)
    其中 format_orphan_report 返回的是一段格式化文本，我们把它解析回结构化数据。

    已知局限：latexnav 按「章节 / 定理环境」组织标签，\\label 被归到最近的章节元素，
    所以「同一段落内引用了自己所在环境的 label」会被漏计，等式标签尤其容易中招。
    因此调用方会把 latexnav 的结果与内置解析器做交叉校验（见 main()）。
    """

    try:
        mod = importlib.import_module("latexnav")
    except Exception as exc:
        return None, f"未安装或导入失败（{type(exc).__name__}）"

    extract = getattr(mod, "extract_latex_structure", None)
    build = getattr(mod, "build_label_registry", None)
    fmt = getattr(mod, "format_orphan_report", None)
    if not all(callable(f) for f in (extract, build, fmt)):
        return None, "库已存在但没有 extract_latex_structure / build_label_registry / format_orphan_report"

    try:
        structure, contents = extract(path)
        registry = build(structure)
        text = fmt(structure, contents, registry)
    except Exception as exc:
        return None, f"latexnav 调用失败（{type(exc).__name__}: {str(exc)[:60]}）"

    try:
        report = StructureReport(source="latexnav")
        # 解析 format_orphan_report 的两段文本。格式示例：
        #   Orphaned labels (5 defined but never referenced):
        #     corollary     cor:d=1       an_midpoint_详解.tex:166
        #   Missing references (6 referenced but never defined):
        #     eq:JC      referenced by: ...
        section = None
        saw_header = False
        for raw in text.splitlines():
            line = raw.rstrip()
            if not line.strip():
                continue
            if line.startswith("Orphaned labels") or line.startswith("No orphaned labels"):
                section = "orphan"
                saw_header = True
                continue
            if line.startswith("Missing references") or line.startswith("No missing references"):
                section = "missing"
                saw_header = True
                continue
            body = line.strip()
            if section == "orphan":
                # 形如 "corollary    cor:d=1    file.tex:166"
                m = re.match(r"^(\S+)\s+(\S+)\s+(\S+?):(\d+)\s*$", body)
                if m:
                    report.orphans.append(LabelDef(key=m.group(2), line=int(m.group(4)),
                                                   context=f"{m.group(1)}（{m.group(3)}）"))
                else:
                    m2 = re.match(r"^(\S+)\s+(\S+)", body)  # 兜底：至少取出标签名
                    if m2:
                        report.orphans.append(LabelDef(key=m2.group(2), line=0, context=m2.group(1)))
            elif section == "missing":
                m = re.match(r"^(\S+)\s+referenced by:", body)
                if m:
                    report.undefined.append(RefUse(key=m.group(1), command="ref", line=0))

        if not saw_header:
            return None, "format_orphan_report 输出无法识别"
        return report, f"✔ latexnav {getattr(mod, '__version__', '')}".strip()
    except Exception as exc:
        return None, f"返回值解析失败（{type(exc).__name__}）"


def _describe_context(text: str, pos: int) -> str:
    head = text[:pos]
    begins = list(re.finditer(r"\\begin\s*\{([^}]+)\}", head))
    ends = list(re.finditer(r"\\end\s*\{([^}]+)\}", head))
    if begins and len(begins) > len(ends):
        return f"{begins[-1].group(1)} 环境"
    sec = re.search(r"\\(?:sub)*section\*?\s*\{([^}]*)\}\s*$", head[-200:])
    if sec:
        return f"章节标题「{sec.group(1)[:18]}」"
    line_start = head.rfind("\n") + 1
    snippet = head[line_start:pos].strip()
    if snippet:
        return snippet[-24:]
    return "独立公式/段落"


def builtin_structure(doc: TexDocument) -> StructureReport:
    text = mask_verbatim(mask_comments(doc.text))
    report = StructureReport(source="builtin")

    def where(offset: int) -> Tuple[str, int]:
        """返回 (文件名, 行号)；多文件项目里必须给出真实文件名。"""
        f, ln = doc.origin(offset)
        return os.path.basename(f), ln

    for m in _LABEL_RE.finditer(text):
        key = m.group(1).strip()
        if not key:
            continue
        fname, line = where(m.start())
        report.labels.setdefault(key, []).append(
            LabelDef(key=key, line=line, context=_describe_context(text, m.start()), file=fname)
        )
    report.duplicate = sorted(k for k, v in report.labels.items() if len(v) > 1)

    for m in _REF_RE.finditer(text):
        command = m.group(1)
        fname, line = where(m.start())
        for key in m.group(2).split(","):
            key = key.strip()
            if key:
                report.refs.append(RefUse(key=key, command=f"\\{command}", line=line, file=fname))

    report.cite_keys = len(_CITE_RE.findall(text))

    defined_keys = {_strip_prefix(k) for k in report.labels}
    referenced_keys = {_strip_prefix(r.key) for r in report.refs}

    for key, defs in report.labels.items():
        if _strip_prefix(key) not in referenced_keys:
            report.orphans.extend(defs)

    for ref in report.refs:
        if _strip_prefix(ref.key) not in defined_keys:
            report.undefined.append(ref)

    report.orphans.sort(key=lambda d: d.line)
    report.undefined.sort(key=lambda r: r.line)
    return report


# ======================================================================
# 模块二：公式提取 + sympy 验证
# ======================================================================


def _kill_tree(proc: "subprocess.Popen") -> None:
    """杀掉一个进程及其所有子孙进程（Windows 用 taskkill /T，其他系统杀进程组）。"""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True, timeout=30)
        else:
            import signal
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:
        pass
    try:
        proc.kill()
    except Exception:
        pass
    try:
        proc.wait(timeout=10)
    except Exception:
        pass


def _run_parsub_isolated(path: str, out_dir: str, timeout_s: int) -> str:
    """在独立子进程里跑完 parsub 的「分析 + 执行」，超时整棵进程树一起清理。

    为什么必须隔离：parsub.analyze_latex_file 本身在 464 条公式的论文上 5 分钟都返回不了
    （瓶颈是它内部的 LaTeX→SymPy 转换），这不是线程能中断的操作。把它放进子进程，
    父进程只用 wait(timeout) 守着，超时就用 taskkill /T 清理，保证体检脚本永不卡死。
    """

    script = (
        "import sys, parsub\n"
        "tex, out, timeout = sys.argv[1], sys.argv[2], float(sys.argv[3])\n"
        "code = parsub.analyze_latex_file(tex, output_dir=out)\n"
        "print('ANALYZED', flush=True)\n"
        "try:\n"
        "    parsub.run_generated_code(code, output_dir=out, timeout=timeout)\n"
        "except Exception:\n"
        "    pass\n"
        "print('DONE', flush=True)\n"
    )
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env.setdefault("MPLBACKEND", "Agg")
    kwargs = {}
    if os.name != "nt":
        kwargs["start_new_session"] = True
    proc = subprocess.Popen([sys.executable, "-c", script, path, out_dir, str(timeout_s)],
                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kwargs)
    try:
        proc.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        raise


def _norm_latex(text: str) -> str:
    """把两种 LaTeX 写法归一化到可比较的形式，用于匹配 parsub 的提取结果与验证结论。

    parsub 的 analysis.json 保留原文（\\theta\\log j=-E_6/E_4），summary.json 里是
    sympy 的规范化输出（\\theta \\log{\\left(j \\right)} = - \\frac{E_{6}}{E_{4}}）。
    只做字符级去装饰是不够的——\\frac、\\log 的写法差异仍然对不上——所以这里再尝试
    用 sympy 解析成规范式；解析成功就用规范式做 key，失败才退回字符归一化。
    """
    s = text.strip()
    s = re.sub(r"\\left|\\right", "", s)
    s = s.replace("{", "").replace("}", "")
    s = s.replace("\\cdot", "*").replace("\\,", "").replace("\\!", "")
    s = re.sub(r"\s+", "", s)
    s = re.sub(r"\^\s*", "^", s)
    s = re.sub(r"_\s*", "_", s)
    s = s.rstrip(".")
    return s


def _parse_key(text: str) -> str:
    """把一段 LaTeX 压成「语义 key」：优先用 sympy 规范式，失败则用字符归一化。

    这是把 parsub 的验证结论挂回公式上的关键——两种写法的字符串差异永远匹配不上，
    但它们的数学结构是同一个。做法是把等式左右两侧都求出来再仿射组合，
    这样 \\theta\\log j=-E_6/E_4 与 \\theta \\log(j)=-\\frac{E_6}{E_4} 会得到同一个 key。
    """
    sympy, _ = _sympy_available()
    if sympy is not None:
        # 由严到宽试多种写法：sympy 的 {E_{6}} / \left( \right) 风格会解析失败，
        # 需要逐步去掉花括号与 \left \right 再试。
        variants = [
            text,
            re.sub(r"\\left|\\right", "", text),
            re.sub(r"\\left|\\right", "", text).replace("{", "").replace("}", ""),
        ]
        for variant in variants:
            try:
                body = sanitize_latex(_flatten_scripts(variant)).replace("@", " ")
                parts, ops = _split_relations(body)
                if len(parts) == len(ops) + 1 and ops and all(op in ("=", "==") for op in ops):
                    exprs = [_parse_expr(sympy, re.sub(r"\s+", " ", p).strip()) for p in parts]
                    key = exprs[0] - exprs[-1]
                    for i in range(1, len(exprs) - 1):
                        key += exprs[i]
                    return "S:" + sympy.srepr(sympy.simplify(key))
                expr = _parse_expr(sympy, re.sub(r"\s+", " ", body).strip())
                return "S:" + sympy.srepr(expr)
            except Exception:
                continue
    return "T:" + _norm_latex(text)


def _parsub_worker(tex: str, out_dir: str, run_verify: bool, task_timeout: int) -> int:
    """子进程入口：跑 parsub 分析；run_verify=True 时只执行 verify 类任务。

    只跑 verify 的理由（实测数据）：
      · parsub 每个任务都要重新启动解释器 + 导入 numpy/scipy/matplotlib，固定开销约 2.5 秒；
      · 一篇 191 条公式的论文会生成 40 个任务，其中 24 个是绘图（对"公式对不对"毫无帮助），
        而且部分绘图任务会卡死（实测单个超过 60 秒）；
      · 真正能给出对错结论的只有 goal_type == "verify" 的任务，通常只有 1~5 个。
    因此默认只执行 verify 任务，并把每个任务限制在 task_timeout 秒内。
    """
    import json

    import parsub

    code = parsub.analyze_latex_file(tex, output_dir=out_dir)
    if not run_verify:
        return 0

    try:
        with open(os.path.join(out_dir, "analysis.json"), encoding="utf-8") as f:
            tasks = json.load(f).get("tasks", [])
    except Exception:
        return 0

    verify_ids = [str(i) for i, t in enumerate(tasks, 1) if t.get("goal_type") == "verify"]
    if not verify_ids:
        return 0

    # 直接调用生成的脚本，并可精确指定只跑哪些任务
    env = os.environ.copy()
    env.setdefault("MPLBACKEND", "Agg")
    subprocess.run(
        [sys.executable, code, "--output-dir", out_dir, "--tasks", ",".join(verify_ids),
         "--timeout", str(task_timeout)],
        cwd=out_dir, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        timeout=task_timeout * len(verify_ids) + 30,
    )
    return 0


def _run_parsub_isolated(path: str, out_dir: str, timeout_s: int,
                         run_verify: bool = True, task_timeout: int = 15) -> None:
    """在独立子进程里跑 parsub，超时整棵进程树一起清理（Windows 上必须 taskkill /T）。"""
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env.setdefault("MPLBACKEND", "Agg")
    kwargs = {}
    if os.name != "nt":
        kwargs["start_new_session"] = True
    cmd = [sys.executable, __file__, "--parsub-worker", path, out_dir,
           "1" if run_verify else "0", str(task_timeout)]
    proc = subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kwargs)
    try:
        proc.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        raise


def try_parsub(path: str, workdir: Optional[str] = None) -> Tuple[Optional[MockParSubResult], str]:
    """调用真实的 parsub 库（PyPI: pip install parsub）。

    ⚠ parsub 没有需求里假设的 extract_and_verify()，真实流程是：
        code = parsub.analyze_latex_file(tex, output_dir=...)   # 解析 + 生成计算脚本
        python code --tasks 8,16 --timeout 15                   # 执行（可只跑指定任务）
        data/summary.json -> identity_holds_numerically / verdict

    默认策略（MATH_GUARD_PARSUB_MODE）：
        analyze  只做分析，不执行 —— 3 秒内完成，给出 parsub 的提取结果（默认）
        verify   分析 + 只执行 verify 任务 —— 能拿到数值对错结论，但更慢
        off      完全不调用 parsub
    单次总时限由 MATH_GUARD_PARSUB_TIMEOUT 控制（秒，默认 120）。
    """

    try:
        mod = importlib.import_module("parsub")
    except Exception as exc:
        return None, f"未安装或导入失败（{type(exc).__name__}）"

    if not callable(getattr(mod, "analyze_latex_file", None)):
        return None, "库已存在但没有 analyze_latex_file()"

    import json
    import tempfile

    mode = os.environ.get("MATH_GUARD_PARSUB_MODE", "analyze").strip().lower()
    if mode == "off":
        return None, "已按 MATH_GUARD_PARSUB_MODE=off 跳过"

    out_dir = workdir or tempfile.mkdtemp(prefix="math_guard_parsub_")
    total_timeout = int(os.environ.get("MATH_GUARD_PARSUB_TIMEOUT", "25"))
    task_timeout = int(os.environ.get("MATH_GUARD_PARSUB_TASK_TIMEOUT", "15"))
    run_verify = mode == "verify"

    ran = False
    mode_note = ""
    try:
        _run_parsub_isolated(path, out_dir, total_timeout, run_verify, task_timeout)
        ran = True
    except subprocess.TimeoutExpired:
        mode_note = f"（超过 {total_timeout}s 已终止）"
    except Exception as exc:
        mode_note = f"（执行失败：{type(exc).__name__}）"

    analysis_path = os.path.join(out_dir, "analysis.json")
    expressions: List[dict] = []
    try:
        with open(analysis_path, encoding="utf-8") as f:
            expressions = json.load(f).get("expressions", []) or []
    except Exception:
        expressions = []

    if not expressions:
        return None, f"parsub 未产出 analysis.json{mode_note}"

    # summary.json 的 latex 是 sympy 规范化写法，analysis.json 是原文写法，
    # 字符串永远对不上——用 _parse_key 压成「语义 key」再匹配。
    verdicts: dict = {}
    if ran:
        try:
            with open(os.path.join(out_dir, "data", "summary.json"), encoding="utf-8") as f:
                for item in json.load(f).get("tasks", []):
                    s = item.get("summary") or {}
                    if "identity_holds_numerically" not in s:
                        continue
                    for key in (s.get("latex"), s.get("equation")):
                        if key:
                            verdicts[_parse_key(str(key))] = s
        except Exception:
            pass

    try:
        result = MockParSubResult(source=f"parsub[{mode}]{mode_note}")
        for expr in expressions:
            latex = str(expr.get("latex") or expr.get("raw_latex") or "").strip()
            if not latex:
                continue
            v = {}
            for cand in (expr.get("raw_latex"), expr.get("latex"), expr.get("sympy_str")):
                if cand and _parse_key(str(cand)) in verdicts:
                    v = verdicts[_parse_key(str(cand))]
                    break
            holds = v.get("identity_holds_numerically")
            if holds is True:
                status, reason = "ok", str(v.get("verdict") or "数值验证通过")
            elif holds is False:
                status, reason = "error", str(v.get("verdict") or "数值验证不通过")
            else:
                status, reason = "skip", "parsub 仅做符号化/求解（未执行 verify 任务）"
            result.details.append(CheckDetail(
                latex=latex, status=status, reason=reason,
                line=0, kind=str(expr.get("environment") or expr.get("kind") or "formula"),
            ))
        result.total = len(result.details)
        result.verified = sum(d.status == "ok" for d in result.details)
        result.skipped = sum(d.status == "skip" for d in result.details)
        result.errors = sum(d.status == "error" for d in result.details)
        result.workdir = out_dir
        return result, f"✔ parsub {getattr(mod, '__version__', '')}（{mode} 模式）"
    except Exception as exc:
        return None, f"返回值解析失败（{type(exc).__name__}）"


_HEADING_RE = re.compile(
    r"\\(?:part|chapter|section|subsection|subsubsection|paragraph|subparagraph|caption"
    r"|title|author|thanks)\*?\s*(?:\[[^\]]*\]\s*)?\{"
)
_MODULO_WORD_RE = re.compile(r"(?:取模|模\s*\$?\s*[0-9p]|pmod|\\bmod\b|\bmodulo\b)")


def heading_spans(text: str) -> List[Tuple[int, int]]:
    """返回所有章节/图表标题花括号内容的 (起, 止) 区间。

    标题里的数学是「记号描述」，例如
        \\subsection{$(3.7)$ 的 $-\\frac{25}{7}$ 与 $-\\frac{252}{42^2}=\\frac{5^2}{42^2}$ 注释}
    这里的等号是在指代一个式子，不是断言它们相等。判成等式错误就是假阳性。
    """
    spans: List[Tuple[int, int]] = []
    for hm in _HEADING_RE.finditer(text):
        depth, j, n = 0, hm.end() - 1, len(text)
        while j < n:
            if text[j] == "{":
                depth += 1
            elif text[j] == "}":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        spans.append((hm.end(), j))
    return spans


def _nearby_modulo(text: str, start: int, end: int, window: int = 120) -> bool:
    """公式前后一小段文本里是否提到「取模 / mod」。

    数论论文常写  $a=2744/5$（取模 1331）  ——「取模」落在相邻文本里，
    不在同一个 $...$ 内。这种等号是模意义下的写法，按普通等式判定必然误报。
    """
    left = text[max(0, start - window):start]
    right = text[end:end + window]
    # 只看最近的一句：遇到句号/换行就截断，避免把上一段的模运算算进来
    for chunk in (left, right):
        cut = max(chunk.rfind("。"), chunk.rfind("\n\n"), chunk.rfind("."))
        seg = chunk[cut + 1:] if cut >= 0 else chunk
        if _MODULO_WORD_RE.search(seg):
            return True
    return False


def _inline_symbol_context(text: str, start: int, end: int, latex: str = "") -> bool:
    r"""判断一个行内 $...$ 是否在「拿符号代替文字」而非写一条完整公式。

    实证案例：
        拟合所需数据点数 $=$ 未知数个数 $+$ 余量（$\ge5$）
        ￥题设常数 $=2\times10^{-4}$ ￥
        ESS$>1000$、$\alpha$ 均值仅 $6\times10^{-4}$（95%CI $[5,9]\times10^{-4}$）

    这些片段的共同点：**以关系符开头**（=、>、<、\ge 等），
    它们是对前面文字的补充说明，不是在独立断言某个等式。

    反例（必须仍然报错）：
        $2+2=5$  —— 完整公式，开头是数字，不是关系符。
    """
    # 核心判据：公式本身以关系符开头
    core = latex.strip()
    if not re.match(r"^\s*(?:[=<>]|\\le|\\leq|\\ge|\\geq|\\neq|\\ne|\\approx|\\equiv|\\to)", core):
        return False
    # 要求紧邻的字符不是换行（公式自身不能跨行拆开）
    # ⚠ 只能看紧邻的 1~2 个字符：早期版本检查前后 20 字符内的换行，
    #   结果在真实文档里因为前面几行是 \documentclass，导致永远判定失败。
    left = text[max(0, start - 2):start]
    right = text[end:end + 2]
    return "\n" not in left and "\n" not in right


def gather_math_items(doc: TexDocument) -> List[MathItem]:
    text = mask_verbatim(mask_comments(doc.text))
    macros = collect_macros(text)   # 老论文的自定义宏，提取时展开
    occupied: List[Tuple[int, int]] = []
    items: List[MathItem] = []
    _hspans = heading_spans(text)

    def in_heading(pos: int) -> bool:
        return any(a <= pos < b for a, b in _hspans)

    def add(kind: str, start: int, body: str, env: str = "") -> None:
        line, _ = doc.locate(start)
        expanded = expand_macros(body, macros) if macros else body
        # 标题里的公式标记为 heading 类型，验证时只做解析、不做数值断言
        if in_heading(start):
            kind = "heading"
        # 跨块模运算上下文：形如 "…=2744/5 、 … 取模 1331"，
        # 「取模」这两个字落在相邻的文本里（不在同一个 $...$ 内）。
        # 这种写法在数论论文里很常见，按普通等式判定必然假阳性，
        # 所以只要紧邻的文本提到取模/pmod，就把整条降级为「仅可解析」。
        if _nearby_modulo(text, start, start + len(body)):
            kind = "modctx"
        # 行内「符号代用」：$$插在中文句子中间，是拿它代替一个符号，不是一条公式。
        # 实证："拟合所需数据点数 $=$ 未知数个数 $+$ 余量（$\ge5$）"
        #   这里的 = + ≥5 都在代替符号，按"缺操作数"判错就是假阳性。
        # 判据：行内公式位于同一行内、且前后紧邻的都不是数学定界符/空白。
        if kind == "inline" and _inline_symbol_context(text, start, start + len(body), expanded):
            kind = "symctx"
        items.append(MathItem(kind=kind, line=line, latex=expanded,
                              original=doc.text[start:start + len(body)], env=env))
        occupied.append((start, start + len(body)))

    for m in _MATH_ENV_RE.finditer(text):
        add(m.group(1) + m.group(2), m.start(3), m.group(3), m.group(1) + m.group(2))

    for m in _DISPLAY_RE.finditer(text):
        if not _overlaps((m.start(1), m.end(1)), occupied):
            add("display", m.start(1), m.group(1))

    for m in re.finditer(r"\$\$(.*?)\$\$", text, re.S):
        if not _overlaps((m.start(1), m.end(1)), occupied):
            add("display", m.start(1), m.group(1))
    for m in _INLINE_MATH_RE.finditer(text):
        if not _overlaps((m.start(1), m.end(1)), occupied):
            add("inline", m.start(1), m.group(1))

    for m in re.finditer(r"\\\((.*?)\\\)", text, re.S):
        if not _overlaps((m.start(1), m.end(1)), occupied):
            add("inline", m.start(1), m.group(1))

    items.sort(key=lambda it: it.line)
    return items


_STRUCTURE_MAP: Dict[str, str] = {
    "frac": "(({})/({}))",
    "dfrac": "(({})/({}))",
    "tfrac": "(({})/({}))",
    "binom": "binomial({},{})",
    "sqrt": "sqrt({})",
    "text": "({})",
    "textbf": "({})",
    "mathrm": "({})",
    "mathit": "({})",
    "operatorname": "({})",
    "big": "({})",
    "Big": "({})",
    "bigg": "({})",
    "Bigg": "({})",
}

# ---- 内建宏库（latex_macros.py，离线、零依赖，300+ 条命令）----
# 采纳宏库中「含义确定」的映射；宏库中含义不明确的命令不会被收录，
# 它们会退化为普通变量或直接导致该公式被跳过——宁可漏认，也不猜。
try:
    import latex_macros as _MACROS

    MACRO_DB_OK = True
    MACRO_DB_SIZE = _MACROS.summary()[0]
except Exception:  # 宏库缺失时仍可运行，只是覆盖率降低
    _MACROS = None
    MACRO_DB_OK = False
    MACRO_DB_SIZE = 0

_SIMPLE_MAP: Dict[str, str] = {
    "quad": " ", "qquad": " ",
    "pi": "pi", "infty": "oo", "infinity": "oo",
}
if _MACROS is not None:
    _SIMPLE_MAP.update(_MACROS.OPERATORS)
# 集合：长名字优先处理，避免 \times 之类被短名字抢先替换
_STRUCTURE_NAMES = sorted(_STRUCTURE_MAP, key=len, reverse=True)
_SIMPLE_NAMES = sorted(_SIMPLE_MAP, key=len, reverse=True)
# 一次性匹配所有反斜杠命令名（含标点符号命令），从左到右按最长优先；
# 同一位置只会匹配一次，所以不会出现 \frac 被标点规则吃掉首字母的问题。
_LATEX_CMD_RE = re.compile(r"\\([A-Za-z]+|[,;:! ])")

_DISPLAY_NOISE_RE = re.compile(r"[\u4e00-\u9fff\uff00-\uffef\u3000-\u303f]+|\\hline|\\\\")
# 下标：单个下划线后面单独一个字符，是 LaTeX 语义。sympy 不认，必须在清理前展开
_SUB_SINGLE_RE = re.compile(r"(?<![A-Za-z0-9\\])_([A-Za-z0-9])")
# 「纯符号」噪音：换行、对齐符、引号、方括号、上标符（等长抹成空格）；
# 下划线不抹，留给上面的下标展开步骤处理
_SYMBOL_NOISE_RE = re.compile(r"[&~'\[\]\n]|\^")
# 单字符判断用的版本（sanitize_latex 逐字符扫描时使用，避免整串正则扫描）
_SYMBOL_NOISE_CHARS = re.compile(r"[&~'\[\]\s]")
# 花括号只在「上下标 / 普通分组」时抹掉，结构命令的参数花括号由 _restore_length 处理
_BRACE_TABLE = str.maketrans({"{": " ", "}": " "})
# 宏展开用词表：只影响可解析性，不影响数学含义
_MACRO_MAP = {"pmod": "", "bmod": "", "mod": "", "pmatrix": "", "cases": "", "array": "", "matrix": ""}
_ARG_ONLY_CMDS = tuple(sorted(set(_MACROS.FONT_WRAPPERS) | set(_MACROS.DISCARD) | set(_MACROS.ARG_DISCARD))) \
    if _MACROS is not None else ("mathbb", "mathcal", "left", "right", "displaystyle")
# 注意：frac / sqrt 已经在 _STRUCTURE_MAP 里有精确展开，绝不能算作"复杂结构"，
# 否则 \frac{1}{2}+\frac{1}{3}=\frac{2}{5} 这种可判定的式子会被整条跳过。
_COMPLEX_CMDS = tuple(sorted(set(_MACROS.COMPLEX) | {"begin", "end", "underbrace", "overbrace",
                                                    "coloneqq", "triangleq"})) \
    if _MACROS is not None else ("sum", "prod", "int", "lim", "partial", "begin", "end")
# 希腊字母等：映射为合法符号名（\alpha -> _alpha）
_GREEK_MAP: Dict[str, str] = dict(_MACROS.GREEK) if _MACROS is not None else {}
# 数学函数名：保留为 sin/log 这类合法标识符，交给 sympy 的隐式乘法处理
_FUNC_NAMES = frozenset(_MACROS.FUNCTIONS) if _MACROS is not None else frozenset(
    {"sin", "cos", "tan", "log", "ln", "exp"})
_REL_RE = re.compile(r"=>|<=|>=|!=|==|=(?!:)|<|>|≡|≈|∼|≃|≅|∝")
# 可用于「数值断言」的关系符（≡ ≈ ∼ ≃ ≅ ∝ 只用来切分，绝不参与判定）
_ASSERTABLE_OPS = frozenset({"=", "==", "<=", ">=", "!="})
# 符号解析的规模上限：极端嵌套（几百层 \frac、几千层括号）会让 sympy 的解析器
# 退化到接近指数级耗时，直接把「体检工具」卡死。超过这些阈值就整体跳过——
# 这类公式本来也超出「基础自洽性验证」的能力范围。
MAX_RELATIONS = 200        # 单条公式最多尝试的关系对比
MAX_OPERAND_LEN = 4000     # 单个操作数最长字符数（真实公式里见过几千位的精确数值）
MAX_BRACKET_DEPTH = 25     # 单个操作数的最大括号嵌套深度
MAX_COUNT = 200            # 数字关系检查时最多处理的数字个数


def _bracket_depth(text: str) -> int:
    """计算一段文本的最大括号嵌套深度（只看圆括号，方括号已被抹掉）。"""
    depth = best = 0
    for ch in text:
        if ch == "(":
            depth += 1
            best = max(best, depth)
        elif ch == ")":
            depth -= 1
    return best
# 命令名 -> 替换结果的查找表（sanitize_latex 里 O(1) 查表）；必须在上面所有映射定义之后构建
_CMD_REPLACEMENTS: Dict[str, str] = {}
_CMD_REPLACEMENTS.update({name: "@" + name for name in _STRUCTURE_MAP})
_CMD_REPLACEMENTS.update(_SIMPLE_MAP)
_CMD_REPLACEMENTS.update({name: " " for name in _ARG_ONLY_CMDS})
_CMD_REPLACEMENTS.update({name: " " * len(name) for name in _MACRO_MAP})
_CMD_REPLACEMENTS.update({"label": " ", "tag": " ", "notag": " ", "nonumber": " ",
                          "displaymath": " ", "math": " "})
# 希腊字母等映射为合法符号名（\alpha -> _alpha），保证区分性且可被 sympy 解析
_CMD_REPLACEMENTS.update({k: "_" + v for k, v in _GREEK_MAP.items()})
# 无花括号的 \frac12 形式：补成 \frac{1}{2} 再走正常流程（需求里明确要求宽松处理）
_BARE_ARG_RE = re.compile(r"\\(frac|dfrac|tfrac|sqrt)\s*([0-9])(?![0-9.])")
_BARE_TWO_RE = re.compile(r"\\(frac|dfrac|tfrac)\s*([0-9])\s*([0-9])(?![0-9.])")


def _brace_bare_args(src: str) -> str:
    r"""把 \frac12 这类简写补成 \frac{1}{2}，避免被误判为「花括号残缺」而跳过。"""
    src = _BARE_TWO_RE.sub(r"\\\1{\2}{\3}", src)
    src = _BARE_ARG_RE.sub(r"\\\1{\2}", src)
    return src


def _unwrap_font_wrappers(latex: str) -> str:
    r"""把 \mathbb{X} / \mathcal{X} / \mathit{X} 这类**字体包装**还原成 X。

    为什么需要：字体命令只改变字形，**不改变数学含义**：
        \mathbb F = \mathbb R   →   F = R
        \mathcal{M}             →   M
    若把命令名连同参数一起清掉，`\mathbb F=\mathbb R` 会变成 `F=`，
    被误判为"关系符缺操作数"。

    实证（arXiv:2611.16739，随机矩阵）正文：
        and we denote by $O(p)$ ... when $\mathbb F=\mathbb R$
    该文件还有 \renewcommand{\C}{\mathbb{C}} —— 字体包装在论文里极其常见。

    只处理**纯样式**命令白名单。\text、\operatorname、\overline、\sqrt
    等语义不同，不动。
    """
    sim = ("mathbb", "mathcal", "mathbf", "mathit", "mathsf", "mathtt",
           "mathfrak", "mathscr", "boldsymbol", "bm", "mathnormal")
    alt = "|".join(sim)
    for _ in range(3):
        # 带花括号：\mathbb{F} -> F
        new = re.sub(r"\\(" + alt + r")\s*\{([^{}]*)\}", lambda mo: mo.group(2), latex)
        # 单 token：\mathbb F -> F
        # ⚠ 必须用否定前瞻排除「紧跟字母」的情况，否则 `\mathbb C` 里的 C
        #   会被前面 `\mathbb` 本身的字母吃掉，剥成 " mathbb"。
        new = re.sub(r"\\(" + alt + r")\s*(\\[A-Za-z]+|(?![A-Za-z])[A-Za-z0-9])",
                     lambda mo: mo.group(1), new)
        if new == latex:
            break
        latex = new
    return latex


def sanitize_latex(src: str) -> str:
    # ⚠ 性能要点：这里绝不能用「多次 re.sub 重建字符串」的写法——每次 re.sub 都会
    # 扫描整条公式，嵌套 200 层的 \\frac{...}{...} 会退化成 O(n^2)，把体检工具自己卡死。
    # 因此所有步骤合并成一次「逐字符扫描」，保证 O(n)。
    # 唯一例外：字体包装（\mathbb{X}）在进入扫描前统一剥掉——它是纯样式，
    # 不剥会让 `\mathbb F=\mathbb R` 变成 `F=` 而误报。该函数只做少量 re.sub，
    # 且只对本就含这些命令的公式生效，实测不影响性能。
    src = _unwrap_font_wrappers(src)
    out: List[str] = []
    i, n = 0, len(src)
    while i < n:
        ch = src[i]
        if ch == "\\":
            m = _LATEX_CMD_RE.match(src, i)
            if m:
                name = m.group(1)
                if name in _FUNC_NAMES:
                    # 函数名（\sin, \log, \max ...）保留为合法标识符，并按 LaTeX 语义补参数：
                    #   \sin x      -> _sin(x)
                    #   \sin^2 x    -> _sin(x)**2     （LaTeX 的 \sin^2 x 就是 (\sin x)^2）
                    #   \log(1-z)   -> _log(1-z)
                    # 只在 sanitize 阶段补括号（允许长度变化），因为语义必须正确：
                    # 否则 \sin x 会被 sympy 理解成「sin 乘以 x」，那是错的。
                    rest = src[m.end():]
                    pow_m = re.match(r"\s*\^(\{)?(-?\d+)", rest)
                    after_pow = rest[pow_m.end():] if pow_m else rest
                    arg_m = re.match(r"\s*(\([^)]*\)|[A-Za-z0-9]+|\\[A-Za-z]+)", after_pow)
                    if arg_m and not after_pow[arg_m.end():].lstrip().startswith("("):
                        arg = arg_m.group(1)
                        if arg.startswith("("):
                            body = "_" + name + arg
                        else:
                            body = "_" + name + "(" + arg + ")"
                        if pow_m:
                            body += "**" + pow_m.group(2)
                            i = m.end() + pow_m.end() + arg_m.end()
                        else:
                            i = m.end() + arg_m.end()
                        out.append(body)
                        continue
                    out.append("_" + name)
                    i = m.end()
                    continue
                out.append(_CMD_REPLACEMENTS.get(name, _placeholder(name)))
                i = m.end()
                continue
            # \$ \{ \} 等「反斜杠 + 单个字符」：直接给空格
            out.append(" ")
            i += 2
            continue
        if ch == "$":
            out.append(" ")
            i += 1
            continue
        if ch == "%":
            # 理论上注释已被 mask_comments 抹掉，这里再兜一次底
            while i < n and src[i] != "\n":
                out.append(" ")
                i += 1
            continue
        if ch in "{}":
            out.append(ch)  # 花括号保留，供 _restore_length 切分参数
            i += 1
            continue
        if ch.isspace() or _SYMBOL_NOISE_CHARS.match(ch) or "\u4e00" <= ch <= "\u9fff":
            out.append(" ")
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _placeholder(name: str) -> str:
    """普通命令名（sin / Theta ...）-> 等长合法标识符 "_sin"；非字母命令 -> 空格。"""
    return "_" + name if name.isalpha() else " "


def _flatten_scripts(src: str) -> str:
    """预处理：把 LaTeX 上下标、简写宏展开成可解析的普通写法。

      · 花括号下标：a_{n} -> a_n，H_{0} -> H_0
      · 单字符下标：a_n 原样保留（单个下划线 + 单字符本身就是合法符号名）
      · 简写分式：\\frac12 -> \\frac{1}{2}，\\frac p2 -> \\frac{p}{2}
    上标（^）不在这里处理：x^{2} 的花括号要留给 _restore_length 当参数解析。
    """
    src = _brace_bare_args(src)
    out = re.sub(r"([A-Za-z])_\{([A-Za-z0-9]+)\}", r"\1_\2", src)
    return re.sub(r"_\{([A-Za-z0-9]+)\}", r"_\1", out)


def _restore_length(sanitized: str) -> str:
    """把 2.3 清理后的文本，替换成「与原文等长」的可解析形式。

    处理两类替换（都严格保持长度不变，从而让解析失败也能精确定位回原文）：

      1) 结构命令（@frac / @sqrt / @text ...）：按花括号切出参数，再用模板重写，
         例如 "@frac 6r+1  2 " -> "(((6r+1)/(2)))"。参数为空（如 \\sqrt}）会抛
         ValueError，由上层转成「跳过」。
      2) 变量名：换成占位符 _v01、_v02……两个 token 不会因为相邻而粘连成一个。
         这里用正则从右到左处理（而不是靠 ast 位置），因为 ast 位置在子节点上
         并非总能拿到，而字符串替换不存在这个依赖。
    """

    # ---------- ① 结构命令 ----------
    pieces: List[str] = []
    i, n = 0, len(sanitized)
    while i < n:
        if sanitized[i] == "@":
            m = re.match(r"@([A-Za-z]+)", sanitized[i:])
            if m and m.group(1) in _STRUCTURE_MAP:
                name = m.group(1)
                j = i + len(m.group(0))
                args: List[str] = []
                ok = True
                for _ in range(_STRUCTURE_MAP[name].count("{}")):
                    while j < n and sanitized[j].isspace():
                        j += 1
                    if j >= n or sanitized[j] != "{":
                        ok = False
                        break
                    depth, k = 0, j
                    while k < n:
                        if sanitized[k] == "{":
                            depth += 1
                        elif sanitized[k] == "}":
                            depth -= 1
                            if depth == 0:
                                break
                        k += 1
                    if depth != 0:
                        ok = False
                        break
                    args.append(sanitized[j + 1:k])
                    j = k + 1
                if not ok:
                    raise ValueError(f"结构命令 @{name} 的花括号参数不完整")
                pieces.append(_STRUCTURE_MAP[name].format(*args))
                i = j
                continue
        # ⚠ 这一行必须在 if 之外：否则所有「普通字符」都会被丢弃，公式只剩命令片段
        pieces.append(sanitized[i])
        i += 1
    work = "".join(pieces)

    # ---------- ② 剩余花括号（下标分组等）等长抹成空格 ----------
    work = work.translate(_BRACE_TABLE)

    # ---------- ② b 上标：x^{2} 这时是 "x  2 "（花括号已被抹成空格）----------
    # 必须显式捕获基底：如果只匹配「空白 + 数字」，起始位置会落在数字里，
    # "**" 就会插到数字前面，生成 "_v01**  " 这种错误结果。
    # 花括号上标：x^{2} 花括号被抹成空格后是 "x  2 "。
    # ⚠ 只允许「基底 + 纯空格 + 数字」：之前用了 \s+ 把 "2^{2}-1" 里的
    #   "-1" 也吃进指数，得到 2**2**-1（= 2^-2），把正确的算式判成错误。
    #   这里限定基底与数字之间只能是空格，且数字后不能再跟运算符。
    _pow_braced = re.compile(r"([A-Za-z0-9)])( +)(\d+(?:\.\d+)?)(?![0-9.])")
    # 单字符上标：基底 + ^ + 空格 + 单个数字
    _pow_caret = re.compile(r"([A-Za-z0-9)]) ?\^ ?(\d)")

    # ⚠ 上标内容是**表达式**而非纯数字：2^{4-1}、10^{3-2}。
    #   抹掉花括号后：
    #       2^{4-1}=8  -> '2^ 4-1 =8'     （4-1 后面是空格 = 原花括号闭合处）
    #       2^{2}-1=3  -> '2^ 2 -1=3'     （2 后面有空格，-1 在花括号**外**）
    #   因此用「内部不能出现空格」来锁定花括号内的表达式。
    #   注意：末尾的负向断言**不能禁空格**——花括号闭合处本来就会留下空格；
    #   只禁数字和小数点即可，避免把 "2^{2}-1" 里的 "-1" 吃进指数。
    #   实证（arXiv:2611.16254）：$2^{4-1}=8$ 曾被误判成 15=8。
    _pow_expr = re.compile(
        r"([A-Za-z0-9)])\s*\^\s*(\d+(?:[+\-*/]\d+)+)(?![0-9.])")

    def _sub_pow_expr(m: "re.Match[str]") -> str:
        base, expr = m.group(1), m.group(2)
        span_len = len(m.group(0))
        head = base + "**("
        body = expr + ")"
        pad = span_len - len(head) - len(body)
        return head + body + " " * max(0, pad)

    def _sub_pow(m: "re.Match[str]") -> str:
        base, gap, digits = m.group(1), m.group(2), m.group(3)
        # ** 紧贴基底，多余空白留在数字之前；总长度与原片段相等
        return base + "**" + " " * (len(gap) - 2) + digits

    # 先处理表达式上标（更长，避免被 _pow_braced 先吃掉前导数字）
    work = _pow_expr.sub(_sub_pow_expr, work)
    work = _pow_braced.sub(_sub_pow, work)

    # 单字符上标（清理后仍带 ^）：x^2 / x^ 2 / x ^ 2 -> x**2，长度保持不变
    def _sub_caret(m: "re.Match[str]") -> str:
        base, digit = m.group(1), m.group(2)
        span = m.group(0)                      # 例如 "x^2" / "x ^ 2"
        return base + "**" + " " * (len(span) - 3) + digit

    work = _pow_caret.sub(_sub_caret, work)

    # ---------- ③ 变量名 -> 占位符（必须排除函数调用名：名字紧跟 "(" 时是函数，不是变量）----------
    pos = 0

    def repl(m: "re.Match[str]") -> str:
        nonlocal pos
        token = m.group(0)
        # 纯数字开头不是变量（例如 "2a" 里的 a 才是变量，数字部分已被 \w 截断）
        if token[0].isdigit():
            return token
        rest = m.string[m.end():]
        # 后面紧跟 "(" 的是函数名（binomial / sqrt / 用户命令占位符），原样保留
        if re.match(r"\s*\(", rest):
            return token
        pos += 1
        return "_v%02d" % pos

    return re.sub(r"(?<![\w\\])[A-Za-z_]\w*", repl, work)


def _safe_eval_num(expr: str) -> Optional[Any]:
    """安全地求一个「纯数字表达式」的值（2+2、3*4、2**10、100/4、-5、1.5 ...）。

    ⚠ 为什么不用 ast.literal_eval：Python 3.13 起它**不再接受二元运算**，
       对 "2+2" 会抛 ValueError。「2 + 2 = 5」曾因此被判为「通过」——
       判定的前提都拿不到，却报告了 ok，这是最危险的一类错误。
    这里改用「先 parse，再白名单遍历」：只允许常量、四则、乘方、一元正负，
    不允许名字、属性、调用，因此同样没有代码执行风险。
    """
    try:
        tree = ast.parse(expr, mode="eval")
    except (SyntaxError, ValueError, MemoryError):
        return None

    def ev(node: ast.AST) -> Any:
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Constant):
            if isinstance(node.value, bool) or not isinstance(node.value, (int, float, complex)):
                raise ValueError("非数字常量")
            return node.value
        if isinstance(node, ast.UnaryOp):
            v = ev(node.operand)
            if isinstance(node.op, ast.USub):
                return -v
            if isinstance(node.op, ast.UAdd):
                return +v
            raise ValueError("不支持的一元运算")
        if isinstance(node, ast.BinOp):
            a, b = ev(node.left), ev(node.right)
            if isinstance(node.op, ast.Add):
                return a + b
            if isinstance(node.op, ast.Sub):
                return a - b
            if isinstance(node.op, ast.Mult):
                return a * b
            if isinstance(node.op, ast.Div):
                # 用精确分数，避免浮点误差把 1/3+1/3=2/3 判成不等
                from fractions import Fraction
                try:
                    return Fraction(a) / Fraction(b)
                except Exception:
                    return a / b
            if isinstance(node.op, ast.Pow):
                # 防止 9**9**9 这类爆炸式运算
                if isinstance(b, (int, float)) and abs(b) > 1e6:
                    raise ValueError("指数过大")
                return a ** b
            raise ValueError("不支持的运算符")
        raise ValueError("不支持的节点：" + type(node).__name__)

    try:
        return ev(tree)
    except Exception:
        return None


def _sympy_available() -> Tuple[Any, str]:
    """惰性导入 sympy，返回 (模块或 None, 说明)。"""
    try:
        import sympy
        return sympy, f"sympy {sympy.__version__}"
    except Exception as exc:
        return None, f"未安装 sympy（{type(exc).__name__}），公式验证将全部跳过"


def _parse_expr(sympy_mod: Any, text: str) -> Any:
    """用 sympy 解析表达式，并开启「隐式乘法」（3y、2(3)、binomial(3r,r) 都能理解）。

    只在 sympy 可用时构造 transformations；构造失败则退回默认解析。
    """
    try:
        from sympy.parsing.sympy_parser import (implicit_multiplication_application,
                                                standard_transformations)
        local = {}
        # binomial 是 sympy 的库函数，但不一定在解析器命名空间里，显式注册
        try:
            local["binomial"] = sympy_mod.binomial
        except Exception:
            pass
        # 宏库里的函数名（_sin/_log/...）要注册成真正的 sympy 函数，
        # 否则 _sin(x) 会被当成「_sin 乘以 x」——那正是幻觉的来源。
        for fname in _FUNC_NAMES:
            key = "_" + fname
            real = fname
            if fname == "ln":
                real = "log"
            elif fname == "lg":
                real = "log"
            fn = getattr(sympy_mod, real, None)
            if callable(fn):
                local[key] = fn
        return sympy_mod.parse_expr(text, local_dict=local or None,
                                    transformations=standard_transformations
                                    + (implicit_multiplication_application,), evaluate=True)
    except ImportError:
        return sympy_mod.parse_expr(text, evaluate=True)


def _insert_implicit_mul(expr: str) -> str:
    """在纯数字表达式中补全隐式乘号：`)3` -> `)*3`、`2(3)` -> `2*(3)`。

    只处理「数字/右括号」后紧跟「左括号」的两种相邻形式，绝不触碰函数名
    （含变量/函数的表达式在调用处已被过滤掉）。
    """
    out = expr.replace(")(", ")*(")
    out = re.sub(r"(?<=\d)\(", "*(", out)          # 2(3) -> 2*(3)
    out = re.sub(r"\)(?=\d)", ")*", out)           # (3)2 -> (3)*2
    return out


def _split_relations(expr: str) -> Tuple[List[str], List[str]]:
    """按关系符切分：返回 (操作数列表, 关系符列表)。

    例："1+2 = 3 = 3" 会得到操作数 ["1+2", "3", "3"] 与关系符 ["=", "="]。
    """
    parts, ops, last = [], [], 0
    for m in _REL_RE.finditer(expr):
        parts.append(expr[last:m.start()])
        ops.append(m.group(0))
        last = m.end()
    parts.append(expr[last:])
    return parts, ops


def _check_pure_numeric(item: MathItem, parts: List[str], ops: List[str]) -> CheckDetail:
    """对「纯数字公式」直接做字面比较（不经 sympy，几万位的整数也很快）。"""
    def lit(piece: str) -> Optional[Any]:
        text = _insert_implicit_mul(re.sub(r"\s+", "", piece)).replace("^", "**")
        return _safe_eval_num(text)

    for idx, op in enumerate(ops):
        if op not in _ASSERTABLE_OPS:
            continue
        # 含同余/近似关系（≡ ≈ ∼）时不做数值断言，避免产生假阳性
        if any(o not in _ASSERTABLE_OPS for o in ops):
            continue
        # 链式比较（如 594 ≡ 87 (mod 169) 的连等）从第二段起不做断言，避免假阳性
        if len(ops) > 1 and idx > 0:
            continue
        lv, rv = lit(parts[idx]), lit(parts[idx + 1])
        if lv is None or rv is None:
            continue
        try:
            ok = {"=": lv == rv, "==": lv == rv, "<=": lv <= rv,
                  ">=": lv >= rv, "!=": lv != rv}[op]
        except Exception:
            continue
        if ok is False:
            # 超长数字只在提示里截断显示，避免刷屏
            ls = str(lv) if len(str(lv)) <= 24 else str(lv)[:10] + "…" + str(lv)[-6:]
            rs = str(rv) if len(str(rv)) <= 24 else str(rv)[:10] + "…" + str(rv)[-6:]
            return CheckDetail(latex=item.latex, status="error",
                               reason=f"数字比较 {ls} {op} {rs} 不成立",
                               line=item.line, kind=item.kind, relations=len(ops), critical=1)
    # 没有等式关系（只是单个数值/表达式，如表格里的 "64"）→ 不算验证通过
    if not ops:
        return CheckDetail(latex=item.latex, status="parsed",
                           reason="纯数值/表达式，无等式可判定（常见于表格数据）",
                           line=item.line, kind=item.kind, relations=0, critical=0)
    # 含同余/近似关系 → 也不能算"通过"
    if any(o not in _ASSERTABLE_OPS for o in ops):
        return CheckDetail(latex=item.latex, status="parsed",
                           reason="含同余/近似关系（≡ ≈ ∼），需结合模数或精度才能判定",
                           line=item.line, kind=item.kind, relations=len(ops), critical=0)
    return CheckDetail(latex=item.latex, status="ok", line=item.line, kind=item.kind,
                       relations=len(ops), critical=len(ops))


def verify_with_sympy(item: MathItem) -> CheckDetail:
    try:
        sympy, _ = _sympy_available()
        if sympy is None:
            return CheckDetail(latex=item.latex, status="skip", reason="缺少 sympy 依赖",
                               line=item.line, kind=item.kind)
        if not item.latex.strip():
            return CheckDetail(latex=item.latex, status="skip", reason="空公式",
                               line=item.line, kind=item.kind)

        masked = sanitize_latex(_flatten_scripts(item.latex))
        if not re.search(r"[A-Za-z0-9_+*/=<>^()-]", masked):
            return CheckDetail(latex=item.latex, status="skip",
                               reason="仅含文本/坐标/xrightarrow，无数学内容",
                               line=item.line, kind=item.kind)

        cmds = set(re.findall(r"\\([A-Za-z]+)", item.original))
        hit = sorted(c for c in cmds if c in _COMPLEX_CMDS)
        if hit:
            return CheckDetail(latex=item.latex, status="skip",
                               reason="含复杂结构 " + "、".join("\\" + c for c in hit[:3]),
                               line=item.line, kind=item.kind)

        try:
            final = _restore_length(masked)
        except ValueError:
            return CheckDetail(latex=item.latex, status="skip", reason="幂/根号参数为空或花括号残缺",
                               line=item.line, kind=item.kind)
        final = final.replace("@", " ")

        parts, ops = _split_relations(final)

        # ---------- 排除「看着像等式、其实不是」的三类写法 ----------
        # 必须放在「关系符缺操作数」判断之前，否则会被更早地判成笔误。
        # 实证：本机 251 份真实文档的 10 个「验证失败」里，8 个属于这三类。
        if item.kind == "heading":
            return CheckDetail(latex=item.latex, status="parsed",
                               reason="章节/图表标题中的记号，不作为等式断言",
                               line=item.line, kind=item.kind, relations=len(ops))
        if item.kind == "modctx":
            return CheckDetail(latex=item.latex, status="parsed",
                               reason="相邻文字提到取模，等号可能是模意义写法",
                               line=item.line, kind=item.kind, relations=len(ops))
        if item.kind == "symctx":
            return CheckDetail(latex=item.latex, status="skip",
                               reason="嵌在中文句中的符号代用（如 $=$、$\\ge5$），非完整公式",
                               line=item.line, kind=item.kind, relations=len(ops))
        if _LEGENDRE_RE.search(item.latex):
            return CheckDetail(latex=item.latex, status="parsed",
                               reason="勒让德/雅可比符号记号（a/p），不是除法等式",
                               line=item.line, kind=item.kind, relations=len(ops))
        # 简写形式 (a/p)、(3/7)：整条公式全是这种记号链时，按勒让德符号处理
        if _LEGENDRE_SHORT_RE.search(item.latex):
            stripped = _LEGENDRE_SHORT_RE.sub("", item.latex)
            # 去掉符号后若只剩 = -1 / = 1 这类值，说明整式是「符号 = 值」
            if re.fullmatch(r"[\s=+\-−]*(-?1)?[\s,.]*", stripped):
                return CheckDetail(latex=item.latex, status="parsed",
                                   reason="勒让德符号简写（(a/p) 链），不是除法",
                                   line=item.line, kind=item.kind, relations=len(ops))
        if _DERIV_NOTATION_RE.search(item.latex):
            return CheckDetail(latex=item.latex, status="parsed",
                               reason="导数记号说明（如 '=d/dτ），不是数值断言",
                               line=item.line, kind=item.kind, relations=len(ops))
        if _MODULO_HINT_RE.search(item.latex):
            return CheckDetail(latex=item.latex, status="parsed",
                               reason="含模运算语境（取模/pmod），普通等号可能是模意义写法",
                               line=item.line, kind=item.kind, relations=len(ops))

        compact_parts = [re.sub(r"\s+", "", p) for p in parts]
        if ops and any(p == "" for p in compact_parts):
            # 0) align/gather 里 `A\ge` + `\\` + `&\ge B` 这种断行：
            #    换行符与对齐符把两个关系符粘在一起，中间夹了个空操作数。
            #    实证（arXiv:2611.16828）：整段 align* 因此被判"缺操作数"。
            #    判据：原文含 align/gather 类环境或 `&`，且空段两侧都是关系符。
            if re.search(r"&|\\\\", item.original):
                return CheckDetail(latex=item.latex, status="skip",
                                   reason="多行对齐公式的断行处（& 或 \\\\），非笔误",
                                   line=item.line, kind=item.kind)
            # 0b) 上标里是**装饰符号**而非关系符：(-)^{\sim}、A^{\sim}、f^{\prime}
            #    \sim 被映射成 ∼ 后会被 _REL_RE 当成关系符，把式子切成 `(-)^` + 空。
            #    实证（arXiv:2611.17210）：$(-)^{\sim}$ 表示"反射包"，被误判缺操作数。
            if re.search(r"\^\s*\{\s*\\(?:sim|approx|simeq|cong|prime|ast|dagger|star)\s*\}", item.latex) \
                    or re.search(r"\^\s*\\(?:sim|prime|ast|dagger|star)(?![A-Za-z])", item.latex):
                return CheckDetail(latex=item.latex, status="parsed",
                                   reason="上标是装饰符号（\\sim、\\prime 等），非关系断言",
                                   line=item.line, kind=item.kind)
            # 1) 「排版用的关系式片段」：如 $=$、$+$、$\ge5$，孤立的 = 不是笔误。
            #    判据：整个公式**没有任何字母/变量**（只有符号和至多一个数字）才算。
            #    "x =" 里有字母 x，那是真笔误，必须继续往下判。
            frag = _RELATION_FRAGMENT_RE.search(item.latex)
            core = re.sub(r"[\s\\{}]", "", item.latex)
            has_var = bool(re.search(r"[A-Za-z]", core))
            if frag and not has_var and len(core) <= 4:
                return CheckDetail(latex=item.latex, status="skip",
                                   reason="排版用的关系式片段（如 =、+、≥），非完整公式",
                                   line=item.line, kind=item.kind)
            # 2) 操作数是 \text{中文} 之类的文字标签：
            #    "教育价值 \ge P_{75}" 是合法的「文字标签 ≥ 数值」写法，
            #    中文被清空成空格后看起来"缺操作数"，其实是误判。
            #    实证：华为杯论文 L243 因此被误报。
            if re.search(r"\\text\s*\{[^{}]*\}|\\mbox\s*\{[^{}]*\}|\\textit\s*\{[^{}]*\}"
                         r"|[\u4e00-\u9fff]", item.original):
                return CheckDetail(latex=item.latex, status="parsed",
                                   reason="操作数含文字标签（如 \\text{中文}），非纯数学断言",
                                   line=item.line, kind=item.kind)
            # 3) 逻辑联结词 \land \lor 会把式子切成多段，段首段尾本就是操作数
            if re.search(r"\\(?:land|lor|wedge|vee|implies|iff)\b", item.original):
                return CheckDetail(latex=item.latex, status="parsed",
                                   reason="含逻辑联结词（\\land / \\lor），由多个条件组合而成",
                                   line=item.line, kind=item.kind)
            # 4) \char 等底层字符命令：多为格式转换残渣，不作断言
            if re.search(r"\\char\s*\"?[0-9A-Fa-f]+|\{\\char", item.original):
                return CheckDetail(latex=item.latex, status="skip",
                                   reason="含 \\char 底层字符命令（常见于格式转换残渣）",
                                   line=item.line, kind=item.kind)
            # 5) 「悬空关系符」：多行公式被排版拆开造成的半句。
            #    结尾型：\displaystyle\Sigma_{4}(m)\equiv   /   p^{2}=
            #    开头型：=2\times10^{-4}   /   \ge5
            #    两者都不是笔误，是 alignsplit 断行的产物。
            stripped = item.latex.strip()
            # 「续行」必须真的是排版断行产物，而不是一句写错了的公式。
            # 判据（缺一不可）：
            #   · 出现在行间公式里（display/inline 都可能，但 inline 不能太短）；
            #   · 关系符一侧为空，另一侧有内容；
            #   · 整个公式不含 \displaystyle 之外的控制序列时，还要求长度足够短——
            #     "x =" 这种只有两个 token 的，宁可报错让人看一眼，也不放过。
            has_display_hint = bool(re.search(r"\\(?:displaystyle|begin|end|qquad|quad|\\\\)",
                                              item.latex))
            token_count = len(re.findall(r"[A-Za-z\\]+|\d+|[^\sA-Za-z\d]", stripped))
            # 「关系符在一端、另一端有实质内容」= 续行；两端都没内容才是真笔误。
            #   "\ge5"    -> 关系符在开头、右边有 5  -> 续行
            #   "x ="     -> 关系符在结尾、左边有 x，右边全空 -> 也可能续行，但太短则报错
            #   实际区分：右边非空（开头型）通常可信；结尾型需要更多 token 佐证。
            looks_like_continuation = has_display_hint or token_count > 3
            # 开头型：=、>=、\ge 在开头，右边必须有内容 —— 这是明确的续行
            if re.match(r"^\s*(?:[=<>]|\\le|\\leq|\\ge|\\geq|\\neq|\\ne|\\equiv|\\sim|\\approx"
                        r"|\\simeq|\\cong|\\propto)\s*\S", stripped):
                return CheckDetail(latex=item.latex, status="skip",
                                   reason="多行公式的续行或不完整关系（关系符在开头），非完整公式",
                                   line=item.line, kind=item.kind)
            # 结尾型：关系符在结尾、左边有内容，且不是极短式子
            if looks_like_continuation and re.search(
                    r"(?:[=<>&\+-]|\\equiv|\\le|\\leq|\\ge|\\geq|\\neq|\\ne|\\approx)\s*$", stripped):
                return CheckDetail(latex=item.latex, status="skip",
                                   reason="多行公式的续行（以关系符结尾），非完整公式",
                                   line=item.line, kind=item.kind)
            # 6) 「关系符号本身作为一条公式」：概率论里 $\eqd$ 展开后是
            #    \overset{d}{=}（依分布相等），常被单独写在两式之间，
            #    此时 = 两侧都空是**正常用法**，不是笔误。
            #    实证（arXiv:2611.17234，first-passage percolation）：
            #      \newcommand{\eqd}{\overset{d}{=}}  然后写成  $X_n \eqd N(0,1)$
            if re.fullmatch(r"\s*\\overset\s*\{[^{}]*\}\s*\{[^{}]*\}\s*", item.latex) \
                    or re.fullmatch(r"\s*\\stackrel\s*\{[^{}]*\}\s*\{[^{}]*\}\s*", item.latex):
                return CheckDetail(latex=item.latex, status="skip",
                                   reason="关系符号本身（如 \\overset{d}{=} 依分布相等），非完整公式",
                                   line=item.line, kind=item.kind)
            return CheckDetail(latex=item.latex, status="error",
                               reason="关系符缺少一侧操作数，疑似笔误",
                               line=item.line, kind=item.kind)

        # 规模上限：超长/超多关系式直接跳过，避免 sympy 解析器卡死。
        # 例外：整条公式只由「数字 + 关系符」组成（例如几万位的精确数值），它既不会
        # 拖慢 sympy，恰恰又最值得核对——直接走字面比较，只受数字个数上限约束。
        if len(parts) > MAX_RELATIONS:
            return CheckDetail(latex=item.latex, status="skip",
                               reason=f"关系符过多（{len(ops)} 个），超出基础验证范围",
                               line=item.line, kind=item.kind)
        long_operand = any(len(p) > MAX_OPERAND_LEN for p in parts)
        digits_only = all(re.fullmatch(r"[\s0-9.+\-*/()^eE]*", p) for p in parts)
        if long_operand and not digits_only:
            return CheckDetail(latex=item.latex, status="skip",
                               reason="单个表达式过长，超出基础验证范围",
                               line=item.line, kind=item.kind)
        if sum(len(re.findall(r"\d+", p)) for p in parts) > MAX_COUNT:
            return CheckDetail(latex=item.latex, status="skip",
                               reason="数字项过多，超出基础验证范围",
                               line=item.line, kind=item.kind)
        if digits_only:
            return _check_pure_numeric(item, parts, ops)
        if any(_bracket_depth(p) > MAX_BRACKET_DEPTH for p in parts):
            return CheckDetail(latex=item.latex, status="skip",
                               reason="括号嵌套过深，超出基础验证范围",
                               line=item.line, kind=item.kind)

        # 逐操作数解析（关系符已被 _split_relations 切掉）：只要求每个操作数能被
        # sympy 理解，不要求整条链式关系合法——「1 = 2」这种数字比较由下面第 6 步
        # 负责判定对错，这里只判断"能不能被符号计算理解"。
        parsed_any = None
        bad_operand = ""
        for piece in parts:
            candidate = piece.strip()
            if not candidate:
                continue
            try:
                parsed_any = _parse_expr(sympy, candidate)
                break
            except Exception:
                bad_operand = candidate
                continue
        if parsed_any is None and len(ops) <= 1:
            detail_reason = "sympy 无法解析（超出符号计算能力的结构）"
            if bad_operand:
                detail_reason += "：" + bad_operand[:40]
            return CheckDetail(latex=item.latex, status="skip",
                               reason=detail_reason,
                               line=item.line, kind=item.kind)

        variables = 0
        try:
            if parsed_any is not None:
                variables = len(getattr(parsed_any, "atoms", lambda _t: set())(sympy.Symbol))
            if not variables:
                plain = re.sub(r"\bint\b|\bfloat\b", " ", final)
                variables = len(set(re.findall(r"(?<![\w\\])[A-Za-z_]\w*", plain)))
        except Exception:
            variables = 0

        def literal(piece: str) -> Optional[Any]:
            """只对「纯数字表达式」求值；含变量则返回 None。

            LaTeX 的隐式乘法（2y、3x）在 Python 里非法，所以先用 ast 做一次安全
            的「乘号补全」，再交给 _safe_eval_num 求值（白名单遍历，不执行代码）。
            补全规则：相邻的 数字/括号 组合之间自动插入乘号。
            """
            stripped = re.sub(r"\s+", "", piece)
            if not stripped:
                return None
            if re.search(r"(?<![\w.])[A-Za-z_]", stripped):
                return None  # 含变量或函数名，不做数字断言
            py = _insert_implicit_mul(stripped).replace("^", "**")
            return _safe_eval_num(py)

        n_critical = 0
        for idx, op in enumerate(ops):
            if op not in _ASSERTABLE_OPS:
                continue
            # 含不可判定关系符（≡ ≈ ∼ 等，同余/近似）的公式整条不做数值断言：
            #   4^4 ≡ 87 (mod 169) 两边本来就不相等，用 == 判定必然产生假阳性。
            if any(o not in _ASSERTABLE_OPS for o in ops):
                continue
            lv = literal(parts[idx])
            rv = literal(parts[idx + 1])
            if lv is None or rv is None:
                continue
            # 连等 / 链式比较的第二段之后不做数字断言：像
            #     22·27 = 594 ≡ 87 (mod 169)
            # 里 594 与 87 本就不相等（差一个模数），断言相等只会是假阳性。
            # ⚠ 但**相邻两段都是纯数字**时必须断言，否则会漏报：
            #     r(4)=16-6=11   —— 三段依次是 符号 / 16-6 / 11，
            #     第二、三段都是纯数字，16-6=10≠11 是确定错误。
            #   实测（变异测试 B01）：这处漏报过。
            if len(ops) > 1 and idx > 0:
                nxt = literal(parts[idx + 1]) if idx + 1 < len(parts) else None
                if nxt is None:
                    continue
            n_critical += 1
            try:
                ok = {"=": lv == rv, "==": lv == rv, "<=": lv <= rv,
                      ">=": lv >= rv, "!=": lv != rv}[op]
            except Exception:
                continue
            if ok is False:
                return CheckDetail(latex=item.latex, status="error",
                                   reason=f"数字比较 {lv} {op} {rv} 不成立",
                                   line=item.line, kind=item.kind, variables=variables,
                                   relations=len(ops), critical=1)

        # ---------- 7. 结论（严格四态，绝不把"能解析"冒充成"已验证"）----------
        #   error  : 数字关系确定不成立            —— 唯一可以断言"错"的状态
        #   ok     : 至少做过一次确定的数字判定     —— 即 n_critical > 0
        #   parsed : 只能解析、没有任何可判定内容   —— 表格里的数据、纯表达式、
        #             同余式、只含符号的关系式。这类"通过"没有证明价值，
        #             必须单列，否则报告会虚高（实测一篇论文 232 个"通过"里有 30 个属于此类）。
        if n_critical > 0:
            detail = CheckDetail(latex=item.latex, status="ok", line=item.line, kind=item.kind,
                                 variables=variables, relations=len(ops), critical=n_critical)
            if len(ops) >= 3:
                detail.reason = f"含 {len(ops)} 个关系符（连等式，仅首段做了数字判定）"
            return detail

        # 没有任何确定的数字断言 —— 不作"通过"结论
        if not ops:
            why = "纯表达式/数值，无等式可判定（常见于表格数据）"
        elif any(o not in _ASSERTABLE_OPS for o in ops):
            why = "含同余/近似关系（≡ ≈ ∼），需结合模数或精度才能判定"
        elif variables > 0:
            why = "只含符号，无数字可判定（需代入数值才能验证）"
        else:
            why = "无可用数字断言"
        return CheckDetail(latex=item.latex, status="parsed", reason=why,
                           line=item.line, kind=item.kind, variables=variables,
                           relations=len(ops), critical=0)
    except Exception as exc:
        return CheckDetail(latex=item.latex, status="skip",
                           reason=f"内部异常已隔离（{type(exc).__name__}）",
                           line=item.line, kind=item.kind)


def mock_parsub_extract_and_verify(path: str, doc: Optional[TexDocument] = None) -> MockParSubResult:
    result = MockParSubResult(source="builtin-mock")
    try:
        doc = doc or read_tex(path)
        items = gather_math_items(doc)
        result.total = len(items)
        for item in items:
            try:
                result.details.append(verify_with_sympy(item))
            except Exception as exc:
                result.details.append(CheckDetail(latex=item.latex, status="skip",
                                                  reason=f"已跳过（{type(exc).__name__}）",
                                                  line=item.line, kind=item.kind))
        result.verified = sum(d.status == "ok" for d in result.details)
        result.skipped = sum(d.status == "skip" for d in result.details)
        result.errors = sum(d.status == "error" for d in result.details)
        result.parsed = sum(d.status == "parsed" for d in result.details)

        aligned = sum(1 for d in result.details
                      if d.status == "skip" and ("不平衡" in d.reason or "矩阵" in d.reason))
        if aligned:
            result.warnings.append(
                f"共有 {aligned} 条公式因矩阵/多行对齐结构（对齐符、换行）无法安全切分而跳过"
                f"——这是预期行为，不影响其他公式的结论"
            )
    except Exception as exc:
        result.warnings.append(f"提取阶段出现异常，已安全跳过：{type(exc).__name__}")
    return result


# ======================================================================
# 模块三：ASCII 报告渲染
# ======================================================================

_TITLE = r"""
  __  __      _   _     ___   ___   _   _   ___   ___
 |  \/  |    | | | |   / __| / _ \ | | | | / _ \ | _ \
 | |\/| |   _| |_| |_ | (_ | | |  || | | || (_) ||  _/
 |_|  |_|  |_|\___/|_| \___| |_|  ||_| |_| \___/ |_|

"""

WIDTH = 118
RULE = "═" * WIDTH
THIN = "─" * (WIDTH - 4)


def _bar(ok: int, skip: int, err: int, width: int = 30) -> str:
    total = max(1, ok + skip + err)
    n_ok = round(width * ok / total)
    n_err = round(width * err / total)
    n_skip = max(0, width - n_ok - n_err)
    return (f"{S.GREEN}{'█' * n_ok}{S.RESET}"
            f"{S.YELLOW}{'▒' * n_skip}{S.RESET}"
            f"{S.RED}{'▓' * n_err}{S.RESET}")


def _preview(text: str, limit: int = 68) -> str:
    t = " ".join(text.split())
    return t if len(t) <= limit else t[:limit - 1] + "…"


def _status_icon(status: str) -> str:
    return {"ok": f"{S.GREEN}[✓]{S.RESET}",
            "error": f"{S.RED}[✗]{S.RESET}",
            "parsed": f"{S.YELLOW}[≈]{S.RESET}"}.get(status, f"{S.DIM}[–]{S.RESET}")


def build_report(info_file: FileInfo, structure: StructureReport, formulas: MockParSubResult,
                 env_lines: List[str], verbose: bool, max_detail: int) -> Tuple[str, int, int]:
    """渲染报告。返回 (报告文本, 结构问题数, 数学错误数)。

    分开返回是为了让退出码能区分两类问题：
    结构问题（孤儿标签/未定义引用）是排版卫生；数学错误是内容错误，严重性不同。
    """
    out: List[str] = []
    add = out.append
    n_ok, n_skip, n_err = formulas.verified, formulas.skipped, formulas.errors
    # 语法错误也算"结构问题"：它同样会阻止编译成功
    n_struct = (len(structure.orphans) + len(structure.undefined)
                + len(getattr(structure, "syntax_errors", []) or []))
    problems = n_struct + n_err

    add(_TITLE)
    add(f"{S.BOLD}{'MATH GUARD  ·  LaTeX 结构 + 公式体检报告':^{WIDTH}}{S.RESET}")
    add(f"{S.DIM}{'本地运行 · 只读检查 · 不会修改任何文件 · 版本 ' + __version__:^{WIDTH}}{S.RESET}")
    add(RULE)

    add(f"{S.BOLD}{S.CYAN}▌ 一、文件基本信息  FILE OVERVIEW{S.RESET}")
    add(THIN)
    rows = [
        ("源文件", info_file.path),
        ("文件大小", f"{info_file.size:,} 字节"),
        ("总行数", f"{info_file.lines} 行"),
        ("文本编码", info_file.encoding),
        ("最后修改", info_file.mtime),
        ("段落 / 引用数", f"{info_file.paragraphs} 段 / {info_file.citations} 个 cite 命令"),
        ("检查时间", time.strftime("%Y-%m-%d %H:%M:%S")),
    ]
    for k, v in rows:
        add(f"  {S.BOLD}{k:<14}{S.RESET}{v}")
    add(f"  {S.BOLD}{'处理环境':<12}{S.RESET}" + "  |  ".join(env_lines))
    add("")

    add(f"{S.BOLD}{S.CYAN}▌ 二、结构检查结果  STRUCTURE CHECK{S.RESET}")
    add(THIN)
    total_labels = sum(len(v) for v in structure.labels.values())
    add(f"  标签 label     : {S.BOLD}{total_labels}{S.RESET} 个定义（{len(structure.labels)} 个不同键）")
    add(f"  引用 ref       : {S.BOLD}{len(structure.refs)}{S.RESET} 处"
        f"（覆盖 {len({_strip_prefix(r.key) for r in structure.refs})} 个不同标签）")
    add(f"  孤儿标签       : {S.YELLOW if structure.orphans else S.GREEN}{S.BOLD}{len(structure.orphans)}"
        f"{S.RESET}      未定义引用 : {S.RED if structure.undefined else S.GREEN}"
        f"{S.BOLD}{len(structure.undefined)}{S.RESET}")
    if structure.duplicate:
        add(f"  {S.YELLOW}重复定义标签   : {len(structure.duplicate)} 个 —— "
            + "、".join(structure.duplicate[:6]) + ("…" if len(structure.duplicate) > 6 else "") + S.RESET)
    # 语法层检查（模块三 / 七）
    syn = getattr(structure, "syntax_errors", []) or []
    if syn:
        add(f"  {S.RED}语法问题       : {len(syn)} 处（括号/环境未配对，或必填参数为空）{S.RESET}")
    else:
        add(f"  语法问题       : {S.GREEN}0 处（环境与括号均配对）{S.RESET}")
    add("")

    if syn:
        add(f"  {S.RED}▲ 语法错误（确定的笔误，必须修）{S.RESET}")
        for e in syn[:max_detail]:
            loc = f"{e.file}:{e.line}" if e.file else f"L{e.line}"
            add(f"    {loc:<24} {e.message}")
        if len(syn) > max_detail:
            add(f"    {S.DIM}… 其余 {len(syn) - max_detail} 处已省略{S.RESET}")
        add("")
    # 警告类（不影响编译，但影响规范性）
    wsyn = getattr(structure, "syntax_warns", []) or []
    if wsyn:
        add(f"  {S.YELLOW}▲ 排版警告（{len(wsyn)} 条，不影响编译）{S.RESET}")
        for w in wsyn[:max_detail]:
            loc = f"{w.file}:{w.line}" if w.file else f"L{w.line}"
            add(f"    {loc:<24} {w.message}")
        if len(wsyn) > max_detail:
            add(f"    {S.DIM}… 其余 {len(wsyn) - max_detail} 条已省略{S.RESET}")
        add("")

    if structure.orphans:
        add(f"  {S.YELLOW}▲ 孤儿标签（定义了但从未被引用）{S.RESET}")
        for d in structure.orphans[:max_detail]:
            loc = f"{d.file}:{d.line}" if d.file else f"L{d.line}"
            ctx = f"  {S.DIM}<- {d.context}{S.RESET}" if d.context else ""
            add(f"    {loc:<22} label: {d.key}{ctx}")
        if len(structure.orphans) > max_detail:
            add(f"    {S.DIM}… 其余 {len(structure.orphans) - max_detail} 条已省略{S.RESET}")
        add("")
    if structure.undefined:
        add(f"  {S.RED}▲ 未定义引用（引用了不存在的 label）{S.RESET}")
        for r in structure.undefined[:max_detail]:
            loc = f"{r.file}:{r.line}" if r.file else f"L{r.line}"
            add(f"    {loc:<22} {r.command}{{{r.key}}}")
        if len(structure.undefined) > max_detail:
            add(f"    {S.DIM}… 其余 {len(structure.undefined) - max_detail} 条已省略{S.RESET}")
        add("")
    if structure.disputed:
        add(f"  {S.YELLOW}▲ 两工具结论不一致（latexnav 判未定义 / 内置解析器判已定义）{S.RESET}")
        for r in structure.disputed[:max_detail]:
            add(f"    {r.key}  {S.DIM}→ 常见原因：latexnav 只统计「所在元素」内的引用，"
                f"同段引用会漏计；建议人工确认{S.RESET}")
        if len(structure.disputed) > max_detail:
            add(f"    {S.DIM}… 其余 {len(structure.disputed) - max_detail} 条已省略{S.RESET}")
        add("")
    if not structure.orphans and not structure.undefined and not structure.disputed:
        add(f"  {S.GREEN}✓ 所有标签都被引用，所有引用都有对应标签——结构完全自洽。{S.RESET}")
    add("")

    add(f"{S.BOLD}{S.CYAN}▌ 三、公式验证结果  FORMULA VERIFICATION{S.RESET}")
    add(THIN)
    n_parsed = getattr(formulas, "parsed", 0)
    add(f"  共提取公式 : {S.BOLD}{formulas.total}{S.RESET} 条"
        f"    {S.DIM}数据来源：{formulas.source} / sympy{S.RESET}")
    add(f"  {S.GREEN}已验证通过{S.RESET} / {S.RED}验证失败{S.RESET} / "
        f"{S.YELLOW}仅可解析{S.RESET} / {S.DIM}无法验证{S.RESET} : "
        f"{S.GREEN}{n_ok}{S.RESET} / {S.RED}{n_err}{S.RESET} / {S.YELLOW}{n_parsed}{S.RESET} / "
        f"{S.DIM}{n_skip}{S.RESET}")
    add(f"  {S.DIM}四态说明：③「仅可解析」= 能读懂但没有任何可判定的数字关系"
        f"（表格数据、纯表达式、只含符号的等式），{S.BOLD}不冒充已验证{S.RESET}")
    if formulas.total:
        real = n_ok + n_err
        add(f"  真正做了判定的比例 : {100.0 * real / max(1, formulas.total):.0f}%"
            f"  {S.DIM}（分母为全部提取到的公式）{S.RESET}")
    add("")

    if n_err:
        add(f"  {S.RED}▲ 验证失败（数字关系确定不成立，需人工复核）{S.RESET}")
        for d in [d for d in formulas.details if d.status == "error"][:max_detail]:
            add(f"    L{d.line:<5} {_status_icon(d.status)} {_preview(d.latex, 70)}")
            add(f"           {S.RED}-> {d.reason}{S.RESET}")
        add("")

    if n_ok:
        add(f"  {S.GREEN}▲ 已验证通过（做过确定的数字判定，前 {min(n_ok, max_detail)} 条）{S.RESET}")
        for d in [d for d in formulas.details if d.status == "ok"][:max_detail]:
            extra = f"{d.variables} 个符号" if d.variables else "纯数字关系"
            add(f"    L{d.line:<5} {_status_icon(d.status)} {_preview(d.latex, 66)}  {S.DIM}({extra}){S.RESET}")
        if n_ok > max_detail:
            add(f"    {S.DIM}… 其余 {n_ok - max_detail} 条已省略{S.RESET}")
        add("")

    if verbose and n_parsed:
        add(f"  {S.YELLOW}▲ 仅可解析、未验证（{n_parsed} 条，详细模式）{S.RESET}")
        for d in [d for d in formulas.details if d.status == "parsed"][:max_detail]:
            add(f"    L{d.line:<5} {_status_icon(d.status)} {_preview(d.latex, 62)}")
            add(f"           {S.DIM}-> {d.reason}{S.RESET}")
        if n_parsed > max_detail:
            add(f"    {S.DIM}… 其余 {n_parsed - max_detail} 条已省略{S.RESET}")
        add("")

    if verbose and n_skip:
        add(f"  {S.DIM}▲ 无法验证（{n_skip} 条，详细模式）{S.RESET}")
        for d in [d for d in formulas.details if d.status == "skip"][:max_detail]:
            add(f"    L{d.line:<5} {_status_icon(d.status)} {_preview(d.latex, 62)}")
            add(f"           {S.DIM}-> {d.reason}{S.RESET}")
        if n_skip > max_detail:
            add(f"    {S.DIM}… 其余 {n_skip - max_detail} 条已省略{S.RESET}")
        add("")

    for w in formulas.warnings:
        add(f"  {S.YELLOW}ℹ {w}{S.RESET}")
    if formulas.warnings:
        add("")

    add(RULE)
    add(f"{S.BOLD}{S.CYAN}▌ 总结  SUMMARY{S.RESET}")
    add(THIN)
    verdict = (f"{S.GREEN}✓ 体检通过{S.RESET}" if problems == 0
               else f"{S.RED}✗ 发现 {problems} 处需要处理的问题{S.RESET}")
    add(f"  结论 : {verdict}")
    add(f"  结构 : {S.GREEN}{len(structure.labels)} 标签{S.RESET} / "
        f"{S.YELLOW}{len(structure.orphans)} 孤儿{S.RESET} / "
        f"{S.RED}{len(structure.undefined)} 未定义引用{S.RESET}")
    add(f"  公式 : {formulas.total} 条 —— 已验证 {n_ok} / 验证失败 {n_err} / "
        f"仅可解析 {n_parsed} / 无法验证 {n_skip}")
    add(f"  {S.DIM}只有「验证失败」是确定错误；「仅可解析」不代表正确，只是没发现问题。{S.RESET}")
    add(RULE)
    return "\n".join(out), n_struct, n_err


# ######################################################################
# 模块四：主流程
# ######################################################################


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="math_guard.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Local LaTeX checker: orphan labels, undefined refs, and formula self-consistency.",
        epilog=(
            "examples:\n"
            "  python math_guard.py paper.tex\n"
            "  python math_guard.py paper.tex -v\n"
            "  python math_guard.py paper.tex --no-color > report.txt\n"
            "exit codes: 0 = clean, 1 = issues found, 2 = usage error"
        ),
    )
    p.add_argument("file", nargs="?", help="path to the .tex file")
    p.add_argument("--demo", action="store_true", help="generate a sample .tex with problems and check it")
    p.add_argument("--batch", nargs="+", metavar="PATH",
                   help="check many files/directories in one run (much faster than one call per file)")
    p.add_argument("-q", "--quiet", action="store_true",
                   help="with --batch: one line per file instead of full reports")
    p.add_argument("--parsub-worker", nargs=4, metavar=("TEX", "OUT", "VERIFY", "TASK_TIMEOUT"),
                   help=argparse.SUPPRESS)  # 内部使用：parsub 子进程入口
    p.add_argument("-v", "--verbose", action="store_true", help="list every math item, including skipped ones")
    p.add_argument("--lint", action="store_true",
                   help="also check typography (missing punctuation at end of display formulas)")
    p.add_argument("--no-color", action="store_true", help="disable ANSI colors")
    p.add_argument("--max-detail", type=int, default=12, metavar="N", help="max items shown per list (default 12)")
    p.add_argument("--debug", action="store_true", help="show full traceback on unexpected errors")
    p.add_argument("--version", action="version", version=f"math_guard {__version__}")
    return p


# 演示用示例文档：故意包含 1 个孤儿标签、1 个未定义引用、1 条错误数字等式
_DEMO_TEX = r"""\documentclass{article}
\begin{document}
\section{演示}
这个公式是对的：$a_n = \frac{p-1}{2}$，且 $x^{2}+3y^{2} \ge 0$。
这个公式是错的：$1 = 2$。

\begin{equation}\label{eq:good}
  \binom{3r}{r} \equiv \frac{2p-1}{2} \pmod{p}
\end{equation}
见 \eqref{eq:good}，但 \ref{eq:missing} 这个标签并不存在。

\begin{equation}\label{eq:orphan}
  a_n \equiv b_n \equiv 0
\end{equation}

\begin{align*}
  (1+2)(3+1) &= 12 \\
  \sum_{k=0}^{n} k &= \frac{n(n+1)}{2}
\end{align*}
\end{document}
"""


def _merge_formulas(builtin: MockParSubResult, sub: MockParSubResult) -> Tuple[MockParSubResult, str]:
    """把 parsub 的验证结论合并进内置结果。

    规则：
      · 以内置结果为主干（它覆盖全部公式）；
      · parsub 只要给出明确对错（ok/error，即 identity_holds_numerically 为真/假），
        就用它覆盖同一条公式的判定，并在结论后标注「parsub 数值验证」；
      · parsub 的 skip 一律忽略（它只做了符号化/求解，没有对错结论，覆盖反而降级）。
    """
    by_latex = {}
    for d in sub.details:
        key = re.sub(r"\s+", "", d.latex)
        if d.status in ("ok", "error") and key:
            by_latex[key] = d

    merged = MockParSubResult(source="内置 sympy + parsub 数值验证")
    upgraded = 0
    for d in builtin.details:
        key = re.sub(r"\s+", "", d.latex)
        alt = by_latex.get(key)
        if alt is not None:
            new = CheckDetail(latex=d.latex, status=alt.status,
                              reason=f"{alt.reason}（parsub 数值验证）",
                              line=d.line, kind=d.kind, variables=d.variables,
                              relations=d.relations, critical=d.critical)
            if alt.status != d.status:
                upgraded += 1
            merged.details.append(new)
        else:
            merged.details.append(d)

    merged.total = len(merged.details)
    merged.verified = sum(x.status == "ok" for x in merged.details)
    merged.skipped = sum(x.status == "skip" for x in merged.details)
    merged.errors = sum(x.status == "error" for x in merged.details)
    merged.parsed = sum(x.status == "parsed" for x in merged.details)
    merged.warnings = list(builtin.warnings) + list(sub.warnings)
    merged.workdir = sub.workdir

    if by_latex:
        note = f"公式验证: 内置 sympy（{builtin.total} 条）+ parsub 数值验证（{len(by_latex)} 条有结论"
        note += f"，其中 {upgraded} 条与内置判定不同）"
    else:
        note = "公式验证: 内置 sympy（parsub 本次未给出任何对错结论，已忽略其 skip 项）"
    return merged, note


def _cross_check(nav: StructureReport, builtin: StructureReport) -> Tuple[StructureReport, str]:
    """把 latexnav 的结果与内置解析器的结果取并集，并如实标注两边不一致之处。

    为什么不能直接用 latexnav？它是「按章节/定理环境」组织标签的，\\label 归到最近
    的章节元素上，于是「同一段落里引用了本环境的标签」会被漏计——等式标签 eq:* 尤其
    容易中招，导致 eq:* 被误报成「未定义引用」。实测在 an_midpoint_详解.tex 上，
    latexnav 报 6 个缺失引用，实际一个都没有（它们都在文档里定义了，只是引用与定义
    同段）。

    而内置解析器直接统计整个文件的 \\label / \\ref，没有这个盲区，所以：
      · 以「并集」为最终结论（宁可人工复核，也不漏报）；
      · 在返回的说明里写明两边各自报了多少、差异是什么，绝不掩盖。
    """
    merged = StructureReport(source="latexnav+builtin（交叉校验）")
    merged.labels = builtin.labels or {}
    merged.refs = builtin.refs
    merged.cite_keys = builtin.cite_keys
    merged.duplicate = builtin.duplicate

    def keys(items: Iterable[Any]) -> set:
        return {_strip_prefix(getattr(i, "key", "")) for i in items}

    # 孤儿标签：latexnav 按「章节/定理元素」聚合引用，判定更准，故以它为主、
    # 内置解析器只补充它没看到的（并集）。
    merged.orphans = list(nav.orphans)
    for d in builtin.orphans:
        if _strip_prefix(d.key) not in keys(merged.orphans):
            merged.orphans.append(d)
    merged.orphans.sort(key=lambda d: d.line)

    # 未定义引用：latexnav 会把「同段引用」漏掉而误判，内置解析器覆盖全文件，以它为准。
    merged.undefined = list(builtin.undefined)
    merged.undefined.sort(key=lambda r: r.line)

    # 两边不一致的部分单独记录下来，如实呈现，而不是悄悄丢掉。
    # ⚠ 只把「内置解析器确实见过这个 \ref」的条目标为待确认。
    #   如果内置解析器根本没见到这个引用（例如它引用了别的文件里的标签，或跨文件
    #   \input 场景），那 latexnav 的"未定义"就是对的，应直接算硬错误——否则会漏报。
    seen_by_builtin = {_strip_prefix(r.key) for r in builtin.refs}
    nav_only = [r for r in nav.undefined
                if _strip_prefix(r.key) not in keys(builtin.undefined)
                and _strip_prefix(r.key) in seen_by_builtin]
    merged.disputed = nav_only
    # 兜底：内置解析器没见到这个 \ref（跨文件引用、或 latexnav 自己的解析差异），
    # 而 latexnav 明确说未定义 —— 同样计入待确认，绝不为了"少报错"而漏掉真实问题。
    nav_unseen = [r for r in nav.undefined
                  if _strip_prefix(r.key) not in keys(builtin.undefined)
                  and _strip_prefix(r.key) not in seen_by_builtin]
    merged.disputed.extend(nav_unseen)
    note = "结构检查: latexnav + 内置解析器交叉校验"
    if merged.disputed:
        note += (f"；其中 {len(merged.disputed)} 个引用的归属两工具判断不同，"
                 f"已列为「待确认」而非硬错误")
    return merged, note


def _run_demo() -> int:
    """--demo：在系统临时目录生成示例 .tex 并直接体检（方便验证安装是否正常）。"""
    import tempfile

    path = os.path.join(tempfile.gettempdir(), "math_guard_demo.tex")
    with open(path, "w", encoding="utf-8") as f:
        f.write(_DEMO_TEX)
    print(f"{S.DIM}已生成示例文件：{path}{S.RESET}\n")
    return main([path, "--max-detail", "20"])


def _count_paragraphs(text: str) -> int:
    body = mask_comments(text)
    n = 0
    for block in re.split(r"\n\s*\n", body):
        s = block.strip()
        if len(s) >= 30 and not s.startswith("\\"):
            n += 1
    return n


def _chapter_file_warning(path: Path, doc: TexDocument) -> str:
    r"""判断被检查的文件是不是「章节文件」而非主文件。

    返回提示文字（空串表示看起来是主文件）。

    判据（保守，只在很确定时才提示）：
      · 文件里**没有** \documentclass
      · 且同目录下**存在**含 \documentclass 且 \input/\include 了本文件的 .tex
    """
    try:
        raw = doc.files[0][1] if doc.files else doc.text
    except Exception:
        raw = doc.text
    if "\\documentclass" in raw:
        return ""
    stem = path.stem
    parent = path.parent
    # 同级目录 + 父目录（章节常放在 sections/ 子目录，主文件在上一层）
    candidates = []
    for d in (parent, parent.parent):
        try:
            candidates.extend(p for p in d.iterdir()
                              if p.suffix.lower() == ".tex" and p.resolve() != path.resolve())
        except OSError:
            continue
    for sib in candidates:
        try:
            if sib.stat().st_size > 4_000_000:
                continue
            # ⚠ 必须读**原始字节**，不能用 read_tex：后者会展开 \input，
            #    合并后的文本里再也找不到 "\input{sections/xxx}" 这条语句，
            #    导致检测永远不触发。
            raw_txt, _enc = _decode_bytes(sib.read_bytes(), str(sib))
        except Exception:
            continue
        if "\\documentclass" not in raw_txt:
            continue
        # \input 可能带路径（sections/dataset_constructing.tex 或 sections/xxx）
        pat = (r"\\(?:input|include|subfile)\s*\{\s*(?:[^{}]*[/\\])?"
               + re.escape(stem) + r"(?:\.tex)?\s*\}")
        if re.search(pat, raw_txt):
            return f"「{path.name}」看起来是章节文件，主文件是「{sib.name}」"
    return ""


def _collect_tex_files(paths: List[str]) -> List[Path]:
    """把命令行给的路径展开成 .tex 文件列表（目录则递归查找）。"""
    from pathlib import Path
    skip_dirs = {"node_modules", ".git", "__pycache__", "_internal", "build",
                 "dist", "site-packages", ".venv", "venv"}
    out: List[Path] = []
    for raw in paths:
        p = Path(raw).expanduser()
        if p.is_file() and p.suffix.lower() == ".tex":
            out.append(p)
        elif p.is_dir():
            for dp, dns, fns in os.walk(p):
                dns[:] = [d for d in dns if d not in skip_dirs]
                for fn in fns:
                    if fn.lower().endswith(".tex"):
                        out.append(Path(dp) / fn)
    # 去重并按路径排序，输出稳定
    seen = set()
    uniq = []
    for p in out:
        try:
            key = p.resolve()
        except OSError:
            key = p
        if key in seen:
            continue
        seen.add(key)
        uniq.append(p)
    return sorted(uniq, key=lambda x: str(x).lower())


def _run_batch(paths: List[str], lint: bool = False, quiet: bool = False) -> int:
    """批处理：一次进程内检查多个文件，共享 sympy 加载开销。

    为什么需要：单文件跑一次约 2 秒，其中 ~0.7 秒是 import sympy 的固定成本。
    逐篇调用 200 篇要 400 秒；本函数只付一次启动成本。

    实现方式：递归调用 main() 自身，复用完整的单文件流程（不重复实现逻辑）。
    批处理时 sys.stdout 被重定向到收集器，因此每篇的报告会被捕获。
    """
    files = _collect_tex_files(paths)
    if not files:
        print(f"{S.RED}✘ 没有找到任何 .tex 文件{S.RESET}")
        return EXIT_USAGE

    t0 = time.time()
    print(f"{S.DIM}批处理：{len(files)} 个文件"
          f"{'（--lint 已开启）' if lint else ''}{S.RESET}")

    # 预加载 sympy：只付一次成本
    _sympy_available()

    real_stdout = sys.stdout
    results = []
    for idx, path in enumerate(files, 1):
        buf: List[str] = []
        t_file = time.time()
        try:
            sys.stdout = _TeeWriter(buf)
            code = main([str(path)] + (["--lint"] if lint else [])
                        + ["--no-color", "--max-detail", "4"])
        except SystemExit as e:
            code = int(e.code) if isinstance(e.code, int) else EXIT_INTERNAL
        except Exception as exc:
            code = EXIT_INTERNAL
            buf.append(f"内部错误 {type(exc).__name__}: {exc}")
        finally:
            sys.stdout = real_stdout
        dt = time.time() - t_file
        text = "".join(buf)

        summary = ""
        for line in text.splitlines():
            s = " ".join(line.split())
            if s.startswith("语法问题"):
                v = s.split(":", 1)[1].strip()
                if not v.startswith("0 处"):
                    summary += f"语法{v[:4]}|"
            elif s.startswith("孤儿标签"):
                m_ = re.search(r"孤儿标签\s*:\s*(\d+)", s)
                if m_ and m_.group(1) != "0":
                    summary += f"孤儿{m_.group(1)}|"
            elif s.startswith("已验证通过 /"):
                parts = s.split(":", 1)[1].strip().split("/")
                if len(parts) >= 2 and parts[1].strip() != "0":
                    summary += f"失败{parts[1].strip()}"
        results.append((path, code, summary.strip("|"), dt, text))

        if quiet:
            mark = {0: f"{S.GREEN}OK  {S.RESET}", 1: f"{S.YELLOW}结构{S.RESET}",
                    3: f"{S.RED}错误{S.RESET}"}.get(code, f"{S.RED}!{code} {S.RESET}")
            print(f"  [{idx}/{len(files)}] {mark} {dt:5.1f}s  {path.name:<32} {summary}")
        else:
            print()
            print(f"{S.BOLD}{'─' * 66}{S.RESET}")
            print(f"{S.BOLD}[{idx}/{len(files)}] {path}{S.RESET}")
            print(text.rstrip())
        if idx % 25 == 0:
            print(f"{S.DIM}  … {idx}/{len(files)}，累计 {time.time()-t0:.0f}s{S.RESET}",
                  flush=True)

    buckets: Dict[int, List] = {0: [], 1: [], 2: [], 3: [], 4: []}
    for path, code, summary, dt, _t in results:
        buckets.setdefault(code, []).append((path, summary))

    total = time.time() - t0
    print()
    print(f"{S.BOLD}{'═' * 66}{S.RESET}")
    print(f"{S.BOLD}批处理汇总{S.RESET}  {len(files)} 个文件 / {total:.1f}s"
          f"  （平均 {total/max(1,len(files)):.2f}s/篇）")
    print(f"{S.BOLD}{'═' * 66}{S.RESET}")
    print(f"  {S.GREEN}干净       {len(buckets.get(0, []))}{S.RESET}")
    print(f"  {S.YELLOW}仅结构问题 {len(buckets.get(1, []))}{S.RESET}")
    print(f"  {S.RED}数学错误   {len(buckets.get(3, []))}{S.RESET}")
    nbad = len(buckets.get(2, [])) + len(buckets.get(4, []))
    if nbad:
        print(f"  用法/内部  {nbad}")

    for label, key in (("数学错误", 3), ("内部错误", 4), ("用法错误", 2)):
        items = buckets.get(key, [])
        if items:
            print(f"\n  {S.RED}▲ {label}{S.RESET}")
            for p, s in items[:15]:
                print(f"    {p.name:<34} {s}")
            if len(items) > 15:
                print(f"    {S.DIM}… 其余 {len(items) - 15} 个已省略{S.RESET}")

    if buckets.get(2):
        return EXIT_USAGE
    if buckets.get(3):
        return 3
    if buckets.get(1):
        return 1
    return 0


class _TeeWriter:
    """收集 print 输出到列表（批处理时替代真实 stdout）。"""

    def __init__(self, sink: List[str]):
        self.sink = sink

    def write(self, s: str) -> int:
        self.sink.append(s)
        return len(s)

    def flush(self) -> None:
        pass

    def isatty(self) -> bool:
        return False


def main(argv: Optional[Sequence[str]] = None) -> int:
    global S
    _init_console()
    args = _build_parser().parse_args(list(argv) if argv is not None else None)
    S = _Style.auto(force_off=args.no_color)

    if args.demo:
        return _run_demo()

    # 批处理模式：一次启动跑多个文件/目录，共享 sympy 加载开销。
    # 单篇 2 秒里有 ~0.7 秒是 import sympy 的固定成本；跑 200 篇时
    # 逐篇调用要 400 秒，而一次启动只要 ~60 秒。
    if args.batch:
        return _run_batch(args.batch, lint=args.lint, quiet=args.quiet)

    # 隐藏子命令：parsub 在独立进程中执行（由 _run_parsub_isolated 调用）
    if args.parsub_worker:
        return _parsub_worker(args.parsub_worker[0], args.parsub_worker[1],
                              args.parsub_worker[2] == "1", int(args.parsub_worker[3]))

    if not args.file:
        print(f"{S.RED}✘ 缺少参数{S.RESET}：请提供 .tex 文件路径")
        print(f"  用法: python math_guard.py <file.tex>   {S.DIM}(-h 查看全部参数){S.RESET}")
        return EXIT_USAGE

    from pathlib import Path
    path = Path(args.file).expanduser()
    if not path.exists():
        print(f"{S.RED}✘ 文件不存在{S.RESET}：{args.file}")
        print(f"  {S.DIM}检查路径拼写；路径含空格/中文时请用引号包住{S.RESET}")
        return EXIT_USAGE
    if not path.is_file():
        print(f"{S.RED}✘ 不是文件{S.RESET}：{args.file}（是一个目录？）")
        return EXIT_USAGE

    # PDF 必须明确拒绝，不能"按文本硬解"：
    #   实测把 PDF 文字层直接当 LaTeX 解析，会得到 0 条公式，
    #   然后报告「体检通过」——什么都没检查却说通过，是最坏的结果。
    if path.suffix.lower() == ".pdf":
        print(f"{S.RED}✘ 不支持 PDF 输入{S.RESET}：{path.name}")
        print(f"  {S.DIM}PDF 里的公式是「排版后的碎片」：分式横线、上下标位置在提取时全部丢失，")
        print(f"  例如 \\binom{{2j}}{{j}}^2/16^j 会被拆成 (2j / j ) / 2 / 16j 几行，无法还原成 LaTeX。")
        print(f"  强行解析会得到错误结果，故本工具拒绝，而不是给你一个假的『通过』。{S.RESET}")
        print(f"\n  请改用 .tex 源码：")
        print(f"    · 你有份稿：直接跑它，例如  math_guard.exe 你的论文.tex")
        print(f"    · 多文件项目：指向主文件，例如 main.tex（会自动展开所有 \\input）")
        print(f"    · 只有别人给的 PDF：向作者索要 .tex")
        return EXIT_USAGE

    if path.suffix.lower() not in (".tex", ".ltx", ".latex"):
        print(f"{S.YELLOW}⚠ 警告{S.RESET}：{path.name} 的扩展名不是 .tex，仍将按 LaTeX 文本尝试解析")

    try:
        doc = read_tex(str(path))
    except Exception as exc:
        print(f"{S.RED}✘ 无法读取文件{S.RESET}：{_friendly_error(exc)}")
        return EXIT_USAGE

    # 乱码防护：编码认不出时，后续扫描会静默失败却仍报「通过」——必须拦住。
    if "\ufffd" in doc.text:
        bad = doc.text.count("\ufffd")
        print(f"{S.RED}✘ 文件编码无法识别{S.RESET}：检测到 {bad} 处乱码字符（U+FFFD）。")
        print(f"  {S.DIM}该文件可能使用了 GBK/Big5/UTF-16 等编码，或本身已损坏。")
        print(f"  在编辑器里确认编码后另存为 UTF-8，再重新运行。{S.RESET}")
        return EXIT_USAGE

    _, sympy_desc = _sympy_available()
    env_lines: List[str] = [sympy_desc, "超长整数保护 ✔" if LONGINT_SAFE else "超长整数保护 —"]

    # ---------- 前置守卫：是否误传了章节文件 ----------
    # 多文件项目里，\label 常在别的章节、\ref 在本章节。若把章节文件当主文件，
    # 未定义引用/孤儿标签全是假的。
    # 实证（arXiv:2303.09095）：把 dataset_constructing.tex 当主文件，报 2 处
    # 未定义引用，而这两个 label 分别定义在 dataset.tex 和 PaperForReview.tex 里。
    sub_note = _chapter_file_warning(path, doc)
    if sub_note:
        print(f"{S.YELLOW}⚠ {sub_note}{S.RESET}")
        print(f"  {S.DIM}本章节单独看时，引用/标签的统计结果不可信，请指向主文件。{S.RESET}")
        print()

    # ---------- 模块一：结构检查（latexnav 与内置解析器交叉校验）----------
    try:
        builtin_report: Optional[StructureReport] = builtin_structure(doc)
    except Exception as exc:
        builtin_report = None
        env_lines.append(f"内置解析器异常已隔离（{type(exc).__name__}）")

    # ---------- 语法层检查（环境配对 / 括号配对）----------
    try:
        syn_errors, syn_warns = syntax_check(doc, lint=args.lint)
    except Exception as exc:
        syn_errors, syn_warns = [], []
        env_lines.append(f"语法检查异常已隔离（{type(exc).__name__}）")
    if builtin_report is not None:
        builtin_report.syntax_errors = syn_errors
        builtin_report.syntax_warns = syn_warns

    nav_report, nav_status = try_latexnav(str(path))
    env_lines.append("latexnav: " + nav_status)

    if nav_report is not None and builtin_report is not None:
        structure, cross_note = _cross_check(nav_report, builtin_report)
        env_lines.append(cross_note)
    elif nav_report is not None:
        structure = nav_report
        env_lines.append("结构检查: latexnav")
    elif builtin_report is not None:
        structure = builtin_report
        env_lines.append("结构检查: 内置解析器")
    else:
        structure = StructureReport()
        env_lines.append("结构检查: 无可用结果")

    # 语法检查结果与 latexnav 无关（纯文本层面的配对），无论走哪条路径都要挂上。
    # ⚠ 早期版本只赋给 builtin_report，而 _cross_check 返回的是新建对象，
    #    导致报告里永远显示"语法问题 0 处"——实际有错也没报出来。
    structure.syntax_errors = syn_errors
    structure.syntax_warns = syn_warns

    # ---------- 模块二：公式验证 ----------
    # 策略：内置 sympy 验证器覆盖全部公式，始终给出底数；parsub 提供「数学等价性」的
    # 数值判定，只有当它真的拿到结论（identity_holds_numerically 为真/假）时才接管该项。
    # 原因：parsub 的执行阶段在真实论文上极慢（40 个任务中含 24 个绘图，实测 300 秒
    # 都跑不完），而它的"提取"只覆盖部分公式；两者结合才能既全又深。
    builtin_formulas = mock_parsub_extract_and_verify(str(path), doc=doc)

    sub_result, sub_status = try_parsub(str(path))
    env_lines.append("parsub: " + sub_status)
    if sub_result is not None and sub_result.details:
        formulas, merge_note = _merge_formulas(builtin_formulas, sub_result)
        env_lines.append(merge_note)
        if sub_result.workdir:
            env_lines.append(f"parsub 输出目录: {sub_result.workdir}")
    else:
        formulas = builtin_formulas
        env_lines.append("公式验证: 内置 sympy 实现")

    try:
        stat = path.stat()
        info = FileInfo(
            path=str(path.resolve()),
            size=doc.size or stat.st_size,
            lines=doc.text.count("\n") + (1 if doc.text and not doc.text.endswith("\n") else 0),
            encoding=doc.encoding,
            mtime=time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stat.st_mtime)),
            paragraphs=_count_paragraphs(doc.text),
            citations=structure.cite_keys,
        )
    except Exception:
        info = FileInfo(str(path), 0, 0, "?", "?", 0, 0)

    try:
        report, n_struct, n_math = build_report(info, structure, formulas, env_lines,
                                                args.verbose, max(1, args.max_detail))
    except Exception as exc:
        print(f"{S.RED}✘ 报告渲染失败，以下为降级输出{S.RESET}：{_friendly_error(exc)}")
        print(f"孤儿标签 {len(structure.orphans)}，未定义引用 {len(structure.undefined)}，"
              f"公式 {formulas.total}（失败 {formulas.errors}）")
        if formulas.errors:
            return EXIT_MATH_ERROR
        return EXIT_ISSUES if (structure.orphans or structure.undefined) else EXIT_OK

    print(report)
    # 数学错误优先于结构问题：前者是内容错了，后者是排版卫生
    if n_math:
        return EXIT_MATH_ERROR
    if n_struct:
        return EXIT_ISSUES
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(safe_main())

# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置（onedir），产出两个 exe 共用一个 _internal 目录。

产物目录 dist/math_guard/：
    math_guard.exe        命令行版
    MathGuard-GUI.exe     图形界面版（双击运行，拖拽即用）
    example.tex           示例
    _internal/            两个 exe 共用的运行库

为什么用 onedir：onefile 每次运行都要解压到临时目录，实测在本机出现
"Could not create temporary directory!"（临时目录受限/杀软拦截时不可靠）。
"""
import os

from PyInstaller.utils.hooks import collect_submodules

hiddenimports = [
    "latex_macros",
    "latexnav",
    "sympy",
    "sympy.parsing.sympy_parser",
    "sympy.core",
    "sympy.functions",
    "sympy.simplify",
]
hiddenimports += [m for m in collect_submodules("sympy")
                  if ".tests" not in m and not m.startswith("sympy.testing")]
# GUI 版需要 tkinter（不能像旧配置那样排除）
hiddenimports += ["tkinter", "tkinter.ttk", "tkinter.filedialog",
                  "tkinter.messagebox", "tkinter.font"]

EXCLUDES = [
    # ---- 体积大头（实测 _internal 里合计约 40 MB）----
    # PIL / lxml：本工具完全用不到，只是被某些库间接拉进来的。
    "PIL", "Pillow", "lxml", "yaml",
    # python-flint：sympy 的可选整数后端，打包后占 21.5 MB
    #   （python_flint.libs 16.2 + flint 5.2）。
    #   实测换成纯 python 后端**反而更快**、结果完全一致：
    #       flint  : 提取 2488 条，合计 1.59 s
    #       python : 提取 2488 条，合计 1.50 s
    #   原因：本工具 99% 的工作是纯数字比较，用不上 flint 的大整数加速。
    "flint", "python_flint", "gmpy2",
    # OpenSSL：只有 requests/urllib 的 https 才需要；本工具完全离线。
    #   （"完全离线"是产品承诺，砍掉它同时也是在代码层面兑现承诺）
    # 注：不在此处排除 ssl —— tkinter/urllib 的导入链上可能出现它，留着更安全。
    # ---- 科学计算与测试框架 ----
    "parsub", "numpy", "scipy", "matplotlib", "pandas",
    "openpyxl", "fastapi", "uvicorn", "starlette", "typer", "rich",
    "IPython", "pytest", "PyQt5", "PySide2", "PySide6",
    "mpmath.tests", "sympy.testing",
]

a1 = Analysis(
    ["math_guard.py"],
    pathex=[os.getcwd()],
    binaries=[], datas=[],
    hiddenimports=hiddenimports,
    hookspath=[], hooksconfig={}, runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False, optimize=0,
)
pyz1 = PYZ(a1.pure)

exe_cli = EXE(pyz1, a1.scripts, [],
              exclude_binaries=True,
              name="math_guard",
              console=True, debug=False, strip=False, upx=False,
              bootloader_ignore_signals=False)

a2 = Analysis(
    ["math_guard_gui.py"],
    pathex=[os.getcwd()],
    binaries=[], datas=[],
    hiddenimports=hiddenimports,
    hookspath=[], hooksconfig={}, runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False, optimize=0,
)
pyz2 = PYZ(a2.pure)

exe_gui = EXE(pyz2, a2.scripts, [],
              exclude_binaries=True,
              name="MathGuard-GUI",
              console=False, debug=False, strip=False, upx=False,
              bootloader_ignore_signals=False)

coll = COLLECT(
    exe_cli, a1.binaries, a1.datas,
    exe_gui, a2.binaries, a2.datas,
    strip=False, upx=False,
    name="math_guard",
)

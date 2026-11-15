# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置 —— 产出两个文件，职责分明。

用户反馈「文件夹里有两个 exe，小写的那个双击打不开」。
问题不是"有两个程序"，而是"两个都叫 exe，看不出区别"。

现在的方案：
  · MathGuard.exe            GUI 版（console=False），双击打开窗口
  · 命令行版.bat             指向同一个程序的批处理，双击会让你输入路径

关键：**两个入口共用一份 _internal**，体积几乎不增加。
MathGuard.exe 检测到命令行参数时会走 math_guard.main()，像命令行版一样工作。

打包模式：onedir（不是 onefile）
  onefile 每次运行都要解压到临时目录，实测出现
  "Could not create temporary directory!"（临时目录受限/杀软拦截）。
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
    "tkinter", "tkinter.ttk", "tkinter.filedialog",
    "tkinter.messagebox", "tkinter.font",
]
hiddenimports += [m for m in collect_submodules("sympy")
                  if ".tests" not in m and not m.startswith("sympy.testing")]

EXCLUDES = [
    "PIL", "Pillow", "lxml", "yaml",
    # python-flint：sympy 可选整数后端，占 21.5 MB；
    # 实测换成纯 python 后端**反而更快**（1.50s vs 1.59s）。
    "flint", "python_flint", "gmpy2",
    "parsub", "numpy", "scipy", "matplotlib", "pandas",
    "openpyxl", "fastapi", "uvicorn", "starlette", "typer", "rich",
    "IPython", "pytest", "PyQt5", "PySide2", "PySide6",
    "mpmath.tests", "sympy.testing",
]

a = Analysis(
    ["math_guard_gui.py"],
    pathex=[os.getcwd()],
    binaries=[], datas=[],
    hiddenimports=hiddenimports,
    hookspath=[], hooksconfig={}, runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False, optimize=0,
)
pyz = PYZ(a.pure)

# GUI 入口：双击无黑框；带参数时在内部转命令行模式
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="MathGuard",
    console=False,
    debug=False, strip=False, upx=False,
    bootloader_ignore_signals=False,
)

# 命令行入口：带控制台，能看到输出
exe_cli = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="MathGuard-命令行",
    console=True,
    debug=False, strip=False, upx=False,
    bootloader_ignore_signals=False,
)

coll = COLLECT(
    exe, exe_cli, a.binaries, a.datas,
    strip=False, upx=False,
    name="MathGuard",
)

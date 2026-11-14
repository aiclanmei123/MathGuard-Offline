# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置（onedir 模式）。

为什么用 onedir 而不是 onefile：
  onefile 每次运行都要把自己解压到临时目录。实测在本机出现
  "[PYI-xxxxx:ERROR] Could not create temporary directory!" —— 在临时目录受限、
  杀毒软件拦截、或用户目录带特殊字符的机器上很不可靠。
  onedir 不做自解压，启动更快、兼容性更好，代价是「一个文件夹」而非「单个 exe」。
  把整个文件夹压缩成 zip 发给别人即可，解压即用。
"""
from PyInstaller.utils.hooks import collect_submodules

hiddenimports = [
    "latex_macros",
    "latexnav",          # 纯标准库单文件模块，带进去可增强结构检查
    "sympy",
    "sympy.parsing.sympy_parser",
    "sympy.core",
    "sympy.functions",
    "sympy.simplify",
]
hiddenimports += [m for m in collect_submodules("sympy") if ".tests" not in m]

a = Analysis(
    ["math_guard.py"],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # 体积杀手：不打包科学计算全家桶（无 Python 机器上会自动降级为内置实现）
        "parsub", "numpy", "scipy", "matplotlib", "pandas",
        "openpyxl", "fastapi", "uvicorn", "starlette", "typer", "rich",
        "IPython", "pytest", "tkinter", "PyQt5", "PySide2",
        "mpmath.tests", "sympy.testing",
    ],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="math_guard",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="math_guard",
)

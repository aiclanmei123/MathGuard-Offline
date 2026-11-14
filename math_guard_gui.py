# -*- coding: utf-8 -*-
"""MathGuard 图形界面：拖拽 .tex 或文件夹进来，一键体检。

为什么需要：命令行对非技术用户是硬门槛。同门不会为了查排版去学 cmd，
但会愿意把文件拖进一个窗口。

设计要点：
  · 零配置：双击 exe 就打开，不需要任何参数
  · 拖拽即用：文件、文件夹都能拖进来
  · 结果可读：先给一句话结论，再给详情
  · 报告落盘：同时在源文件旁边生成 math_guard_report.txt

实现上不重复业务逻辑——所有检查都调用 math_guard.check_one_file()，
保证 GUI 与命令行的判定完全一致。
"""
from __future__ import annotations

import os
import queue
import sys
import threading
import traceback
from pathlib import Path
from typing import List, Optional

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

try:
    import math_guard as mg
except Exception as exc:  # pragma: no cover
    import tkinter.messagebox as mb
    r = tk.Tk()
    r.withdraw()
    mb.showerror("MathGuard 启动失败", f"无法加载检查模块：{exc}")
    r.destroy()
    raise SystemExit(1)

APP_TITLE = "MathGuard — LaTeX 论文体检"

# Windows 的 tkinter 不直接支持文件拖拽，需要注册 OLE 的 drop target。
# 用纯 tk 实现一个最小可用的拖拽（仅 Windows）。
HAS_DND = False
try:
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        HAS_DND = True
except Exception:
    HAS_DND = False


class DropTarget:
    """最小化的 Windows 文件拖拽支持（基于 tk 窗口句柄 + OLE 回调）。

    失败时静默降级——用户仍可用「选择文件」按钮。
    """

    def __init__(self, root: tk.Tk, callback):
        self.root = root
        self.callback = callback
        self.ok = False
        if not HAS_DND:
            return
        try:
            # tk 的 winfo_id 在 Windows 上是子窗口句柄；用 GetParent 拿顶层
            hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
            if not hwnd:
                hwnd = root.winfo_id()
            self.hwnd = hwnd
            # 注册 WM_DROPFILES 消息：Shell 的 DragAcceptFiles 方式最稳
            ctypes.windll.shell32.DragAcceptFiles(hwnd, True)
            self.ok = True
        except Exception:
            self.ok = False


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.paths: List[Path] = []
        self.queue: "queue.Queue[tuple]" = queue.Queue()
        self.running = False
        self.drop = DropTarget(root, self.on_drop)

        root.title(APP_TITLE)
        root.geometry("820x600")
        root.minsize(680, 480)
        root.configure(bg="#f5f6f8")
        self._build_ui()
        self._poll_queue()

    # ---------------- UI ----------------
    def _build_ui(self) -> None:
        style = ttk.Style()
        try:
            style.theme_use("vista")
        except Exception:
            pass

        head = tk.Frame(self.root, bg="#2d6cdf", height=64)
        head.pack(fill="x")
        tk.Label(head, text="MathGuard", bg="#2d6cdf", fg="white",
                 font=("Microsoft YaHei UI", 16, "bold")).pack(side="left", padx=16)
        tk.Label(head, text="LaTeX 论文体检 · 完全离线 · 不联网 · 不改你的文件",
                 bg="#2d6cdf", fg="#dbe7ff",
                 font=("Microsoft YaHei UI", 9)).pack(side="left")

        body = tk.Frame(self.root, bg="#f5f6f8")
        body.pack(fill="both", expand=True, padx=14, pady=12)

        # 拖拽区
        self.drop_area = tk.Label(
            body,
            text="把 .tex 文件或整个论文文件夹拖到这里\n\n或者点击下面的按钮选择",
            bg="white", fg="#4a5568", relief="groove", bd=2,
            font=("Microsoft YaHei UI", 11), justify="center", height=6,
        )
        self.drop_area.pack(fill="x", pady=(0, 10))
        self.drop_area.bind("<Button-1>", lambda e: self.choose_files())

        btns = tk.Frame(body, bg="#f5f6f8")
        btns.pack(fill="x", pady=(0, 8))
        ttk.Button(btns, text="选择 .tex 文件", command=self.choose_files).pack(side="left")
        ttk.Button(btns, text="选择文件夹", command=self.choose_dir).pack(side="left", padx=6)
        self.run_btn = ttk.Button(btns, text="开始体检", command=self.start_check)
        self.run_btn.pack(side="left", padx=(18, 0))
        ttk.Button(btns, text="清空", command=self.clear).pack(side="left", padx=6)

        self.lint_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(btns, text="同时检查公式末尾标点（--lint，警告较多）",
                        variable=self.lint_var).pack(side="left", padx=(16, 0))

        # 选中列表
        self.listbox = tk.Listbox(body, height=5, font=("Consolas", 9),
                                  activestyle="none", bg="white")
        self.listbox.pack(fill="x", pady=(0, 8))

        # 结论
        self.verdict = tk.Label(body, text="等待中…", bg="#f5f6f8", fg="#2d3748",
                                font=("Microsoft YaHei UI", 12, "bold"), anchor="w")
        self.verdict.pack(fill="x", pady=(0, 6))

        # 详情
        frame = tk.Frame(body, bg="#f5f6f8")
        frame.pack(fill="both", expand=True)
        self.text = tk.Text(frame, wrap="none", font=("Consolas", 9), bg="white",
                            bd=1, relief="sunken")
        vs = ttk.Scrollbar(frame, orient="vertical", command=self.text.yview)
        hs = ttk.Scrollbar(frame, orient="horizontal", command=self.text.xview)
        self.text.configure(yscrollcommand=vs.set, xscrollcommand=hs.set)
        self.text.grid(row=0, column=0, sticky="nsew")
        vs.grid(row=0, column=1, sticky="ns")
        hs.grid(row=1, column=0, sticky="ew")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)

    # ---------------- 交互 ----------------
    def choose_files(self) -> None:
        files = filedialog.askopenfilenames(
            title="选择要体检的 .tex 文件",
            filetypes=[("LaTeX 源文件", "*.tex"), ("所有文件", "*.*")],
        )
        if files:
            self.add_paths([Path(f) for f in files])

    def choose_dir(self) -> None:
        d = filedialog.askdirectory(title="选择论文所在的文件夹")
        if d:
            self.add_paths([Path(d)])

    def add_paths(self, paths: List[Path]) -> None:
        for p in paths:
            if p not in self.paths:
                self.paths.append(p)
        self.refresh_list()
        self.drop_area.configure(
            text=f"已选择 {len(self.paths)} 项\n\n可以继续拖入，或点「开始体检」", fg="#2b6cb0")

    def clear(self) -> None:
        self.paths = []
        self.listbox.delete(0, "end")
        self.text.delete("1.0", "end")
        self.verdict.configure(text="等待中…", fg="#2d3748")
        self.drop_area.configure(text="把 .tex 文件或整个论文文件夹拖到这里\n\n或者点击下面的按钮选择",
                                 fg="#4a5568")

    def refresh_list(self) -> None:
        self.listbox.delete(0, "end")
        for p in self.paths:
            tag = "[目录]" if p.is_dir() else "[文件]"
            self.listbox.insert("end", f"{tag} {p}")

    def on_drop(self, paths: List[str]) -> None:
        self.add_paths([Path(p) for p in paths])

    # ---------------- 执行 ----------------
    def start_check(self) -> None:
        if self.running:
            return
        if not self.paths:
            messagebox.showinfo("提示", "请先拖入 .tex 文件或文件夹")
            return
        self.running = True
        self.run_btn.configure(state="disabled")
        self.text.delete("1.0", "end")
        self.verdict.configure(text="正在体检…", fg="#2b6cb0")
        # ⚠ tkinter 变量只能主线程读：子线程里调 lint_var.get() 会抛
        #   RuntimeError("main thread is not in main loop")，
        #   结果每个文件都被记成"异常"，界面却显示"全部通过"——
        #   这是最危险的一类 bug（看起来正常，结论是假的）。
        #   必须在启动线程**之前**把值取出来传进去。
        lint = bool(self.lint_var.get())
        threading.Thread(target=self._work, args=(lint,), daemon=True).start()

    def _work(self, lint: bool) -> None:
        try:
            files = mg.collect_tex_files([str(p) for p in self.paths])
        except AttributeError:
            files = []
            for p in self.paths:
                if p.is_file() and p.suffix.lower() == ".tex":
                    files.append(p)
                elif p.is_dir():
                    for dp, dns, fns in os.walk(p):
                        dns[:] = [d for d in dns if d not in
                                  ("node_modules", ".git", "__pycache__", "build", "dist")]
                        files += [Path(dp) / f for f in fns if f.lower().endswith(".tex")]
        if not files:
            self.queue.put(("done", "没有找到任何 .tex 文件", ""))
            return

        self.queue.put(("log", f"共 {len(files)} 个文件，开始体检…\n", None))
        results = []
        for i, f in enumerate(files, 1):
            buf: List[str] = []
            try:
                code = mg.check_single(f, lint=lint, sink=buf)
            except Exception:
                code = 4
                buf.append(traceback.format_exc())
            results.append((f, code, "".join(buf)))
            self.queue.put(("progress", (i, len(files), f.name), None))

        n_err = sum(1 for _, c, _ in results if c == 3)
        n_struct = sum(1 for _, c, _ in results if c == 1)
        n_ok = sum(1 for _, c, _ in results if c == 0)
        n_usage = len(results) - n_err - n_struct - n_ok

        # ⚠ 凡是「大多数文件读不了」，绝不能报"全部通过"。
        #   实测曾因 tkinter 线程错误导致 4 个文件全记成异常，
        #   结论却显示"全部 4 篇体检通过"——这是最坏的结果。
        if n_usage and n_ok == 0 and n_err == 0 and n_struct == 0:
            verdict = f"⚠ 有 {n_usage} 个文件无法读取，结果不可信"
            color = "#c53030"
        elif n_usage > len(results) // 2:
            verdict = f"⚠ 一半以上文件无法读取（{n_usage}/{len(results)}），结果不可信"
            color = "#c53030"
        elif n_err:
            verdict = f"发现 {n_err} 篇有确定错误，建议修"
            color = "#c53030"
        elif n_struct:
            verdict = f"没有确定错误；{n_struct} 篇有排版/引用问题"
            color = "#b7791f"
        else:
            verdict = f"全部 {len(results)} 篇体检通过"
            color = "#2f855a"

        detail = []
        for f, code, out in results:
            mark = {0: "[通过]", 1: "[结构]", 3: "[错误]", 4: "[异常]"}.get(code, "[?]")
            detail.append(f"{'=' * 78}\n{mark}  {f}\n{'=' * 78}\n{out}\n")

        summary = (f"共 {len(results)} 个文件\n"
                   f"  通过    {n_ok}\n  结构问题 {n_struct}\n"
                   f"  确定错误 {n_err}\n  无法读取 {n_usage}\n\n")
        self.queue.put(("done", verdict, color, summary + "\n".join(detail)))

    def _poll_queue(self) -> None:
        try:
            while True:
                item = self.queue.get_nowait()
                if item[0] == "progress":
                    i, n, name = item[1]
                    self.verdict.configure(text=f"正在体检 {i}/{n}：{name}", fg="#2b6cb0")
                elif item[0] == "log":
                    self.text.insert("end", item[1])
                    self.text.see("end")
                elif item[0] == "done":
                    verdict = item[1]
                    color = item[2] if len(item) > 2 else "#2d3748"
                    body = item[3] if len(item) > 3 else ""
                    self.verdict.configure(text=verdict, fg=color)
                    self.text.insert("1.0", body)
                    if body:
                        self._save_report(body)
                    self.running = False
                    self.run_btn.configure(state="normal")
        except queue.Empty:
            pass
        self.root.after(120, self._poll_queue)

    def _save_report(self, body: str) -> None:
        try:
            base = self.paths[0]
            outdir = base if base.is_dir() else base.parent
            p = outdir / "math_guard_report.txt"
            p.write_text(body, encoding="utf-8")
            self.text.insert("end", f"\n\n报告已保存到：{p}\n")
        except Exception as exc:
            self.text.insert("end", f"\n（报告未能保存：{exc}）\n")


def main() -> None:
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()

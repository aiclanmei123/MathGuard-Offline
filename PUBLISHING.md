# 发布到 GitHub —— 操作步骤

> 仓库已经准备好：`.git` 已初始化，首次提交已完成（commit `4cd570a`）。
> 剩下的是**只有你能做**的部分：登录你的 GitHub 账号并推送。

---

## 方式 A：GitHub Desktop（最简单）

1. 下载安装 [GitHub Desktop](https://desktop.github.com/)（本机目前未安装）
2. 打开 → `File` → `Add local repository...`
3. 选择文件夹：`C:\Users\yummy15\Documents\deepseek-harness\default-workspace\MathGuard-Offline`
4. 点 `Publish repository`
   - **Name**：`MathGuard-Offline`
   - **Description**：
     ```
     纯离线的 LaTeX 结构与公式数值校验工具。四态报告，零假阳性，拒绝用大模型做算术。
     ```
   - ⚠️ **先勾选 `Keep this code private`**（见下方「为什么先私有」）
5. 点 `Publish repository`

---

## 方式 B：命令行 + 网页建仓

1. 打开 https://github.com/new
   - Repository name: `MathGuard-Offline`
   - 选择 **Private**（建议）
   - **不要**勾选 "Add a README file"（本地已有）
2. 在终端执行：

```powershell
cd "C:\Users\yummy15\Documents\deepseek-harness\default-workspace\MathGuard-Offline"
git remote add origin https://github.com/你的用户名/MathGuard-Offline.git
git branch -M main
git push -u origin main
```

首次 push 会弹出浏览器让你登录授权。

---

## 发布 exe 到 Releases（可选，但推荐）

GitHub 仓库不适合放 38 MB 的 zip，用 Releases：

1. 仓库页面右侧 → `Releases` → `Create a new release`
2. Tag version: `v1.0.0`
3. Release title: `MathGuard v1.0.0`
4. 描述：
   ```markdown
   ## 下载

   - **Windows x64（免安装 Python）**：`MathGuard-Windows-x64.zip`

   解压后运行：
   ```cmd
   math_guard.exe 你的论文.tex
   ```

   ## 核心特性

   - 四态报告：`[✓]` 已验证 / `[✗]` 验证失败 / `[≈]` 仅可解析 / `[–]` 无法验证
   - 零假阳性优先：宁可少报，不可错报
   - 完全离线，无需联网

   ## 已知边界

   - 只接受 `.tex`，不接受 PDF（PDF 公式无法还原，会明确拒绝）
   - 不验证证明的正确性，只做结构与数值校验
   ```
5. 上传 `MathGuard-Windows-x64.zip` → `Publish release`

---

## 为什么建议先私有

| 理由 | 说明 |
|---|---|
| **签名与安全** | exe 没有代码签名，公开后有人可能重新打包伪装 |
| **可撤回** | 发现重大问题（如又一类假阳性）时，私有仓库改起来没压力 |
| **先验证价值** | 等有 2-3 个同学真的用起来、给出反馈，再公开也不迟 |

确认没问题后再转公开：仓库 `Settings` → 最下方 `Danger Zone` → `Change visibility`。

---

## 如果之后改了代码

```powershell
cd "C:\Users\yummy15\Documents\deepseek-harness\default-workspace\MathGuard-Offline"

# 1. 改完先跑自测（必须全过）
python test_math_guard.py

# 2. 提交
git add -A
git commit -m "说明这次改了什么"

# 3. 推送
git push
```

**铁律**：改任何判定逻辑，必须先跑 `test_math_guard.py`，31 项全过才能提交。
理由：一个永远报"通过"的防幻觉工具，本身就是幻觉。

---

## 当前状态

```
仓库路径  C:\Users\yummy15\Documents\deepseek-harness\default-workspace\MathGuard-Offline
已提交    4cd570a（8 个文件，3027 行）
远程      未配置
exe       已打包，位于 MathGuard-Windows-x64.zip（37.8 MB）
```

# 从零到发布 —— 完整操作清单

> 当前状态（2026-11-14 实测）：
> - ✅ 代码已写好并在本机跑通（31 项自测全过）
> - ✅ 仓库已初始化并提交（单提交 `39c9303`，9 个文件 3143 行）
> - ✅ exe 已打包（37.8 MB，免装 Python）
> - ❌ **还没有 GitHub 账号**（凭据管理器空、无 `.gitconfig`、未装 GitHub Desktop）
>
> 下面是从零开始的完整步骤。全程约 15 分钟。

---

## 第 1 步：注册 GitHub 账号（5 分钟）

1. 打开 https://github.com/signup
2. 填邮箱 → 设密码 → 起用户名
   - **用户名会出现在仓库地址里**，建议短且有辨识度，例如 `yimingwang` 或 `yw-ang`
   - 中文用户名不支持，只能用字母数字和连字符
3. 验证邮箱（GitHub 会发一封带验证码的邮件）
4. 跳过 "personalize your experience" 之类的引导，直接进主页

> 不需要付费。免费账号可以建无限量私有/公开仓库。

---

## 第 2 步：安装 GitHub Desktop（3 分钟）

下载：https://desktop.github.com/

安装后打开 → `Sign in to GitHub.com` → 用第 1 步的账号登录（会跳浏览器授权）。

> 为什么不装 GitHub Desktop 而用命令行？
> 命令行 push 需要配置 Personal Access Token，配置过程比 Desktop 复杂。
> Desktop 用浏览器 OAuth，点几下就完成。

---

## 第 3 步：把本地仓库加进去（1 分钟）

1. GitHub Desktop 菜单：`File` → `Add local repository...`
2. 选择这个文件夹（直接复制粘贴到路径栏）：
   ```
   C:\Users\yummy15\Documents\deepseek-harness\default-workspace\MathGuard-Offline
   ```
3. 点 `Add repository`

左侧应该显示 `MathGuard-Offline`，右侧显示 9 个文件和一个提交。

---

## 第 4 步：发布（1 分钟）

点右上角 `Publish repository`，填写：

| 字段 | 填什么 |
|---|---|
| **Name** | `MathGuard-Offline` |
| **Description** | `纯离线的 LaTeX 结构与公式数值校验工具。四态报告，零假阳性，拒绝用大模型做算术。` |
| **Keep this code private** | ☑️ **勾上**（见下方说明） |

点 `Publish repository`。

---

## 第 5 步：上传 exe 到 Releases（2 分钟）

仓库里不放 38 MB 的 zip，用 Releases：

1. 浏览器打开你的仓库页面
2. 右侧 `Releases` → `Create a new release`
3. 填写：
   - **Tag version**: `v1.0.0`
   - **Release title**: `MathGuard v1.0.0`
   - **Description**（可直接复制）：

     ```markdown
     ## 下载

     **Windows x64 · 免安装 Python**：`MathGuard-Windows-x64.zip`

     解压后运行：
     ```cmd
     math_guard.exe 你的论文.tex
     ```

     ## 核心特性

     - 四态报告：`[✓]` 已验证 / `[✗]` 验证失败 / `[≈]` 仅可解析 / `[–]` 无法验证
     - 零假阳性优先：宁可少报，不可错报
     - 完全离线，无需联网
     - 支持 GBK 老稿件、多文件 `\input`、自定义 `\newcommand` 宏

     ## 已知边界

     - 只接受 `.tex`，不接受 PDF（PDF 公式无法还原，会明确拒绝）
     - 不验证证明的正确性，只做结构与数值校验
     ```

4. 把 `MathGuard-Windows-x64.zip` 拖进 "Attach binaries" 区域
5. 点 `Publish release`

---

## 第 6 步：找个同学试用（最重要）

给同门发这句话：

> 我写了个完全不联网的小工具，能查 LaTeX 的 label 引用问题和数值算错。
> 你把论文的 .tex 丢进去跑一下？解压即用，不用装 Python。
> 跑出来什么贴给我看看。

**重点关注两件事**：
1. 有没有他说"这个我写对了，它却报错" → 这是假阳性，最严重
2. 有没有大量 `[≈] 仅可解析` → 可能是我漏检了能判定的情况

---

## 为什么建议先设私有

| 理由 | 说明 |
|---|---|
| **exe 没有代码签名** | 公开后可能被人重新打包伪装成你的版本 |
| **可撤回** | 万一又发现一类假阳性，私有仓库改起来没压力 |
| **先验证价值** | 等有 2–3 人真的用起来再公开也不迟 |

确认没问题后转公开：仓库 `Settings` → 拉到底 `Danger Zone` → `Change visibility` → `Change to public`。

---

## 之后改代码的流程

```powershell
cd "C:\Users\yummy15\Documents\deepseek-harness\default-workspace\MathGuard-Offline"

# 1. 改完先跑自测（必须 31 项全过）
python test_math_guard.py

# 2. GitHub Desktop 里会自动显示改动 → 填一句说明 → Commit → Push

# 3. 如果改了判定逻辑，还要重新打包 exe 并更新 Release
python -m PyInstaller build.spec --noconfirm
```

**铁律**：改任何判定逻辑，必须先跑 `test_math_guard.py`。理由：
一个永远报"通过"的防幻觉工具，本身就是幻觉。

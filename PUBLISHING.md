# 网络受限环境下的发布路线（实测可行）

> 背景：你这台机器访问 `github.com/signup` 返回 **403 访问受限**（IP 被判定为机器人），
> 但 `github.com/login` 和 `github.com/login/device` 都是 **200 正常**。
>
> **实测结论**：
> - `git ls-remote https://github.com/...` → **成功**（git 协议完全正常）
> - `https://github.com/login/device/code` → **成功下发设备码**（`11C1-261F` 之类）
>
> 所以：**只有"注册"这一步需要换网络，之后全部可以在这台电脑上完成。**

---

## 步骤 1：用手机流量注册（唯一需要换网络的一步）

1. **关闭手机 WiFi，改用 4G/5G 流量**
2. 手机浏览器打开：`https://github.com/signup`
3. 填邮箱 → 密码 → 用户名
   - 用户名会出现在仓库地址里，建议短一点，例如 `yimingwang`
   - 只能用字母、数字、连字符，不支持中文
4. 收邮件验证码，完成验证
5. 见到 "Let's get started" 页面就成功了

> **为什么用流量**：你家宽带的公网 IP（`223.67.108.7`）已被 GitHub 标记，
> 手机流量的出口 IP 通常不同，大概率不被拦。
>
> 如果手机也被拦，退路见文末"方案 B"。

---

## 步骤 2：回到电脑，用设备码登录（不需要浏览器）

在 PowerShell 里执行：

```powershell
cd "C:\Users\yummy15\Documents\deepseek-harness\default-workspace\MathGuard-Offline"

& "C:\Program Files\Git\ucrt64\bin\git-credential-manager.exe" github login --device --no-ui
```

终端会显示类似：

```
Please enter the code: 11C1-261F
and navigate to: https://github.com/login/device
```

然后：

1. **在手机浏览器**打开 `https://github.com/login/device`（登录页不被拦）
2. 输入终端显示的那个码
3. 点 Authorize
4. 电脑终端会显示登录成功

> 为什么用手机输入码？因为该页面在电脑上虽然能打开，但如果你之后又在电脑浏览器里
> 遇到验证码，用手机走一遍会顺得多。两边都行，看你方便。

---

## 步骤 3：建仓库并推送

登录成功后，在电脑上执行：

```powershell
cd "C:\Users\yummy15\Documents\deepseek-harness\default-workspace\MathGuard-Offline"

# 建远程仓库（需要先在手机上用 GitHub 网页建一个空仓库，或用 gh CLI）
git remote add origin https://github.com/你的用户名/MathGuard-Offline.git
git branch -M main
git push -u origin main
```

**建空仓库这一步**：手机浏览器打开 `https://github.com/new`
- Repository name: `MathGuard-Offline`
- 选 **Private**
- **不要**勾选 "Add a README file"（本地已有）
- 点 Create repository

---

## 步骤 4：上传 exe 到 Releases

手机上也能做（`https://github.com/你的用户名/MathGuard-Offline/releases/new`），
但要把 38 MB 的 zip 传上去，用电脑网页更方便：

1. 电脑浏览器打开仓库页
2. 右侧 `Releases` → `Create a new release`
3. Tag: `v1.0.0`，Title: `MathGuard v1.0.0`
4. 把 `MathGuard-Windows-x64.zip` 拖进 "Attach binaries"
5. Publish release

> 如果 computer 网页也被验证码拦，见"方案 B"。

---

## 方案 B：如果实在过不去

| 方案 | 做法 | 代价 |
|---|---|---|
| **Gitee（码云）** | `gitee.com` 国内不拦，注册和推送都很顺 | 社区小一些，但私有的发布够用 |
| **直接发 zip** | 把 `MathGuard-Windows-x64.zip` 用微信/QQ 发给同学 | 没有仓库，但功能一个不少 |
| **等几小时** | GitHub 的 IP 封禁常是临时的，换个时间段再试 | 可能明天就好了 |

**核心提醒**：GitHub 上的仓库是"分发渠道"，不是"功能本身"。
工具已经打包好了，**发给同学用不需要任何账号**。
如果你只是想让人试用，直接把 zip 发过去就够了。

---

## 已验证的状态

| 项目 | 状态 |
|---|---|
| git 安装 | ✅ 2.56.0 |
| git 协议连通 GitHub | ✅ `ls-remote` 成功 |
| Git Credential Manager | ✅ 2.9.1，已配置为 helper |
| 设备码端点 | ✅ 实测拿到 user_code |
| 注册页（电脑） | ❌ 403 |
| 登录页 / 设备码页 | ✅ 200 |
| 本地仓库 | ✅ 单提交，9 文件 3143 行 |
| exe | ✅ 37.8 MB，与源码行为一致 |

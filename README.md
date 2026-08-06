# HidenCloud 自动续期

使用 GitHub Actions 定时自动续期 [HidenCloud](https://hidencloud.com) 云服务，**支持单账号 / 多账号**，并利用独享代理节点隔离环境，有效避免因多账号共用 IP 而被封禁。

---

## ✨ 特性

- 🔐 支持 **Cookie 登录**（有效期长）和 **账号密码登录**（备选）
- 👥 **多账号支持**：可配置多个账号，按顺序依次续期，每个账号之间自动间隔 **3 分钟**
- 🌐 **独享代理**：配合 `NODE_LINK` 使用自己的节点（VLESS / VMess / Trojan / Hysteria2 等），避免触发风控
- 📢 **Telegram 通知**：实时推送续期结果（可选）
- 🧹 自动清理运行记录，保留最近一次工作流日志
- 🔄 支持手动触发和定时触发（cron）

---

## 📦 配置

在仓库 `Settings → Secrets and variables → Actions` 中添加以下 Secrets：

| Secret 名称 | 是否必填 | 说明 | 示例 |
|---|---|---|---|
| `ACCOUNTS_JSON` | ❌（二选一） | **多账号**模式：JSON 数组，包含每个账号的邮箱、密码、Cookie | `[{"email":"a@b.com","password":"pwd","cookie":"xxx"}]` |
| `EMAIL` | ❌（二选一） | **单账号**模式：登录邮箱 | `your@email.com` |
| `PASSWORD` | ❌（二选一） | **单账号**模式：登录密码 | `your_password` |
| `COOKIE_VALUE` | ❌（二选一） | **单账号**模式：`remember_web_*` Cookie 值（优先于密码） | 见下方获取方法 |
| `NODE_LINK` | ❌（强烈推荐） | 代理节点分享链接，用于启动本地 SOCKS5 代理 | `vless://...` / `vmess://...` / `trojan://...` 等 |
| `TG_BOT_TOKEN` | ❌ | Telegram Bot Token | `123456:ABC-DEF` |
| `TG_CHAT_ID` | ❌ | Telegram Chat ID | `123456789` |

> **注意**：  
> - 若配置了 `ACCOUNTS_JSON`，则忽略单账号的三个凭证（`EMAIL`/`PASSWORD`/`COOKIE_VALUE`）。  
> - 单账号模式下，推荐配置 `COOKIE_VALUE`（有效期内免密码登录），若失效则自动回退到账号密码登录。

---

### 🔑 如何获取 `COOKIE_VALUE`？

1. 使用浏览器登录 [HidenCloud Dashboard](https://dash.hidencloud.com)  
2. 打开开发者工具（F12） → 切换到 **Application**（Chrome）或 **存储**（Firefox）  
3. 在左侧找到 **Cookies** → `https://dash.hidencloud.com`  
4. 找到名为 `remember_web_59ba36addc2b2f9401580f014c7f58ea4e30989d` 的 Cookie，复制其 **Value**

![获取 Cookie 示例](https://github.com/user-attachments/assets/be28a597-eef8-481b-862d-cc98533a2e27)

---

### 📌 代理节点格式（`NODE_LINK`）

支持以下常见协议的**完整分享链接**（与 v2rayN / Nekoray 等客户端通用）：

- **VLESS**：`vless://uuid@server:port?security=reality&sni=...&type=ws&...`
- **VMess**：`vmess://base64encoded...`
- **Trojan**：`trojan://password@server:port?sni=...&type=ws&...`
- **tuic**：`tuic://uuid:password@server:port...`
- **anytls**：`anytls://uuid@server:port...`
- **Hysteria2**：`hysteria2://base64@server:port...`
- **SOCKS5**：`socks5://user:pass@server:port` 或 `socks://user:pass@server:port`

> ⚠️ 建议使用 **独享节点**（仅自己使用），避免因多人共用 IP 导致账号关联或被限制。

---

## 🚀 使用

### 1. Fork 本仓库
点击右上角 **Fork** 将仓库复制到你的 GitHub 账户。

### 2. 配置 Secrets
按上述表格在仓库中依次添加所需的 Secrets。

### 3. 触发工作流
- **手动触发**：进入仓库的 **Actions** 选项卡，选择 **Auto Renew HidenCloud**，点击 **Run workflow** → **Run workflow**
- **自动触发**：默认每周日 05:30（UTC）运行一次。可根据你的服务到期日修改 `.github/workflows/renew.yml` 中的 `cron` 表达式（例如每周二运行则改为 `30 05 * * 2`）。

### 4. 查看运行结果
- 在 Actions 页面点击最新的运行记录，查看详细日志。
- 若配置了 Telegram，会收到续期结果通知。

---

## ⚙️ 高级用法

### 多账号 JSON 格式示例

```json
[
  {
    "email": "user1@example.com",
    "password": "pass123",
    "cookie": "cookie_value_1"
  },
  {
    "email": "user2@example.com",
    "password": "pass456",
    "cookie": "cookie_value_2"
  }
]
```

每个账号对象中，`cookie` 为可选字段（若提供则优先使用），否则使用 `email` + `password` 登录。

脚本会**顺序处理**每个账号，当前账号处理完成后等待 **3 分钟**再继续下一个，有效降低被风控的风险。

---

## 🛠️ 本地调试

如需在本地测试，请先安装依赖：

```bash
pip install playwright requests 'requests[socks]'
playwright install chromium
```

然后设置环境变量（或在脚本中硬编码），运行：

```bash
python app.py
```

> 若本地无 Xvfb，可将脚本中的 `headless=False` 改为 `headless=True`（但可能影响 Cloudflare 验证处理）。

---

## 📢 Telegram 通知模板

成功发送的通知示例如下：

```
🎉 HidenCloud 续期通知

✅ 续期成功
👤 账号: us****@example.com
📅 续期前到期：1 Jan 2026
📅 续期后到期：1 Feb 2026
🕒 续期时间：2026-01-15 08:30:00
```

---

## ❓ 常见问题

**Q：为什么我总是收到“未到续期时间”？**  
A：HidenCloud 只能在到期前 7 天内续期。脚本会检测并跳过，不影响后续流程。

**Q：Cookie 登录失败怎么办？**  
A：脚本会自动尝试账号密码登录，请确保 `EMAIL` 和 `PASSWORD` 正确。若 Cookie 过期，建议重新获取并更新 Secret。

**Q：代理节点不稳定或无法连接？**  
A：请检查 `NODE_LINK` 是否有效，且确保你使用的协议在 `sing-box` 支持范围内。可先本地测试该节点能否正常使用。

**Q：如何修改账号间等待时间？**  
A：在 `app.py` 中找到 `time.sleep(180)`，将 `180`（秒）改为你需要的间隔。

---

## ⚠️ 免责声明

本脚本仅供**学习与交流**使用，使用者需严格遵守 [HidenCloud](https://hidencloud.com) 的服务条款。  
**作者不承担任何因使用本脚本导致的账号封禁、数据丢失或其他损失的责任**。  
请合理使用，并注意保护个人隐私与账号安全。

---

## 📄 许可证

本项目采用 [MIT License](LICENSE)。

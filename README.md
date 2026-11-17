# Telegram 签到工作台

通过 Telegram Web 管理多账号签到，支持单账号、单群执行、群内话题、自定义每日定时和 Server酱微信通知。无需申请 Telegram API。

![工作台](docs/assets/workbench.png)

## 下载与启动

在 [GitHub Releases](https://github.com/ElmaTony/telegram-checkin/releases/latest) 下载对应系统的包，解压后保留整个目录。

| 系统 | 文件后缀 |
| --- | --- |
| Windows x64 | `windows-x64.zip` |
| macOS Apple 芯片 | `macos-arm64.tar.gz` |
| macOS Intel | `macos-x64.tar.gz` |
| Linux x64 | `linux-x64.tar.gz` |
| Linux ARM64 | `linux-arm64.tar.gz` |

独立包无需安装 Python，首次运行需下载浏览器。

Windows（PowerShell）：

```powershell
.\telegram-checkin.exe --install-browser
.\telegram-checkin.exe
```

macOS / Linux：

```bash
./telegram-checkin --install-browser
./telegram-checkin
```

打开 [http://127.0.0.1:8577](http://127.0.0.1:8577)。Linux 缺少系统库时，先执行 `sudo ./telegram-checkin --install-browser-deps`。macOS 包尚未签名和公证。

## 使用

1. 添加账号并保存，点击登录，在浏览器中扫码。
2. 添加签到会话，配置文字、按钮或图片；论坛群选择发送话题。
3. 执行全部、单账号或单群签到，在列表中查看结果。

「定时与通知」中可自定义每日时间、填写 Server酱 SendKey 并测试微信推送。计划默认关闭，程序需保持运行。

已发送但未确认的会话先补查，避免重复发送。已有草稿会在签到前备份清空，结束后恢复；图片步骤失败或取消时先关闭本轮预览。草稿恢复失败会保留备份并提示。

机器人询问是否继续签到时，仍属待确认；需完成配置的按钮步骤并取得最终反馈，才计为成功。
若提示「判定词需要核对」，可从会话详情检查规则，保存后仅补查反馈，无需重新发送签到内容。

## 源码运行

需要 Python 3.11+，运行设备须能访问 Telegram。

```powershell
git clone https://github.com/ElmaTony/telegram-checkin.git
cd telegram-checkin
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock.txt
.\.venv\Scripts\python.exe -m playwright install chromium
.\.venv\Scripts\python.exe app.py
```

macOS / Linux 将 `.\.venv\Scripts\python.exe` 替换为 `.venv/bin/python`；Linux 安装浏览器时使用 `playwright install --with-deps chromium`。Windows 日常可双击 `start_gui.bat`。

## 数据与部署

源码运行默认将数据保存在项目目录。独立包使用系统用户数据目录：Windows 为 `%LOCALAPPDATA%\TelegramCheckin`，macOS 为 `~/Library/Application Support/TelegramCheckin`，Linux 为 `~/.local/share/telegram-checkin`（支持 `XDG_DATA_HOME`）。可用 `--home 路径` 指定目录。

升级前退出程序并备份数据，再替换程序目录。账号登录、配置和签到记录保存在数据目录中。

无桌面的 Linux 服务自动使用后台浏览器；首次扫码仍需可视桌面环境。独立包已包含 systemd 服务模板。

[云端部署](docs/CLOUD.md) · [配置参考](docs/CONFIGURATION.md) · [更新记录](CHANGELOG.md) · [MIT License](LICENSE)

# Telegram 签到工作台

在本地或 Linux 服务器上管理 Telegram 多账号签到。通过 Telegram Web 登录，无需申请 API ID / API Hash。

- 全部账号、单账号、单会话签到，支持论坛话题。
- 文字、口令、仅按钮、口令＋按钮、图片和多步签到，自定义成功／失败词。
- 自定义每日时间与时区，使用 Server酱推送微信结果。
- 查看发送证据、只读补查反馈，签到前备份草稿，结束后恢复。

![签到工作台](docs/assets/workbench.png)

## 下载安装

前往 **[最新发行版](https://github.com/ElmaTony/telegram-checkin/releases/latest)**，下载对应系统的独立包。无需安装 Python，解压后保留完整目录。

| 系统 | 下载文件 |
| --- | --- |
| Windows x64 | `*-windows-x64.zip` |
| macOS Apple 芯片 | `*-macos-arm64.tar.gz` |
| macOS Intel | `*-macos-x64.tar.gz` |
| Linux x64 | `*-linux-x64.tar.gz` |
| Linux ARM64 | `*-linux-arm64.tar.gz` |

进入解压后的目录，首次安装浏览器，再启动工作台：

Windows / PowerShell：

```powershell
.\telegram-checkin.exe --install-browser
.\telegram-checkin.exe
```

macOS / Linux：

```bash
./telegram-checkin --install-browser
./telegram-checkin
```

工作台地址：**[http://127.0.0.1:8577](http://127.0.0.1:8577)**。运行设备需要能够访问 Telegram。Linux 若缺少浏览器系统库，执行 `sudo ./telegram-checkin --install-browser-deps`；macOS 包尚未签名和公证。

## 开始使用

1. **登录账号**：添加账号、保存配置，点击登录并在浏览器扫码。
2. **配置会话**：选中账号，从 Telegram 会话列表选择群或机器人，设置签到内容。论坛群需选择话题。
3. **执行签到**：选择全部、单账号或单会话；勾选「显示签到过程」可查看浏览器操作。
4. **设置计划**：在「定时与通知」中填写每日时间、时区和 Server酱 SendKey，可先测试微信推送。

定时计划默认关闭，启用后程序必须保持运行。

「执行记录」按日期分组，可筛选或导出，点击任意记录查看完整反馈。

| 结果 | 含义与处理 |
| --- | --- |
| 成功 | 已取得本账号的成功反馈，并满足配置的签到步骤。 |
| 待确认 | 有发送记录，但成功证据不足；先使用只读补查，避免重发。 |
| 失败 | 打开会话、发送或执行步骤出现异常；先查看详情和发送记录。 |

签到前会备份并清空已有草稿，完成、失败或取消后恢复。若无法可靠恢复，会保留备份并提示。机器人要求继续点击按钮时，需完成按钮步骤后才能确认成功。

## 源码安装

需要 **Python 3.11+**。

```bash
git clone https://github.com/ElmaTony/telegram-checkin.git
cd telegram-checkin
```

Windows / PowerShell：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock.txt
.\.venv\Scripts\python.exe -m playwright install chromium
.\.venv\Scripts\python.exe app.py
```

macOS / Linux：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock.txt
.venv/bin/python -m playwright install chromium
.venv/bin/python app.py
```

Linux 安装浏览器时可使用 `playwright install --with-deps chromium`。Windows 日常使用可双击 `start_gui.bat`。

## 数据与升级

源码运行的数据默认保存在项目目录，独立包默认使用以下位置；均可通过 `--home 路径` 指定数据目录。

| 系统 | 独立包数据目录 |
| --- | --- |
| Windows | `%LOCALAPPDATA%\TelegramCheckin` |
| macOS | `~/Library/Application Support/TelegramCheckin` |
| Linux | `~/.local/share/telegram-checkin`，支持 `XDG_DATA_HOME` |

升级时先退出程序并备份数据，再替换程序文件，继续使用原数据目录。数据目录包含账号登录凭据、SendKey、会话配置和签到记录，请勿公开上传。

## 云端运行

支持 Linux 后台浏览器和 systemd 常驻服务，独立包内提供服务模板。首次扫码需可视桌面环境；部署步骤、持久数据和远程访问设置见 **[云端部署](docs/CLOUD.md)**。

[配置参考](docs/CONFIGURATION.md) · [更新记录](CHANGELOG.md) · [MIT License](LICENSE)

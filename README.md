# telegram-checkin

面向个人多账号的 **Telegram Web 自动化签到工作台**。基于真实浏览器会话运行，**无需申请 API ID / API Hash**，提供本地 Web 控制台、每日定时调度、只读证据补查、草稿无损恢复与 Server酱 微信汇总推送，支持桌面端与 Linux 服务器常驻运行。

[![Build](https://github.com/ElmaTony/telegram-checkin/actions/workflows/build.yml/badge.svg)](https://github.com/ElmaTony/telegram-checkin/actions/workflows/build.yml)
[![Releases](https://img.shields.io/badge/Releases-Latest-1474c4)](https://github.com/ElmaTony/telegram-checkin/releases/latest)
[![Python](https://img.shields.io/badge/Python-3.11%2B-3776ab)](https://www.python.org/)
[![Platform](https://img.shields.io/badge/Platform-Windows%20%7C%20macOS%20%7C%20Linux-4a607a)](https://github.com/ElmaTony/telegram-checkin/releases/latest)
[![License](https://img.shields.io/badge/License-MIT-137347)](LICENSE)

[核心特性](#核心特性) · [界面预览](#界面预览) · [下载安装](#下载安装) · [快速上手](#快速上手) · [结果与安全机制](#结果与安全机制) · [云端部署](#云端部署) · [相关文档](#相关文档)

---

## 核心特性

- **多账号、多类型会话全覆盖**：支持普通群组、只读频道、私聊机器人以及**论坛话题（Forum Topics）**；添加会话时自动识别论坛话题并支持虚拟长列表滚动定位。
- **六种签到方式与多步串联**：支持文字、口令、仅按钮、口令＋按钮、图片＋文字、仅图片及多步动作组合，支持随机文案与 `{n}` / `X` 数字通配判定词，支持**共用系统主群优先与结果代签**。
- **证据驱动与防盲目重发**：联合监测机器人回复、模态弹窗、非模态 Toast 与原按钮消息编辑，严格区分「成功 / 待确认 / 失败」；当天已发或状态不明的目标优先只读补查，不盲目重发。
- **云端草稿无损保护**：进入会话后先备份并清空已有草稿，无论成功、失败或中途取消均恢复至原账号、原群与原话题；无法可靠恢复时保留本地备份并显著提示。
- **定时调度与微信通知**：内置独立于服务器系统时区的每日计划（支持全量 IANA 时区检索、账号范围筛选、后台浏览器与错过补跑），整轮结束后通过 **Server酱** 汇总推送微信结果。
- **双色现代工作台**：内置 Telegram Sky 浅色与夜色主题，提供发送预览、实时执行进度、按日历史归档、统计面板与运行日志排查。

---

## 界面预览

### 签到工作台
![签到工作台](docs/assets/workbench.png)

### 定时计划与微信通知
![定时计划与微信通知](docs/assets/automation.png)

### 论坛话题选择
![论坛话题选择](docs/assets/topic-picker.png)

---

## 下载安装

### 方式一：独立发行包（推荐，免安装 Python）

前往 **[Latest Release](https://github.com/ElmaTony/telegram-checkin/releases/latest)** 下载对应系统的独立包，解压后保留完整目录结构（建议同时核对同名 `.sha256` 校验文件）：

| 操作系统与架构 | 下载文件 |
| --- | --- |
| Windows x64 | `telegram-checkin-v*-windows-x64.zip` |
| macOS Apple 芯片（M1–M4） | `telegram-checkin-v*-macos-arm64.tar.gz` |
| macOS Intel | `telegram-checkin-v*-macos-x64.tar.gz` |
| Linux x64 | `telegram-checkin-v*-linux-x64.tar.gz` |
| Linux ARM64 | `telegram-checkin-v*-linux-arm64.tar.gz` |

进入解压目录，首次安装 Chromium 浏览器后再启动工作台：

**Windows（PowerShell）**：
```powershell
.\telegram-checkin.exe --install-browser
.\telegram-checkin.exe
```

**macOS / Linux**：
```bash
./telegram-checkin --install-browser
./telegram-checkin
```

启动后在浏览器打开 **[http://127.0.0.1:8577](http://127.0.0.1:8577)**。

> [!NOTE]
> 运行设备需可访问 Telegram Web。Linux 如缺少浏览器系统依赖，请先执行 `sudo ./telegram-checkin --install-browser-deps`；macOS 独立包暂未经过 Apple 签名与公证，首次运行需在系统设置中允许打开。

### 方式二：从源码运行（Python 3.11+）

```bash
git clone https://github.com/ElmaTony/telegram-checkin.git
cd telegram-checkin
```

**Windows（PowerShell）**：
```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock.txt
.\.venv\Scripts\python.exe -m playwright install chromium
.\.venv\Scripts\python.exe app.py
```

**macOS / Linux**：
```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock.txt
.venv/bin/python -m playwright install chromium
.venv/bin/python app.py
```

Linux 源码部署安装浏览器系统库可使用 `playwright install --with-deps chromium`。Windows 源码用户日常可直接双击 `start_gui.bat` 启动控制台。

### 常用命令行参数

| 参数 | 用途 |
| --- | --- |
| *(无参数)* / `--gui` | 启动本地 Web 工作台与内置定时服务（默认 `http://127.0.0.1:8577`） |
| `--run` | 立即执行一次全量签到（可配合 `--account 名称` 仅运行单个账号） |
| `--schedule` | 以纯后台命令行模式常驻运行每日定时计划（不启动 GUI） |
| `--login 账号名` | 打开可视浏览器为指定账号扫码登录 Telegram Web |
| `--doctor --browser` | 检查资源完整性、数据目录权限、时区数据库与 Chromium 启动状态 |
| `--notify-test` | 使用已保存结果测试 Server酱 微信推送（不触发签到） |
| `--home 路径` | 指定自定义数据目录（等同于设置环境变量 `CHECKIN_HOME`） |

---

## 快速上手

1. **添加并登录账号**：进入「账号与规则」，新增账号并保存配置，点击登录并在弹出的浏览器窗口扫码登录 Telegram Web。
2. **添加会话与签到规则**：在对应账号下从 Telegram 会话列表选取群组、频道或机器人，按需设置签到方式、内容与成功／失败关键词；论坛群自动识别并选择目标话题。
3. **执行与核对**：回到「签到工作台」，支持全量、单账号或单会话执行；勾选「显示签到过程」可实时观察浏览器操作。
4. **配置定时与通知**：进入「定时与通知」，设定每日执行时间、运行时区与账号范围，填入 Server酱 `SendKey` 并先发送测试通知验证送达。

> 定时计划默认关闭；开启后程序（Web 工作台或 `--schedule` 服务）需保持运行。

---

## 结果与安全机制

### 状态语义

| 状态 | 含义与建议处理 |
| --- | --- |
| **成功** | 已捕获可归属于本账号的明确成功反馈，且满足配置的全部步骤或共用系统代签条件。 |
| **待确认** | 已有发送记录，但反馈证据尚不足以确认结果；建议先点击**只读补查**，避免盲目重发。 |
| **失败** | 打开会话、话题定位、发送或按钮交互阶段出现异常；先展开详情与发送记录排查原因。 |

### 草稿保护与防重发
- 进入会话后先备份并清空原草稿，签到结束、失败或中途取消均会恢复原草稿；若检测到输入框出现新草稿或目标不匹配，将保留现有内容与本地备份文件并提示核对。
- 同日已发送或发送状态不明的会话不会在定时任务中自动重发；若进程异常中断，当日计划亦不会自动二次发起。

### 数据目录与升级

源码运行默认将数据保存在项目目录，独立发行包默认使用以下持久数据目录（均支持 `--home` 或 `CHECKIN_HOME` 覆盖）：

| 操作系统 | 独立包默认数据目录 |
| --- | --- |
| Windows | `%LOCALAPPDATA%\TelegramCheckin` |
| macOS | `~/Library/Application Support/TelegramCheckin` |
| Linux | `~/.local/share/telegram-checkin`（遵循 `XDG_DATA_HOME`） |

升级版本前请先停止程序并备份数据目录，替换程序文件后即可沿用原有配置、登录会话与历史记录。

> [!IMPORTANT]
> 数据目录包含 `config.yaml`、`data/` 登录凭据、`data/secrets.json`（SendKey）、状态文件与签到历史，属于个人敏感数据，请勿公开上传或分享。

---

## 云端部署

支持在 Ubuntu 22.04 / 24.04（x64 / ARM64）服务器上以 systemd 常驻运行，独立包内已附带 `deploy/` 服务模板与环境变量示例：

- 首次扫码需借助临时可视环境（如 SSH X11 / 本地 VNC），登录后自动切换无头后台浏览器执行。
- 推荐保持回环监听并通过 `ssh -L 8577:127.0.0.1:8577` 隧道访问工作台；对外监听前须配置 `CHECKIN_GUI_TOKEN` 与 HTTPS。

完整安装、首次登录、systemd 配置与上线验收步骤请查看 **[云端部署指南](docs/CLOUD.md)**。

---

## 相关文档

- **[配置参考（docs/CONFIGURATION.md）](docs/CONFIGURATION.md)**：完整 YAML 配置字段、六种签到方式、判定词通配与共用系统说明
- **[云端部署指南（docs/CLOUD.md）](docs/CLOUD.md)**：Linux 服务器常驻、systemd 服务、代理与上线验收步骤
- **[更新记录（CHANGELOG.md）](CHANGELOG.md)**：各版本变更明细
- **[MIT License](LICENSE)**：开源许可协议

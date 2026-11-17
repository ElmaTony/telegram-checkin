# 云服务器部署与验收

支持 Linux 单机常驻运行。首次扫码需要可视桌面，后续可使用后台浏览器自动签到。上线前仍需在目标服务器验证 Telegram 网络、账号登录和微信送达。

推荐 Ubuntu 22.04 / 24.04 的 x64 或 ARM64 主机。服务需要持续运行，GUI 与 `--schedule` 二选一。以下使用 `/opt/telegram-checkin` 存放程序、`/var/lib/telegram-checkin` 存放持久数据。

## 1. 准备目录和运行用户

创建普通服务用户 `telegram-checkin`，让它拥有程序和数据目录。下载与 CPU 架构匹配的独立包，或下载源码包。

首次部署创建用户和目录；已有服务用户时跳过 `useradd`：

```bash
sudo useradd --system --user-group --home-dir /var/lib/telegram-checkin --create-home telegram-checkin
sudo install -d /opt/telegram-checkin
sudo install -d -o telegram-checkin -g telegram-checkin /var/lib/telegram-checkin
```

首次将旧服务改用独立数据目录时，**先停服并备份**，复制原有 `config.yaml`、`data/`、`state.json*`、`history/` 和 `images/`。也可以保持 `CHECKIN_HOME` 指向原目录。不要把已有账号的服务切到空状态后直接重跑。

### 独立包

解压至 `/opt/telegram-checkin`，保留完整目录。在该目录执行：

```bash
sudo ./telegram-checkin --install-browser-deps
sudo install -d -o telegram-checkin -g telegram-checkin /var/lib/telegram-checkin
sudo -u telegram-checkin env CHECKIN_HOME=/var/lib/telegram-checkin ./telegram-checkin --install-browser
sudo -u telegram-checkin env CHECKIN_HOME=/var/lib/telegram-checkin ./telegram-checkin --doctor --browser
```

浏览器安装和运行必须使用同一个服务用户。独立包的浏览器保存在持久数据目录下，更新程序时不需要重新复制账号登录资料。

### 源码部署

在 `/opt/telegram-checkin` 安装：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock.txt
sudo .venv/bin/python -m playwright install-deps chromium
sudo -u telegram-checkin env PLAYWRIGHT_BROWSERS_PATH=/var/lib/telegram-checkin/data/browsers .venv/bin/python -m playwright install chromium
sudo -u telegram-checkin env CHECKIN_HOME=/var/lib/telegram-checkin PLAYWRIGHT_BROWSERS_PATH=/var/lib/telegram-checkin/data/browsers .venv/bin/python app.py --doctor --browser
```

以上命令与服务环境文件使用同一浏览器目录。安装浏览器和启动服务必须使用同一个用户及路径。

## 2. 首次账号登录

在工作台添加账号并保存。无桌面服务器仍需要临时的可视浏览器环境完成扫码，例如 SSH X11 转发或仅本机监听的 VNC 桌面。使用服务用户，在该可视环境中运行：

```bash
CHECKIN_HOME=/var/lib/telegram-checkin ./telegram-checkin --login 示例账号
# 源码方式：
CHECKIN_HOME=/var/lib/telegram-checkin .venv/bin/python app.py --login 示例账号
```

每个账号分别登录。Windows 资料目录不能保证可迁移到 Linux，应在服务器重新登录。首次登录完成后可在无界面模式执行签到；不要并发占用同一个账号 profile。

服务器必须能够访问 Telegram。桌面电脑的 `127.0.0.1` 代理不会自动变成服务器代理，需按服务器实际网络设置 `web_proxy`。

## 3. 常驻运行

独立包已附带 `deploy/`。独立包使用 `telegram-checkin-portable.service`，源码使用 `telegram-checkin.service`：

```bash
sudo cp deploy/telegram-checkin.env.example /etc/telegram-checkin.env
sudo chmod 600 /etc/telegram-checkin.env
sudo cp deploy/telegram-checkin-portable.service /etc/systemd/system/telegram-checkin.service
# 源码方式改用：sudo cp deploy/telegram-checkin.service /etc/systemd/system/telegram-checkin.service
sudo systemctl daemon-reload
sudo systemctl enable --now telegram-checkin
```

服务模板使用 `CHECKIN_HOME=/var/lib/telegram-checkin`，`StateDirectory` 会建立对应目录。确认实际安装路径、服务用户和数据目录一致。

桌面通过 SSH 隧道访问：

```bash
ssh -L 8577:127.0.0.1:8577 用户@服务器
```

打开 [http://127.0.0.1:8577](http://127.0.0.1:8577)，在「定时与通知」设置任意 `HH:MM`、IANA 时区和账号范围，再启用每日计划。没有可视桌面的 Linux 服务自动使用后台浏览器；发送预览会显示实际运行方式。

默认时区 `Asia/Hong_Kong`，不依赖服务器系统时区。服务自带调度器，不需要另加 cron；不需要 Codex 或桌面聊天保持在线。同一账号不要在多台服务器同时调度。

对外监听前必须配置 `CHECKIN_GUI_TOKEN`，并使用 HTTPS 和访问控制；仅通过 SSH 隧道时可保持回环监听。API 中的配置和运行证据属于私密资料。

## 4. 微信与正式验收

在「定时与通知」保存 Server酱 SendKey，或在服务环境文件配置 `SERVERCHAN_SENDKEY`。密钥不回显，不进入源码或发行包。推送结果分类展示；网络失败不改变签到结果，也不自动反复推送。

上线前依次验证：

1. `--doctor --browser` 通过：资源完整、数据可写、时区可用、浏览器实际启动。
2. 各账号可登录；只读体检能准确打开群和话题。
3. 在当天尚未发送的目标完成一次签到，核对真实反馈及原草稿恢复；已发或状态不明的会话先只读补查。
4. Server酱测试请求被接受，并在微信实际收到通知。
5. 自定义每日计划生效，重启后状态保留；确认相同日期不会重复触发。

环境诊断不检查 Telegram 网络、账号权限或微信通道；这些是目标服务器的实际验收条件。建议先运行几个完整日期，观察待核对项，再长期无人值守。

## 5. 升级与备份

```bash
sudo systemctl stop telegram-checkin
# 备份 CHECKIN_HOME 指向的整个目录，然后更新程序文件
# 源码用户按新版 requirements.lock.txt 安装依赖
sudo systemctl start telegram-checkin
sudo journalctl -u telegram-checkin -n 100 --no-pager
```

配置、登录资料、状态、历史、自定义图片和未恢复草稿必须保留。独立包数据目录与程序分离，直接替换程序目录；源码用户不要用示例配置覆盖真实配置。

计划在触发前持久化；中断后不会盲目重复触发。开启“错过当天时间补跑”时，仅补跑当天尚未触发的计划，不补发更早日期。

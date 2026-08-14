# DanmakuRender Linux 持久化部署

本文给出一套适合长期运行的 Ubuntu 24.04 LTS 部署方案：使用独立低权限用户、Python 虚拟环境和 `systemd`。Debian 等使用 systemd 的发行版也可参考，但包名可能不同。`systemd` 负责开机启动、异常退出重启、单实例和日志；不要再同时运行 `screen`、`tmux`、`nohup`、cron 或另一层 watchdog，否则可能重复录制、重复上传。

本文按以下固定目录编写：

| 用途 | 路径 |
| --- | --- |
| 程序代码 | `/opt/danmakurender/app` |
| Python 虚拟环境 | `/opt/danmakurender/venv` |
| 本机私密配置 | `/etc/danmakurender` |
| 录像和渲染结果 | `/srv/danmakurender` |
| 项目自身日志及运行状态 | `/var/lib/danmakurender` |

这种布局不会把密钥、Cookie 或录像混入 Git 工作树，今后同步 fork 更简单。下文假设代码仓库为 `https://github.com/cnxianyi/DanmakuRender.git`、分支为 `v5`；如果实际地址或分支不同，只修改对应命令。

部署前先在开发机确认所需提交已经推送：

```bash
git fetch origin
git status -sb
git rev-parse HEAD
git rev-parse origin/v5
```

如果状态显示 `ahead`，或两个提交哈希不同，新的服务器直接 clone 后可能缺少本机功能。确认提交内容无密钥后，再明确执行 `git push origin v5`；也可以推到另一个安全分支，并将下文的分支名改成那个分支。部署过程不会自动替你推送。

## 1. 系统准备

建议使用 Ubuntu 24.04 LTS 自带的 Python 3.12。下面所有管理命令由有 `sudo` 权限的账号执行。

```bash
sudo apt update
sudo apt install -y \
  git curl ca-certificates xz-utils \
  python3 python3-venv python3-pip \
  ffmpeg nodejs \
  fonts-noto-cjk fonts-noto-color-emoji
```

如果项目的某个平台需要 npm，再额外安装 `npm`。先检查关键组件：

```bash
python3 --version
ffmpeg -version | head -n 1
ffprobe -version | head -n 1
node --version
fc-match 'Noto Sans CJK SC'
```

硬件编码需要在此之外安装宿主机显卡驱动，并用 `ffmpeg -encoders` 验证编码器。第一次部署推荐先用 CPU 编码 `libx264` 跑通全流程，再切换 NVIDIA/Intel/AMD 硬件编码。

## 2. 创建服务用户和目录

```bash
sudo useradd --system \
  --home-dir /var/lib/danmakurender \
  --create-home \
  --shell /usr/sbin/nologin \
  danmakurender

sudo install -d -o root -g root -m 0755 \
  /opt/danmakurender \
  /opt/danmakurender/bin

sudo install -d -o root -g danmakurender -m 0750 \
  /etc/danmakurender

sudo install -d -o danmakurender -g danmakurender -m 0750 \
  /srv/danmakurender \
  /srv/danmakurender/source \
  /srv/danmakurender/rendered \
  /var/lib/danmakurender/logs \
  /var/lib/danmakurender/temp \
  /var/lib/danmakurender/cache \
  /var/lib/danmakurender/login_info
```

服务用户没有交互式 shell，并且无权修改程序代码，可以降低风险。代码、虚拟环境和二进制由 root 管理；运行数据由 `danmakurender` 用户管理。

## 3. 安装代码和 Python 依赖

```bash
sudo git clone \
  --branch v5 \
  https://github.com/cnxianyi/DanmakuRender.git \
  /opt/danmakurender/app

sudo git -C /opt/danmakurender/app log -1 --oneline

sudo python3 -m venv /opt/danmakurender/venv
sudo /opt/danmakurender/venv/bin/python -m pip install --upgrade pip
sudo /opt/danmakurender/venv/bin/python -m pip install \
  -r /opt/danmakurender/app/requirements.txt
```

固定使用虚拟环境中的 Python，不依赖 shell 是否执行过 `activate`。验证导入和程序版本：

```bash
sudo -u danmakurender \
  /opt/danmakurender/venv/bin/python -c \
  'import requests, aiohttp, yaml, PIL; print("Python dependencies OK")'

cd /opt/danmakurender/app
sudo -u danmakurender \
  /opt/danmakurender/venv/bin/python main.py --version
```

如果这里出现等待“回车自动安装”，说明依赖未装完整；不要让 systemd 处理交互式安装，先在命令行修复依赖。

## 4. 安装并固定 biliup-rs

只有自动上传 B 站时才需要此步骤。把经过校验的固定版本放在 Git 仓库之外，并在 YAML 中显式指定路径。以下示例固定为 `v0.2.4`。`biliup-rs` 上游仓库已经归档，所以固定并校验版本是有意为之；后续若 B 站接口变化，应先在测试环境验证替代版本，不能无人值守地自动升级。

先检查 CPU 架构：

```bash
uname -m
```

### x86_64

```bash
cd /tmp
curl -fL --retry 3 -o biliup.tar.xz \
  https://github.com/biliup/biliup-rs/releases/download/v0.2.4/biliupR-v0.2.4-x86_64-linux.tar.xz
echo 'c0e509424caec2ad5d7c3a049f686d7201891e749e0e7be8a4127232b70bf5d7  biliup.tar.xz' | sha256sum -c -
tar -xJf biliup.tar.xz
sudo install -o root -g root -m 0755 \
  biliupR-v0.2.4-x86_64-linux/biliup \
  /opt/danmakurender/bin/biliup
rm -rf -- /tmp/biliup.tar.xz /tmp/biliupR-v0.2.4-x86_64-linux
```

### aarch64 / arm64

```bash
cd /tmp
curl -fL --retry 3 -o biliup.tar.xz \
  https://github.com/biliup/biliup-rs/releases/download/v0.2.4/biliupR-v0.2.4-aarch64-linux.tar.xz
echo 'd21c149d2a3ef15b3bbadb16edc453a938bbfdbf9b848e11da795cc3d79629e2  biliup.tar.xz' | sha256sum -c -
tar -xJf biliup.tar.xz
sudo install -o root -g root -m 0755 \
  biliupR-v0.2.4-aarch64-linux/biliup \
  /opt/danmakurender/bin/biliup
rm -rf -- /tmp/biliup.tar.xz /tmp/biliupR-v0.2.4-aarch64-linux
```

验证二进制：

```bash
sudo -u danmakurender /opt/danmakurender/bin/biliup --version
```

不要把 Windows 的 `biliup.exe`、FFmpeg 路径或 Cookie 绝对路径原样复制到 Linux。

## 5. 放置本机配置

配置放在 `/etc/danmakurender`。当前的 `DMR-kami.yml`、`DMR-oyo.yml` 等任务配置受 `.gitignore` 保护，新的 clone 中不会存在，必须从原机器安全迁移，不能指望 `git clone` 带过去。

先在服务器登录账号的主目录创建临时接收目录：

```bash
install -d -m 0700 ~/dmr-config-transfer
```

然后在原 Windows 机器的 PowerShell、项目根目录中执行；把 `your-user@server` 改成真实 SSH 地址：

```powershell
scp .\configs\global.yml `
    .\configs\DMR-kami.yml `
    .\configs\DMR-oyo.yml `
    your-user@server:dmr-config-transfer/

scp .\tools\cookies.json `
    your-user@server:dmr-config-transfer/cookies.json
```

回到服务器，将配置安装到 `/etc`：

```bash
sudo install -o root -g danmakurender -m 0640 \
  ~/dmr-config-transfer/global.yml \
  /etc/danmakurender/global.yml

sudo install -o root -g danmakurender -m 0640 \
  ~/dmr-config-transfer/DMR-kami.yml \
  /etc/danmakurender/DMR-kami.yml

sudo install -o root -g danmakurender -m 0640 \
  ~/dmr-config-transfer/DMR-oyo.yml \
  /etc/danmakurender/DMR-oyo.yml
```

如果任务名不同，迁移自己的所有 `DMR-*.yml`。确认 `/etc/danmakurender` 中的文件完整后，可删除传输目录中的副本。

如果使用 B 站上传，再以同样方式传输 Cookie，并让服务用户拥有它：

```bash
sudo install -o danmakurender -g danmakurender -m 0600 \
  ~/dmr-config-transfer/cookies.json \
  /var/lib/danmakurender/login_info/bilibili.json
```

`main.py` 启动后会主动切换到代码目录，所以要从代码目录把运行状态链接到持久数据目录。代码目录由 root 管理，这些链接也由 root 创建：

```bash
sudo ln -s \
  /var/lib/danmakurender/temp \
  /opt/danmakurender/app/.temp
sudo ln -s \
  /var/lib/danmakurender/login_info \
  /opt/danmakurender/app/.login_info
sudo ln -s \
  /var/lib/danmakurender/logs \
  /opt/danmakurender/app/logs
```

如果目标已经存在，先确认里面是否有要保留的数据；迁移数据后再建立链接，不要直接删除非空目录。

编辑配置：

```bash
sudoedit /etc/danmakurender/global.yml
sudoedit /etc/danmakurender/DMR-kami.yml
sudoedit /etc/danmakurender/DMR-oyo.yml
```

至少检查以下内容：

```yaml
executable_tools_path:
  ffmpeg: /usr/bin/ffmpeg
  ffprobe: /usr/bin/ffprobe
  biliup: /opt/danmakurender/bin/biliup

dmr_engine_args:
  enabled_plugins: [downloader, render, uploader, cleaner]
  config_path: /etc/danmakurender

download_args:
  live:
    output_dir: /srv/danmakurender/source
    font: Noto Sans CJK SC

render_args:
  dmrender:
    output_dir: /srv/danmakurender/rendered
    hwaccel_args: []
    vencoder: libx264
    vencoder_args: ['-preset', 'medium', '-crf', '20']
```

实际层级以现有 YAML 为准；只替换对应字段，不要复制一个不完整的片段覆盖整份配置。任务配置会覆盖全局配置，所以也要检查每个 `DMR-*.yml` 中的 `output_dir`、`font`、`render_args` 和 Cookie 路径。

特别注意：

- 把 `C:\...`、`D:\...` 全部改为 Linux 绝对路径。
- Windows 的 `h264_amf` 通常不能直接用于 Linux。首次运行使用上面的 `libx264`；稳定后再根据硬件改为已验证的编码器。
- Windows 字体 `Segoe UI Emoji`、`Microsoft YaHei` 在 Linux 上通常不存在，改用 `Noto Sans CJK SC`；可通过 `fc-match` 确认。
- B 站 Cookie 建议保存为 `/var/lib/danmakurender/login_info/<账号>.json`，权限设为 `0600`。
- `api_key`、Telegram Bot Token 和 Cookie 属于私密数据。即使直接写入 YAML，也必须保持 `/etc/danmakurender/*.yml` 为 `0640`，且不要提交到 Git。
- AI 缓存、失败上传任务状态位于运行目录下的 `.temp`。这个目录不能放在系统重启会自动清空的 `/tmp`。

检查是否还残留 Windows 路径或编码器：

```bash
sudo grep -RInE '[A-Za-z]:\\|h264_amf|Segoe UI Emoji|Microsoft YaHei' \
  /etc/danmakurender || true
```

检查 YAML 语法：

```bash
cd /opt/danmakurender/app
sudo -u danmakurender \
  /opt/danmakurender/venv/bin/python - <<'PY'
import yaml
from pathlib import Path

for path in Path('/etc/danmakurender').glob('*.yml'):
    with path.open(encoding='utf-8') as file:
        yaml.safe_load(file)
    print('YAML OK:', path)
PY
```

检查程序能否加载全局配置和任务：

```bash
cd /opt/danmakurender/app
sudo -u danmakurender \
  /opt/danmakurender/venv/bin/python - <<'PY'
from DMR.Config import Config
config = Config('/etc/danmakurender/global.yml')
print('Tasks:', config.get_replaytasks())
PY
```

## 6. 前台试运行

必须先前台运行，确认至少能完成配置加载、直播间检查、FFmpeg 调用、截图和 Telegram 请求，再注册服务：

```bash
cd /opt/danmakurender/app
sudo -u danmakurender env \
  HOME=/var/lib/danmakurender \
  XDG_CACHE_HOME=/var/lib/danmakurender/cache \
  PATH=/opt/danmakurender/venv/bin:/opt/danmakurender/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin \
  PYTHONUNBUFFERED=1 \
  /opt/danmakurender/venv/bin/python -u \
  /opt/danmakurender/app/main.py \
  --skip_update \
  --config /etc/danmakurender/global.yml
```

按 `Ctrl+C` 停止。正常情况下末尾应出现 `DMR engine stoped.`。如果仍在录制、渲染或上传，先观察其完成或确认中断风险后再停止。

## 7. 创建 systemd 服务

创建 `/etc/systemd/system/danmakurender.service`：

```bash
sudo tee /etc/systemd/system/danmakurender.service >/dev/null <<'EOF'
[Unit]
Description=DanmakuRender live recorder and uploader
Wants=network-online.target
After=network-online.target
RequiresMountsFor=/srv/danmakurender /var/lib/danmakurender
StartLimitIntervalSec=300
StartLimitBurst=5

[Service]
Type=exec
User=danmakurender
Group=danmakurender
WorkingDirectory=/opt/danmakurender/app
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONDONTWRITEBYTECODE=1
Environment=HOME=/var/lib/danmakurender
Environment=XDG_CACHE_HOME=/var/lib/danmakurender/cache
Environment=PATH=/opt/danmakurender/venv/bin:/opt/danmakurender/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin
ExecStartPre=/usr/bin/test -r /etc/danmakurender/global.yml
ExecStartPre=/usr/bin/test -x /opt/danmakurender/venv/bin/python
ExecStartPre=/usr/bin/test -w /srv/danmakurender/source
ExecStartPre=/usr/bin/test -w /srv/danmakurender/rendered
ExecStartPre=/usr/bin/test -w /var/lib/danmakurender/temp
ExecStart=/opt/danmakurender/venv/bin/python -u /opt/danmakurender/app/main.py --skip_update --config /etc/danmakurender/global.yml

# main.py 会捕获 KeyboardInterrupt 并调用 dmr.stop()，因此用 SIGINT 做有序停止。
KillSignal=SIGINT
KillMode=mixed
TimeoutStopSec=180
FinalKillSignal=SIGKILL

Restart=always
RestartSec=10

StandardOutput=journal
StandardError=journal
SyslogIdentifier=danmakurender

UMask=0027
NoNewPrivileges=true
PrivateTmp=true
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
EOF
```

这里使用 `Restart=always`，任何非人工停止的进程退出都会重新拉起；管理员执行 `systemctl stop` 后，systemd 仍会保持停止，不会自己复活。`StartLimit*` 可阻止配置错误时无限快速拉起。`KillMode=mixed` 会先把 `SIGINT` 发给 Python 主进程以执行 `dmr.stop()`，超时后才清理残留的 FFmpeg/biliup 子进程。

如果机器使用 NVIDIA/Intel 视频设备，还需把服务用户加入相应组，然后重新登录或重启服务：

```bash
sudo usermod -aG video danmakurender
getent group render >/dev/null && sudo usermod -aG render danmakurender
```

有些发行版不存在 `render` 组；此时只加入实际存在的组。

加载、启用并立即启动：

```bash
sudo systemd-analyze verify /etc/systemd/system/danmakurender.service
sudo systemctl daemon-reload
sudo systemctl enable --now danmakurender.service
```

## 8. 日常管理

查看状态：

```bash
systemctl status danmakurender.service --no-pager -l
systemctl is-enabled danmakurender.service
systemctl is-active danmakurender.service
```

实时查看 systemd 日志：

```bash
journalctl -u danmakurender.service -f
```

查看本次启动、最近两小时或最近 200 行：

```bash
journalctl -u danmakurender.service -b --no-pager
journalctl -u danmakurender.service --since '2 hours ago'
journalctl -u danmakurender.service -n 200 --no-pager
```

项目自身的 DEBUG 日志仍写入 `/var/lib/danmakurender/logs/`。单次进程内有轮转，但频繁重启产生的旧文件不保证自动清完，因此后文会设置统一的 30 天清理策略：

```bash
ls -lh /var/lib/danmakurender/logs
tail -F /var/lib/danmakurender/logs/DMR-*.log
```

停止、启动和重启：

```bash
sudo systemctl stop danmakurender.service
sudo systemctl start danmakurender.service
sudo systemctl restart danmakurender.service
```

永久取消开机自启并立即停止：

```bash
sudo systemctl disable --now danmakurender.service
```

配置改动规则：

- `DMR-*.yml` 任务配置支持动态检测，一般等待约一分钟即可重载；重大修改仍建议重启。
- `global.yml` 改动后程序只提示需要重启，执行 `sudo systemctl restart danmakurender.service`。
- 修改 `.service` 后必须执行 `sudo systemctl daemon-reload`，然后重启服务。

检查是否意外运行了多个实例：

```bash
systemctl show danmakurender.service -p MainPID -p NRestarts
pgrep -a -f '/opt/danmakurender/app/main.py'
pgrep -a -f 'ffmpeg|biliup'
```

正常情况下 `main.py` 只有一个。FFmpeg 可能因同时录制和渲染而存在多个，这是正常的。

## 9. journald 持久日志与限额

Ubuntu/Debian 通常由 journald 自动管理轮转。若 `/var/log/journal` 不存在，日志可能只保留到重启；可开启持久保存并设上限：

```bash
sudo mkdir -p /var/log/journal
sudo install -d -m 0755 /etc/systemd/journald.conf.d
sudo systemd-tmpfiles --create --prefix /var/log/journal
sudo tee /etc/systemd/journald.conf.d/danmakurender.conf >/dev/null <<'EOF'
[Journal]
Storage=persistent
SystemMaxUse=1G
MaxRetentionSec=30day
Compress=yes
EOF
sudo systemctl restart systemd-journald
```

检查占用：

```bash
journalctl --disk-usage
```

再为项目自身日志设置 30 天保留期；这里只清理 `logs`，不会碰录像、AI 缓存或失败上传记录：

```bash
sudo tee /etc/tmpfiles.d/danmakurender.conf >/dev/null <<'EOF'
d /var/lib/danmakurender/logs 0750 danmakurender danmakurender 30d -
EOF
sudo systemd-tmpfiles --create /etc/tmpfiles.d/danmakurender.conf
sudo systemd-tmpfiles --clean /etc/tmpfiles.d/danmakurender.conf
systemctl status systemd-tmpfiles-clean.timer --no-pager
```

## 10. 稳定更新和回滚

不要在服务运行期间直接 `git pull` 或升级 Python 包。最好等待本场直播的录制、渲染、上传和 BV 标题更新全部完成；部分分组及 Telegram 回复映射只保存在内存中。先记录当前提交，再停止服务：

```bash
cd /opt/danmakurender/app
sudo git rev-parse HEAD
sudo systemctl stop danmakurender.service
```

确认停止后没有正在写入的 Python/FFmpeg/biliup 进程：

```bash
systemctl is-active danmakurender.service
pgrep -a -f '/opt/danmakurender/app/main.py|ffmpeg|biliup' || true
```

同步 fork 并更新依赖：

```bash
cd /opt/danmakurender/app
sudo git status --short
sudo git fetch --prune origin
sudo git pull --ff-only origin v5
sudo /opt/danmakurender/venv/bin/python -m pip install \
  -r requirements.txt
```

`git status --short` 应为空。私密配置和录像位于仓库外，因此不会妨碍 `--ff-only` 更新。先运行测试，再启动服务：

```bash
cd /opt/danmakurender/app
sudo -u danmakurender \
  /opt/danmakurender/venv/bin/python -m unittest discover -p 'test*.py'
sudo systemctl start danmakurender.service
journalctl -u danmakurender.service -n 100 -f
```

若新版本异常，停止服务并回滚到刚才记录的已知良好提交。把 `GOOD_COMMIT_HASH` 替换为确切哈希，不要对不确定目录执行 reset：

```bash
sudo systemctl stop danmakurender.service
cd /opt/danmakurender/app
sudo git switch --detach GOOD_COMMIT_HASH
sudo /opt/danmakurender/venv/bin/python -m pip install \
  -r requirements.txt
sudo systemctl start danmakurender.service
```

确认稳定后，再决定何时切回 `v5` 分支。

## 11. 备份与恢复

至少备份以下内容：

- `/etc/danmakurender/`：全局配置、任务配置、AI 和 Telegram 设置。
- `/var/lib/danmakurender/login_info/`：B 站登录 Cookie。
- `/var/lib/danmakurender/temp/failed_uploads.json`：失败上传任务记录；它不等于文件上传断点自动续传。
- `/var/lib/danmakurender/temp/ai_rename_cache.json`：AI 识别缓存，可选。
- `/srv/danmakurender/`：录像和渲染结果；通常体积最大，单独制定保留策略。

备份前最好有序停止服务，或使用支持一致性快照的文件系统。不要只备份 Git 仓库，因为运行状态和私密配置不在仓库内。

同时监控磁盘空间：

```bash
df -h /srv/danmakurender /var/lib/danmakurender
du -sh /srv/danmakurender/* /var/lib/danmakurender/* 2>/dev/null
```

建议为录像盘设置外部告警；磁盘写满会同时破坏录制、渲染、缓存和上传恢复。

## 12. 故障排查

### 服务不断重启

```bash
systemctl status danmakurender.service --no-pager -l
journalctl -u danmakurender.service -n 300 --no-pager
systemctl show danmakurender.service -p Result -p ExecMainCode -p ExecMainStatus -p NRestarts
```

修复配置后，如果触发了启动限速：

```bash
sudo systemctl reset-failed danmakurender.service
sudo systemctl start danmakurender.service
```

### FFmpeg、字体或编码器错误

```bash
sudo -u danmakurender /usr/bin/ffmpeg -hide_banner -encoders | grep -E 'libx264|nvenc|qsv|vaapi|amf'
sudo -u danmakurender fc-match 'Noto Sans CJK SC'
sudo -u danmakurender test -w /srv/danmakurender/source
sudo -u danmakurender test -w /srv/danmakurender/rendered
```

优先回到 `hwaccel_args: []` 和 `vencoder: libx264` 排除驱动问题。

### Telegram 图片能发送，但 `/update` 无响应

- `tg.enabled`、`bot_token`、`chat_id` 必须配置正确。
- `/update 游戏名` 必须回复相册中的任意一张图片，不能只发送一条独立消息。
- 回复需发生在 `reply_window` 内。
- 相册与视频分组的对应关系保存在内存中；服务重启后，重启前相册的 `/update` 回复不能再映射到原 BV。
- 同一个 Bot Token 不应同时被其他程序调用 `getUpdates`，否则更新可能被另一个实例消费。
- 检查 `journalctl -u danmakurender.service -f` 中的 Telegram 请求错误。

### B 站上传或改标题失败

```bash
sudo -u danmakurender test -r /var/lib/danmakurender/login_info/ACCOUNT.json
sudo -u danmakurender /opt/danmakurender/bin/biliup --version
ls -l /var/lib/danmakurender/temp/failed_uploads.json
```

确认 Cookie 路径为 Linux 路径、文件属于 `danmakurender` 用户可读，并检查 B 站接口是否要求重新登录。

### 停止服务后显示 inactive，但仍有进程

```bash
sudo systemctl kill --kill-who=all --signal=SIGKILL danmakurender.service
pgrep -a -f '/opt/danmakurender/app/main.py|ffmpeg|biliup' || true
```

只在正常 `systemctl stop` 和 180 秒超时仍无法收尾时使用强制终止；强杀可能留下不完整视频或中断上传。

## 13. 上线验收清单

- `systemd-analyze verify` 无错误。
- `systemctl is-enabled` 返回 `enabled`，`systemctl is-active` 返回 `active`。
- `pgrep` 只有一个 `main.py` 主实例。
- 所有 `C:\`、`D:\` 路径和 Windows 编码器、字体已替换。
- 服务用户对源视频、弹幕版、日志、`.temp` 和 Cookie 目录权限正确。
- 已完成一次真实的录制分段、渲染、三张截图/Telegram、上传和 BV 标题更新测试。
- 执行一次 `sudo systemctl restart danmakurender.service` 后能恢复工作。
- 重启整台机器后服务自动启动。
- 已配置磁盘容量告警和配置/Cookie/失败任务状态备份。

完成以上检查后，这套部署即可作为长期运行的基线。

## 14. 参考资料

- [systemd 服务及重启策略](https://www.freedesktop.org/software/systemd/man/latest/systemd.service.html)
- [systemd 进程终止策略](https://www.freedesktop.org/software/systemd/man/latest/systemd.kill.html)
- [journald 配置](https://www.freedesktop.org/software/systemd/man/latest/journald.conf.html)
- [Python `venv` 官方文档](https://docs.python.org/3/library/venv.html)
- [biliup-rs v0.2.4 发布页](https://github.com/biliup/biliup-rs/releases/tag/v0.2.4)

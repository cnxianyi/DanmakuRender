# Windows 进程守护与 Telegram 通知

本目录中的脚本运行在 DanmakuRender 外部，不修改 Python 业务代码或直播任务 YAML 配置。

守护脚本提供以下功能：

- DanmakuRender 退出后自动重启。
- 发现 `ERROR`、`CRITICAL` 或 Python traceback 时发送 Telegram 通知。
- 检测到直播开始和直播结束时发送 Telegram 通知。
- 对重复的开播日志进行去重，避免录制重试时反复通知。
- 通过 Windows 计划任务在用户登录后自动启动。

## 初始配置

以下命令都需要在项目根目录执行：

```powershell
cd C:\Users\thefa\Videos\DanmakuRender
```

第一次使用时，复制本机配置模板：

```powershell
Copy-Item .\ops\watchdog.local.example.psd1 .\ops\watchdog.local.psd1
```

查看当前 Python 的绝对路径：

```powershell
python -c "import sys; print(sys.executable)"
```

编辑 `ops\watchdog.local.psd1`，设置 Python 和 Telegram：

```powershell
@{
    PythonCommand = 'C:\Users\thefa\AppData\Local\Microsoft\WindowsApps\python.exe'
    TelegramBotToken = '123456:ABCDEF'
    TelegramChatId = '7129142702'
    TelegramApiBase = 'https://api.telegram.org'
    TelegramProxy = ''
    TelegramMessageThreadId = ''
    PythonArguments = @('main.py', '--skip_update')
    NotificationCooldownSeconds = 300
    ErrorPattern = '\[(ERROR|CRITICAL)\]|Traceback \(most recent call last\):|Unhandled exception|Fatal Python error'
}
```

`watchdog.local.psd1` 包含 Telegram Bot Token，已经通过 `.gitignore` 排除，不会被 Git 跟踪。

## 测试 Telegram

只发送测试通知，不启动 DanmakuRender：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\ops\watchdog.ps1 -TestNotification
```

## 前台测试守护

先停止已经运行的计划任务，避免启动两个实例：

```powershell
Stop-ScheduledTask -TaskName 'DanmakuRender Watchdog'
powershell -NoProfile -ExecutionPolicy Bypass -File .\ops\watchdog.ps1
```

按 `Ctrl+C` 停止前台守护。

## 注册登录自启

只需要执行一次：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\ops\register-startup-task.ps1
```

注册后立即启动：

```powershell
Start-ScheduledTask -TaskName 'DanmakuRender Watchdog'
```

## 常用管理命令

查看任务状态：

```powershell
Get-ScheduledTask -TaskName 'DanmakuRender Watchdog' |
    Select-Object TaskName, State
```

查看最近运行结果：

```powershell
Get-ScheduledTaskInfo -TaskName 'DanmakuRender Watchdog' |
    Select-Object LastRunTime, LastTaskResult
```

停止守护：

```powershell
Stop-ScheduledTask -TaskName 'DanmakuRender Watchdog'
```

启动守护：

```powershell
Start-ScheduledTask -TaskName 'DanmakuRender Watchdog'
```

修改 `watchdog.ps1` 或 `watchdog.local.psd1` 后，重启守护使配置生效：

```powershell
Stop-ScheduledTask -TaskName 'DanmakuRender Watchdog'
Start-ScheduledTask -TaskName 'DanmakuRender Watchdog'
```

删除登录自启任务：

```powershell
Unregister-ScheduledTask -TaskName 'DanmakuRender Watchdog'
```

## 查看日志

实时查看守护日志：

```powershell
Get-Content .\ops\watchdog.log -Tail 50 -Wait
```

查看最新的 DanmakuRender 日志文件：

```powershell
Get-ChildItem .\logs -File |
    Sort-Object LastWriteTime -Descending |
    Select-Object -First 5 Name, Length, LastWriteTime
```

## 通知格式

开播通知：

```text
标题：直播开始
内容：kami
```

结束通知：

```text
标题：直播结束
内容：kami
```

任务名会根据日志自动替换为 `kami`、`oyo` 或其他任务名。程序启动时直播间本来就是离线状态，不会误发直播结束通知。

守护生命周期和 Telegram 请求失败记录在 `ops\watchdog.log`；DanmakuRender 自身日志继续写入 `logs\`。

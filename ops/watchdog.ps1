[CmdletBinding()]
param(
    [string]$ConfigFile,
    [string]$PythonCommand,
    [string]$TelegramBotToken,
    [string]$TelegramChatId,
    [string]$TelegramApiBase,
    [string]$TelegramProxy,
    [string]$TelegramMessageThreadId,
    [int]$MaxRestarts = 0,
    [switch]$TestNotification
)

$ErrorActionPreference = 'Stop'
if (-not $ConfigFile) { $ConfigFile = Join-Path $PSScriptRoot 'watchdog.local.psd1' }
$projectRoot = Split-Path -Parent $PSScriptRoot
$settings = @{}

if (Test-Path -LiteralPath $ConfigFile) {
    $settings = Import-PowerShellDataFile -LiteralPath $ConfigFile
}

if (-not $PythonCommand) { $PythonCommand = $settings.PythonCommand }
if (-not $PythonCommand) { $PythonCommand = 'python' }
if (-not $TelegramBotToken) { $TelegramBotToken = $settings.TelegramBotToken }
if (-not $TelegramBotToken) { $TelegramBotToken = $env:DMR_TG_BOT_TOKEN }
if (-not $TelegramChatId) { $TelegramChatId = $settings.TelegramChatId }
if (-not $TelegramChatId) { $TelegramChatId = $env:DMR_TG_CHAT_ID }
if (-not $TelegramApiBase) { $TelegramApiBase = $settings.TelegramApiBase }
if (-not $TelegramApiBase) { $TelegramApiBase = 'https://api.telegram.org' }
if (-not $TelegramProxy) { $TelegramProxy = $settings.TelegramProxy }
if (-not $TelegramProxy) { $TelegramProxy = $env:DMR_TG_PROXY }
if (-not $TelegramMessageThreadId) { $TelegramMessageThreadId = $settings.TelegramMessageThreadId }
if (-not $MaxRestarts -and $settings.MaxRestarts) { $MaxRestarts = [int]$settings.MaxRestarts }

$pythonArguments = @('main.py', '--skip_update')
if ($settings.PythonArguments) { $pythonArguments = @($settings.PythonArguments) }

$errorPattern = '\[(ERROR|CRITICAL)\]|Traceback \(most recent call last\):|Unhandled exception|Fatal Python error'
if ($settings.ErrorPattern) { $errorPattern = $settings.ErrorPattern }

$cooldownSeconds = 300
if ($settings.NotificationCooldownSeconds) { $cooldownSeconds = [int]$settings.NotificationCooldownSeconds }

$liveStartPattern = '^\[[^\]]+\](?:\[[^\]]+\])?\[INFO\]:\s*(?<task>[^:]+):\s*\u76F4\u64AD\u5F00\u59CB[.\u3002]?\s*$'
$liveEndPattern = '^\[[^\]]+\](?:\[[^\]]+\])?\[INFO\]:\s*(?<task>[^:]+):\s*\u76F4\u64AD(?:\u5DF2)?\u7ED3\u675F[.\u3002]?\s*$'
$liveStartedTitle = [string]([char]0x76F4) + [char]0x64AD + [char]0x5F00 + [char]0x59CB
$liveEndedTitle = [string]([char]0x76F4) + [char]0x64AD + [char]0x7ED3 + [char]0x675F
$restartDelay = 5
$maxRestartDelay = 60
$stableRunSeconds = 600
$notificationTimes = @{}
$liveTasks = @{}
$restartCount = 0
$machineName = $env:COMPUTERNAME
$watchdogLog = Join-Path $PSScriptRoot 'watchdog.log'

function Write-WatchdogLog {
    param([string]$Message)
    $line = '[{0}] {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Message
    Write-Host $line
    Add-Content -LiteralPath $watchdogLog -Value $line -Encoding UTF8
}

function Send-TelegramNotification {
    param(
        [string]$Title,
        [string]$Body,
        [string]$CooldownKey = 'general'
    )

    if (-not $TelegramBotToken -or -not $TelegramChatId) { return $false }
    $now = Get-Date
    if ($script:notificationTimes.ContainsKey($CooldownKey) -and
        ($now - $script:notificationTimes[$CooldownKey]).TotalSeconds -lt $cooldownSeconds) {
        return $false
    }

    try {
        $payload = @{
            chat_id = $TelegramChatId
            text = "$Title`n$Body"
        }
        if ($TelegramMessageThreadId) { $payload.message_thread_id = $TelegramMessageThreadId }
        $request = @{
            Method = 'Post'
            Uri = "{0}/bot{1}/sendMessage" -f $TelegramApiBase.TrimEnd('/'), $TelegramBotToken
            Body = $payload
            ContentType = 'application/x-www-form-urlencoded'
            TimeoutSec = 60
        }
        if ($TelegramProxy) { $request.Proxy = $TelegramProxy }
        $response = Invoke-RestMethod @request
        if (-not $response.ok) { throw [Exception]($response.description) }
        $script:notificationTimes[$CooldownKey] = $now
        return $true
    }
    catch {
        Write-WatchdogLog "Telegram notification failed: $($_.Exception.Message)"
        return $false
    }
}

if ($TestNotification) {
    if (-not $TelegramBotToken -or -not $TelegramChatId) { throw 'TelegramBotToken and TelegramChatId must be set in watchdog.local.psd1 first.' }
    $sent = Send-TelegramNotification -Title "DanmakuRender test on $machineName" -Body 'The Windows watchdog can reach Telegram.' -CooldownKey 'test'
    if (-not $sent) { throw 'Telegram test notification failed. See ops\watchdog.log for details.' }
    Write-WatchdogLog 'Telegram test notification sent.'
    return
}

$mutexId = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($projectRoot))
$mutexName = 'Local\DanmakuRenderWatchdog-' + $mutexId.Replace('=', '').Replace('/', '_').Replace('+', '-')
$mutex = New-Object Threading.Mutex($false, $mutexName)
if (-not $mutex.WaitOne(0, $false)) {
    $mutex.Dispose()
    throw 'Another DanmakuRender watchdog is already running for this directory.'
}

try {
    Set-Location -LiteralPath $projectRoot
    Write-WatchdogLog "Watchdog started. Python command: $PythonCommand"

    while ($true) {
        $startedAt = Get-Date
        $exitCode = -1
        Write-WatchdogLog "Starting DanmakuRender: $PythonCommand $($pythonArguments -join ' ')"

        $previousErrorActionPreference = $ErrorActionPreference
        try {
            # PowerShell 5 represents native stderr as ErrorRecord objects. Keep those
            # in the merged stream instead of turning ordinary stderr into a script error.
            $ErrorActionPreference = 'Continue'
            $LASTEXITCODE = -1
            & $PythonCommand @pythonArguments 2>&1 | ForEach-Object {
                $line = $_.ToString()
                Write-Host $line

                if ($line -match $liveEndPattern) {
                    $taskName = $Matches['task'].Trim()
                    $wasLive = $script:liveTasks.ContainsKey($taskName)
                    $script:liveTasks.Remove($taskName) | Out-Null
                    Write-WatchdogLog "Live-end event detected: $taskName"
                    if ($wasLive) {
                        $endCooldownKey = "live-end:$taskName"
                        $script:notificationTimes.Remove($endCooldownKey) | Out-Null
                        Send-TelegramNotification -Title $liveEndedTitle -Body $taskName -CooldownKey $endCooldownKey | Out-Null
                    }
                }
                elseif ($line -match $liveStartPattern) {
                    $taskName = $Matches['task'].Trim()
                    if (-not $script:liveTasks.ContainsKey($taskName)) {
                        $script:liveTasks[$taskName] = $true
                        $startCooldownKey = "live-start:$taskName"
                        $script:notificationTimes.Remove($startCooldownKey) | Out-Null
                        Send-TelegramNotification -Title $liveStartedTitle -Body $taskName -CooldownKey $startCooldownKey | Out-Null
                        Write-WatchdogLog "Live-start event detected: $taskName"
                    }
                }

                if ($line -match $errorPattern) {
                    $shortLine = if ($line.Length -gt 800) { $line.Substring(0, 800) + '...' } else { $line }
                    Send-TelegramNotification -Title "DanmakuRender error on $machineName" -Body $shortLine -CooldownKey 'error' | Out-Null
                }
            }
            $exitCode = $LASTEXITCODE
        }
        catch {
            $startError = $_.Exception.Message
            Write-WatchdogLog "Failed to run DanmakuRender: $startError"
            Send-TelegramNotification -Title "DanmakuRender failed on $machineName" -Body $startError -CooldownKey 'error' | Out-Null
        }
        finally {
            $ErrorActionPreference = $previousErrorActionPreference
        }

        $restartCount++
        $runSeconds = [int]((Get-Date) - $startedAt).TotalSeconds
        Write-WatchdogLog "DanmakuRender exited with code $exitCode after ${runSeconds}s."
        Send-TelegramNotification -Title "DanmakuRender stopped on $machineName" -Body "Exit code: $exitCode`nRuntime: ${runSeconds}s`nThe watchdog will restart it." -CooldownKey 'error' | Out-Null

        if ($MaxRestarts -gt 0 -and $restartCount -ge $MaxRestarts) {
            Write-WatchdogLog "Maximum restart count ($MaxRestarts) reached; watchdog exiting."
            break
        }

        $delayThisTime = $restartDelay
        Write-WatchdogLog "Restarting in ${delayThisTime}s."
        Start-Sleep -Seconds $delayThisTime

        if ($runSeconds -ge $stableRunSeconds) {
            $restartDelay = 5
        }
        else {
            $restartDelay = [Math]::Min($restartDelay * 2, $maxRestartDelay)
        }
    }
}
finally {
    $mutex.ReleaseMutex()
    $mutex.Dispose()
}

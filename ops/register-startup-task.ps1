[CmdletBinding()]
param(
    [string]$TaskName = 'DanmakuRender Watchdog'
)

$ErrorActionPreference = 'Stop'
$watchdog = Join-Path $PSScriptRoot 'watchdog.ps1'
$config = Join-Path $PSScriptRoot 'watchdog.local.psd1'

if (-not (Test-Path -LiteralPath $config)) {
    throw "Create $config from watchdog.local.example.psd1 first."
}

$arguments = '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{0}" -ConfigFile "{1}"' -f $watchdog, $config
$projectRoot = Split-Path -Parent $PSScriptRoot
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $arguments -WorkingDirectory $projectRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$taskSettings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $taskSettings -Description 'Runs DanmakuRender under an external watchdog and sends Telegram alerts.' -Force | Out-Null
Write-Host "Registered scheduled task: $TaskName"
Write-Host "Start it now with: Start-ScheduledTask -TaskName '$TaskName'"

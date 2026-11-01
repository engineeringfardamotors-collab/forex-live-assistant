# نصب اجرای خودکار دستیار لایو فارکس در زمان ورود به ویندوز (Task Scheduler)
# اجرا:  powershell -ExecutionPolicy Bypass -File scripts\install_autostart_task.ps1
# حذف:  scripts\uninstall_autostart_task.ps1

$ErrorActionPreference = "Stop"
$taskName = "ForexLiveAssistant"
$batPath = Join-Path $PSScriptRoot "..\start_forever.bat"
$batPath = [System.IO.Path]::GetFullPath($batPath)

if (-not (Test-Path $batPath)) {
    Write-Error "start_forever.bat پیدا نشد: $batPath"
}

$action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"$batPath`"" -WorkingDirectory (Split-Path $batPath)
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal -Force | Out-Null

Write-Host "✔ تسک «$taskName» نصب شد: با هر ورود به ویندوز، start_forever.bat اجرا می‌شود." -ForegroundColor Green
Write-Host "  اجرای فوری: Start-ScheduledTask -TaskName $taskName"
Write-Host "  حذف:        scripts\uninstall_autostart_task.ps1"

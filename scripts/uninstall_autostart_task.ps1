# حذف تسک اجرای خودکار دستیار لایو فارکس
$taskName = "ForexLiveAssistant"
if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    Write-Host "✔ تسک «$taskName» حذف شد." -ForegroundColor Green
} else {
    Write-Host "تسک «$taskName» نصب نبود."
}

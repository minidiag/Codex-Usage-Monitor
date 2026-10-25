$usageShortcutPath = Join-Path ([Environment]::GetFolderPath('Startup')) 'Codex Usage Monitor.lnk'
if (Test-Path -LiteralPath $usageShortcutPath) { Remove-Item -LiteralPath $usageShortcutPath }
Write-Output '已取消当前用户的登录自启动；需要停止当前运行时，请运行 Stop-UsageMonitor.ps1。'

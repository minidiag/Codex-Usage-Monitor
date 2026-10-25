$ErrorActionPreference='Stop'
$usageProjectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$usageStartupRoot = [Environment]::GetFolderPath('Startup')
$usageShortcutPath = Join-Path $usageStartupRoot 'Codex Usage Monitor.lnk'
$usageShell = New-Object -ComObject WScript.Shell
$usageShortcut = $usageShell.CreateShortcut($usageShortcutPath)
$usageShortcut.TargetPath = Join-Path $usageProjectRoot '.local/client/CodexUsageMonitor.exe'
$usageShortcut.Arguments = ''
$usageShortcut.WorkingDirectory = $usageProjectRoot
$usageShortcut.WindowStyle = 1
$usageShortcut.Description = '按 Codex 账号、Windows 用户和电脑查看用量、活跃会话及额度'
$usageShortcut.Save()
$usageDesktopShortcut = $usageShell.CreateShortcut((Join-Path ([Environment]::GetFolderPath('Desktop')) 'Codex 用量统计.lnk'))
$usageDesktopShortcut.TargetPath = $usageShortcut.TargetPath
$usageDesktopShortcut.Arguments = $usageShortcut.Arguments
$usageDesktopShortcut.WorkingDirectory = $usageProjectRoot
$usageDesktopShortcut.WindowStyle = 1
$usageDesktopShortcut.Description = '查看多账号用量、Windows 用户统计、活跃会话和剩余额度'
$usageDesktopShortcut.Save()
Write-Output '已设置当前 Windows 用户登录时自动启动采集器和悬浮窗。'

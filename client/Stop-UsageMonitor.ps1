$usageProjectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
. (Join-Path $PSScriptRoot 'Paths.ps1')
$usageStopPath = Join-Path $usageState 'stop.request'
[void](New-Item -ItemType Directory -Force -Path ([IO.Path]::GetDirectoryName($usageStopPath)))
[IO.File]::WriteAllText($usageStopPath,'stop')
Write-Output '已请求停止采集器和悬浮窗；已保存的数据和待传队列会保留。'

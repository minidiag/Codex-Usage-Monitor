param([switch]$NoWindow)
$ErrorActionPreference='Stop'
$usageProjectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
. (Join-Path $PSScriptRoot 'Paths.ps1')
[void](New-Item -ItemType Directory -Force -Path $usageState)
$usageStop = Join-Path $usageState 'stop.request'
if (Test-Path -LiteralPath $usageStop) { Remove-Item -LiteralPath $usageStop }
$usageConfiguredPython=$null
try { $usageConfiguredPython=(Get-Content -LiteralPath (Join-Path $usageState 'runtime.json') -Raw | ConvertFrom-Json).pythonw } catch {}
$usagePythonCandidates = @(
    $usageConfiguredPython,
    (Join-Path $env:LOCALAPPDATA 'Programs/Python/Python312/pythonw.exe'),
    (Join-Path $usageProjectRoot '.local/venv/Scripts/pythonw.exe')
)
$usagePython = $usagePythonCandidates | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -First 1
if (-not $usagePython) { throw '未找到 Python 3.12，请检查运行环境。' }
$usageArguments = @('-m','client.agent','--state',('"'+$usageState+'"'))
if ($NoWindow) {
    Start-Process -FilePath $usagePython -ArgumentList $usageArguments -WorkingDirectory $usageProjectRoot -WindowStyle Hidden
} else {
    $usageWindowExe=Join-Path $usageProjectRoot '.local/client/CodexUsageMonitor.exe'
    if (-not (Test-Path -LiteralPath $usageWindowExe)) { & (Join-Path $PSScriptRoot 'Build-Window.ps1') }
    Start-Process -FilePath $usageWindowExe -WorkingDirectory $usageProjectRoot -WindowStyle Hidden
}

# UTF-8 BOM is required for Windows PowerShell 5.1 Chinese strings.
$ErrorActionPreference='Stop'
$usageProjectRoot=[IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
. (Join-Path $PSScriptRoot 'Paths.ps1')
$usageRuntime=Get-Content -LiteralPath (Join-Path $usageState 'runtime.json') -Raw | ConvertFrom-Json
$usagePython=Join-Path ([IO.Path]::GetDirectoryName($usageRuntime.pythonw)) 'python.exe'
if (-not (Test-Path -LiteralPath $usagePython)) { throw '找不到当前监控使用的 Python。' }
$usageToken=$null
Push-Location $usageProjectRoot
try {
    $usageToken=& $usagePython -c "import sys;from client.transport import load_credentials;print(load_credentials(sys.argv[1])['read_token'])" $usageCredentials
    if ($LASTEXITCODE -ne 0 -or -not $usageToken -or $usageToken -notmatch '^[A-Za-z0-9_-]{32,256}$') { throw '读取本用户只读凭据失败。' }
    Set-Clipboard -Value $usageToken
    Write-Output '已复制遥测只读凭据。请粘贴到仪表盘的“只读凭据”框并连接；凭据没有输出到终端。'
}
finally { $usageToken=$null; Pop-Location }

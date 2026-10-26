param([string]$CredentialImport,[string]$PythonExe,[string[]]$CodexHome,[string]$QuotaProxy)
$ErrorActionPreference='Stop'
. (Join-Path $PSScriptRoot 'Paths.ps1')
if (-not $PythonExe) {
    $usageCandidates=@((Join-Path $env:LOCALAPPDATA 'Programs/Python/Python312/python.exe'),(Join-Path $usageProjectRoot '.local/venv/Scripts/python.exe'))
    $PythonExe=$usageCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
}
if (-not $PythonExe) { throw '请用 -PythonExe 指定当前用户可运行的 Python 3.12 或更新版本。' }
$PythonExe=[IO.Path]::GetFullPath($PythonExe)
& $PythonExe -c 'import sys;sys.exit(0 if sys.version_info >= (3,12) else 1)'
if ($LASTEXITCODE -ne 0) { throw '需要当前用户可运行的 Python 3.12 或更新版本。' }
if ($PSBoundParameters.ContainsKey('QuotaProxy') -and $QuotaProxy) {
    $usageProxyUri=$null
    if (-not [Uri]::TryCreate($QuotaProxy,[UriKind]::Absolute,[ref]$usageProxyUri) -or
        $usageProxyUri.Scheme -notin @('http','https') -or -not $usageProxyUri.Host -or
        $usageProxyUri.UserInfo -or $usageProxyUri.AbsolutePath -notin @('','/') -or
        $usageProxyUri.Query -or $usageProxyUri.Fragment -or $QuotaProxy -match '\s') {
        throw '额度代理需要不含用户名、密码或路径的 HTTP/HTTPS 代理地址。'
    }
}
Push-Location -LiteralPath $usageProjectRoot
try {
    if ($CredentialImport) { & $PythonExe -m client.install_user --import-credentials $CredentialImport }
    else { & $PythonExe -m client.install_user }
    if ($LASTEXITCODE -ne 0) { throw '当前用户配置失败，未设置启动入口。' }
    $usagePythonWindow=Join-Path ([IO.Path]::GetDirectoryName($PythonExe)) 'pythonw.exe'
    if (-not (Test-Path -LiteralPath $usagePythonWindow)) { throw '同目录下未找到 pythonw.exe。' }
    [IO.File]::WriteAllText((Join-Path $usageState 'runtime.json'),(@{pythonw=$usagePythonWindow}|ConvertTo-Json),[Text.UTF8Encoding]::new($false))
    if ($PSBoundParameters.ContainsKey('QuotaProxy')) {
        $usageNetworkPath=Join-Path $usageState 'quota-network.json'
        if ($QuotaProxy) {
            [IO.File]::WriteAllText($usageNetworkPath,(@{proxy=$QuotaProxy}|ConvertTo-Json),[Text.UTF8Encoding]::new($false))
        } elseif (Test-Path -LiteralPath $usageNetworkPath) {
            Remove-Item -LiteralPath $usageNetworkPath
        }
    }
    if ($CodexHome) {
        $usageHomes=@($CodexHome | ForEach-Object { [IO.Path]::GetFullPath($_) })
        [IO.File]::WriteAllText((Join-Path $usageState 'sources.json'),(ConvertTo-Json -InputObject $usageHomes),[Text.UTF8Encoding]::new($false))
    }
    & (Join-Path $PSScriptRoot 'Install-Startup.ps1')
    # Legacy backups can contain local titles. Keep them private even if program files are shared.
    $usageCurrentSid=[Security.Principal.WindowsIdentity]::GetCurrent().User
    $usageLegacyPaths=@((Join-Path $usageProjectRoot '.local/client.credentials'))
    foreach ($usageName in @('collector.sqlite3','collector.sqlite3-wal','collector.sqlite3-shm','status.json','quota-cache.json','control.json','window.json')) {
        $usageLegacyPaths+=Join-Path $usageProjectRoot ('.local/client/'+$usageName)
    }
    foreach ($usageLegacyFile in $usageLegacyPaths) {
        if (-not (Test-Path -LiteralPath $usageLegacyFile)) { continue }
        $usageOwner=(Get-Acl -LiteralPath $usageLegacyFile).GetOwner([Security.Principal.SecurityIdentifier])
        if ($usageOwner.Value -ne $usageCurrentSid.Value) { continue }
        # Change only the DACL; Set-Acl can attempt SACL writes requiring an elevated privilege.
        & (Join-Path $env:WINDIR 'System32/icacls.exe') $usageLegacyFile '/inheritance:r' '/grant:r' ('*'+$usageCurrentSid.Value+':F') '*S-1-5-18:F' '*S-1-5-32-544:F' | Out-Null
        if ($LASTEXITCODE -ne 0) { throw '无法保护旧备份的访问范围，请检查该文件的权限。' }
        $usageAllowed=@($usageCurrentSid.Value,'S-1-5-18','S-1-5-32-544')
        foreach ($usageRule in (Get-Acl -LiteralPath $usageLegacyFile).GetAccessRules($true,$false,[Security.Principal.SecurityIdentifier])) {
            if ($usageRule.AccessControlType -eq 'Allow' -and $usageRule.IdentityReference.Value -notin $usageAllowed) {
                & (Join-Path $env:WINDIR 'System32/icacls.exe') $usageLegacyFile '/remove:g' ('*'+$usageRule.IdentityReference.Value) | Out-Null
                if ($LASTEXITCODE -ne 0) { throw '无法移除旧备份的共享读取权限。' }
            }
        }
    }
} finally { Pop-Location }
Write-Output '当前 Windows 用户已配置；可使用任意 Codex 账号，无需设置账号使用名单。'

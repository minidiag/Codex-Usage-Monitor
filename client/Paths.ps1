$usageProjectRoot=[IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$usageHasher=[Security.Cryptography.SHA256]::Create()
try { $usageInstallationKey=([BitConverter]::ToString($usageHasher.ComputeHash([Text.Encoding]::UTF8.GetBytes($usageProjectRoot.ToLowerInvariant())))).Replace('-','').Substring(0,16) }
finally { $usageHasher.Dispose() }
$usageUserRoot=Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) ('CodexUsageMonitor/'+$usageInstallationKey)
$usageState=Join-Path $usageUserRoot 'state'
$usageCredentials=Join-Path $usageUserRoot 'client.credentials'

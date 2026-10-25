$ErrorActionPreference='Stop'
$quotaRoot=[IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$quotaFramework=Join-Path $env:WINDIR 'Microsoft.NET/Framework64/v4.0.30319'
$quotaTestRoot=Join-Path $quotaRoot '.local/client-tests'
[void](New-Item -ItemType Directory -Force -Path $quotaTestRoot)
$quotaTestExe=Join-Path $quotaTestRoot 'QuotaViewTests.exe'
& (Join-Path $quotaFramework 'csc.exe') /nologo /target:exe ('/out:'+$quotaTestExe) (Join-Path $PSScriptRoot 'QuotaView.cs') (Join-Path $PSScriptRoot 'tests/QuotaViewTests.cs')
if ($LASTEXITCODE -ne 0) { throw 'Quota view test build failed' }
& $quotaTestExe
if ($LASTEXITCODE -ne 0) { throw 'Quota view tests failed' }

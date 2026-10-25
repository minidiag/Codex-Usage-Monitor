$ErrorActionPreference='Stop'
$usageRoot=[IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$usageFramework=Join-Path $env:WINDIR 'Microsoft.NET/Framework64/v4.0.30319'
$usageTestOutput=Join-Path $usageRoot '.local/client-tests/CycleViewTests.exe'
[void](New-Item -ItemType Directory -Force -Path ([IO.Path]::GetDirectoryName($usageTestOutput)))
& (Join-Path $usageFramework 'csc.exe') /nologo /target:exe ('/reference:'+(Join-Path $usageFramework 'System.Numerics.dll')) ('/out:'+$usageTestOutput) (Join-Path $PSScriptRoot 'CycleView.cs') (Join-Path $PSScriptRoot 'tests/CycleViewTests.cs')
if($LASTEXITCODE -ne 0){throw '周期用量检查编译失败。'}
& $usageTestOutput
if($LASTEXITCODE -ne 0){throw '周期用量检查失败。'}

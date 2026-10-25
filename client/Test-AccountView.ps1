$ErrorActionPreference='Stop'
$usageRoot=[IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$usageFramework=Join-Path $env:WINDIR 'Microsoft.NET/Framework64/v4.0.30319'
$usageTestOutput=Join-Path $usageRoot '.local/client-tests/AccountViewTests.exe'
[void](New-Item -ItemType Directory -Force -Path ([IO.Path]::GetDirectoryName($usageTestOutput)))
$usageArgs=@('/nologo','/target:exe',('/out:'+$usageTestOutput))
foreach($usageReference in @('System.dll','System.Core.dll','System.Xaml.dll','System.Web.Extensions.dll','WPF/PresentationFramework.dll','WPF/PresentationCore.dll','WPF/WindowsBase.dll')) {$usageArgs+=('/reference:'+(Join-Path $usageFramework $usageReference))}
$usageArgs+=(Join-Path $PSScriptRoot 'AccountView.cs')
$usageArgs+=(Join-Path $PSScriptRoot 'tests/AccountViewTests.cs')
& (Join-Path $usageFramework 'csc.exe') $usageArgs
if($LASTEXITCODE -ne 0){throw '账号显示检查编译失败。'}
& $usageTestOutput
if($LASTEXITCODE -ne 0){throw '账号显示检查失败。'}

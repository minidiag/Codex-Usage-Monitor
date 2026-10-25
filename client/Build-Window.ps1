$ErrorActionPreference='Stop'
$usageRoot=[IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$usageFramework=Join-Path $env:WINDIR 'Microsoft.NET/Framework64/v4.0.30319'
$usageCompiler=Join-Path $usageFramework 'csc.exe'
$usageOutput=Join-Path $usageRoot '.local/client/CodexUsageMonitor.exe'
[void](New-Item -ItemType Directory -Force -Path ([IO.Path]::GetDirectoryName($usageOutput)))
$usageReferences=@('System.dll','System.Core.dll','System.Numerics.dll','System.Xaml.dll','System.Web.Extensions.dll','WPF/PresentationFramework.dll','WPF/PresentationCore.dll','WPF/WindowsBase.dll')
$usageCompilerArgs=@('/nologo','/target:winexe','/optimize+',('/out:'+$usageOutput))
foreach ($usageReference in $usageReferences) { $usageCompilerArgs+=('/reference:'+(Join-Path $usageFramework $usageReference)) }
$usageCompilerArgs+=(Join-Path $PSScriptRoot 'FloatingWindow.cs')
$usageCompilerArgs+=(Join-Path $PSScriptRoot 'QuotaView.cs')
$usageCompilerArgs+=(Join-Path $PSScriptRoot 'AccountView.cs')
$usageCompilerArgs+=(Join-Path $PSScriptRoot 'CycleView.cs')
& $usageCompiler $usageCompilerArgs
if ($LASTEXITCODE -ne 0) { throw '悬浮窗编译失败。' }
Write-Output '桌面悬浮窗已构建。'

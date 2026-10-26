$ErrorActionPreference='Stop'
$usageRoot=[IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$usageFramework=Join-Path $env:WINDIR 'Microsoft.NET/Framework64/v4.0.30319'
$usageOutput=Join-Path $usageRoot '.local/client-tests/ProxySettingsTests.exe'
[void](New-Item -ItemType Directory -Force -Path ([IO.Path]::GetDirectoryName($usageOutput)))
$usageArgs=@('/nologo','/target:exe',('/out:'+$usageOutput))
foreach($usageReference in @('System.dll','System.Core.dll','System.Xaml.dll','System.Web.Extensions.dll','WPF/PresentationFramework.dll','WPF/PresentationCore.dll','WPF/WindowsBase.dll')) {
    $usageArgs+=('/reference:'+(Join-Path $usageFramework $usageReference))
}
$usageArgs+=(Join-Path $PSScriptRoot 'ProxySettingsWindow.cs')
$usageArgs+=(Join-Path $PSScriptRoot 'tests/ProxySettingsTests.cs')
& (Join-Path $usageFramework 'csc.exe') $usageArgs
if($LASTEXITCODE -ne 0){throw '代理设置测试编译失败。'}
& $usageOutput
if($LASTEXITCODE -ne 0){throw '代理设置测试失败。'}

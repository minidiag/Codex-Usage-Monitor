param(
    [string]$CredentialPath,
    [string]$Account,
    [string]$WindowsUser,
    [string]$Machine,
    [ValidateSet('account','principal','machine')][string[]]$GroupBy,
    [switch]$ListIdentities,
    [switch]$QuotaCycles,
    [switch]$AsJson
)
$ErrorActionPreference = 'Stop'
if (-not $CredentialPath) {
    . (Join-Path $PSScriptRoot '../client/Paths.ps1')
    $CredentialPath=$usageCredentials
}
if (-not (Test-Path -LiteralPath $CredentialPath)) {
    throw '未找到本机加密凭据，请先完成凭据交付。'
}
$usageSecureConfig = (Get-Content -LiteralPath $CredentialPath -Raw).Trim() | ConvertTo-SecureString
$usageConfigPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($usageSecureConfig)
try {
    $usageConfig = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($usageConfigPointer) | ConvertFrom-Json
} finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($usageConfigPointer)
}
$usageReadToken = ConvertTo-SecureString -String $usageConfig.read_token -AsPlainText -Force
$usageQuery=@{}
if ($Account) { $usageQuery.account=$Account }
if ($WindowsUser) { $usageQuery.principal=$WindowsUser }
if ($Machine) { $usageQuery.machine=$Machine }
if ($GroupBy) { $usageQuery.group_by=$GroupBy -join ',' }
$usageQueryText=($usageQuery.GetEnumerator() | ForEach-Object { [Uri]::EscapeDataString($_.Key)+'='+[Uri]::EscapeDataString($_.Value) }) -join '&'
if($QuotaCycles -and (-not $Account -or $Account -eq 'unassigned' -or $Machine -or $GroupBy)) { throw '周期记录需要 -Account 完整账号键，可选 -WindowsUser；不支持未归属账号、电脑或分组参数。' }
$usageApiPath=if($ListIdentities){'/v2/identities'}elseif($QuotaCycles){'/v2/usage/quota-cycles?'+$usageQueryText}else{'/v2/usage/summary?'+$usageQueryText}
$usageResult = Invoke-RestMethod -Uri ($usageConfig.base_url.TrimEnd('/') + $usageApiPath) `
    -Authentication Bearer -Token $usageReadToken -NoProxy -TimeoutSec 20
if ($AsJson -or $ListIdentities -or $QuotaCycles) {
    $usageResult | ConvertTo-Json -Depth 10
} else {
    $usageLabels = @{rolling_5h='最近5小时';today='今日';week='本周';month='本月'}
    $usageResult.windows | ForEach-Object {
        [pscustomobject]@{周期=$usageLabels[$_.name];累计Token=$_.totals.total;输入=$_.totals.input;输出=$_.totals.output}
    } | Format-Table -AutoSize
    if ($GroupBy) {
        $usageResult.windows | ForEach-Object {
            $usageWindow=$_
            $_.groups | ForEach-Object {
                [pscustomobject]@{周期=$usageLabels[$usageWindow.name];账号=$_.keys.account;Windows用户=$_.keys.principal;电脑=$_.keys.machine;累计Token=$_.totals.total}
            }
        } | Format-Table -AutoSize
    }
    $usageQualities = @{unknown='未知';partial='部分覆盖';complete='完整覆盖（已登记且支持的来源）'}
    Write-Output ('覆盖状态：' + $usageQualities[$usageResult.quality])
    if ($usageResult.as_of -is [DateTime]) {
        $usageDate = $usageResult.as_of
        if ($usageDate.Kind -eq [DateTimeKind]::Unspecified) {
            $usageDate = [DateTime]::SpecifyKind($usageDate, [DateTimeKind]::Utc)
        }
        $usageAsOf = [DateTimeOffset]::new($usageDate)
    } else {
        $usageAsOf = [DateTimeOffset]::Parse([string]$usageResult.as_of)
    }
    Write-Output ('统计时间：' + $usageAsOf.ToOffset([TimeSpan]::FromHours(8)).ToString('yyyy-MM-dd HH:mm:ss zzz'))
    if ($usageResult.quality_reasons.Count) {
        Write-Output ('状态原因：' + ($usageResult.quality_reasons -join ', '))
    }
}

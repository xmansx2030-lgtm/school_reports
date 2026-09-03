$ErrorActionPreference = 'Continue'
$outputDirectory = Join-Path $env:LOCALAPPDATA 'Temp\CodexDeviceRepair\PostReboot'
New-Item -ItemType Directory -Path $outputDirectory -Force | Out-Null
$progress = Join-Path $outputDirectory 'progress.log'
Set-Content -LiteralPath $progress -Value "$(Get-Date -Format o) STARTING" -Encoding utf8

function Mark([string]$message) {
    Add-Content -LiteralPath $progress -Value "$(Get-Date -Format o) $message" -Encoding utf8
}

Mark 'CHECKING_BITLOCKER'
& manage-bde.exe -status C: 2>&1 | Set-Content -LiteralPath (Join-Path $outputDirectory 'bitlocker.txt') -Encoding utf8

Mark 'CHECKING_TPM'
Get-Tpm |
    Select-Object TpmPresent, TpmReady, TpmEnabled, TpmActivated, ManagedAuthLevel, AutoProvisioning |
    ConvertTo-Json |
    Set-Content -LiteralPath (Join-Path $outputDirectory 'tpm.json') -Encoding utf8

Mark 'RUNNING_DISM_CHECKHEALTH'
& dism.exe /Online /Cleanup-Image /CheckHealth 2>&1 |
    Tee-Object -FilePath (Join-Path $outputDirectory 'dism-checkhealth.txt')
$dismExit = $LASTEXITCODE

Mark 'RUNNING_SFC_SCANNOW'
& sfc.exe /scannow 2>&1 |
    Tee-Object -FilePath (Join-Path $outputDirectory 'sfc-scannow.txt')
$sfcExit = $LASTEXITCODE

Mark 'SEARCHING_WINDOWS_UPDATES'
try {
    $session = New-Object -ComObject Microsoft.Update.Session
    $searcher = $session.CreateUpdateSearcher()
    $result = $searcher.Search('IsInstalled=0 and IsHidden=0')
    $titles = for ($index = 0; $index -lt $result.Updates.Count; $index++) {
        $result.Updates.Item($index).Title
    }
    $titles | Set-Content -LiteralPath (Join-Path $outputDirectory 'pending-updates.txt') -Encoding utf8
    $updateCount = $result.Updates.Count
} catch {
    $_ | Out-String | Set-Content -LiteralPath (Join-Path $outputDirectory 'pending-updates-error.txt') -Encoding utf8
    $updateCount = $null
}

[ordered]@{
    CompletedAt = (Get-Date).ToString('o')
    DismCheckHealthExitCode = $dismExit
    SfcScanNowExitCode = $sfcExit
    PendingWindowsUpdateCount = $updateCount
} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $outputDirectory 'summary.json') -Encoding utf8
Mark 'COMPLETED'

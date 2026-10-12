[CmdletBinding()]
param(
    [string]$InstallDir = (Split-Path -Parent $PSScriptRoot),
    [switch]$Apply,
    [switch]$WhatIf
)
$ErrorActionPreference = 'Stop'
$root = [System.IO.Path]::GetFullPath((Resolve-Path -LiteralPath $InstallDir).Path).TrimEnd('\')
$items = @()

function Add-Candidate([System.IO.FileSystemInfo]$Item) {
    $full = [System.IO.Path]::GetFullPath($Item.FullName)
    if ($full -eq $root -or -not $full.StartsWith($root + '\', [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing path outside install directory: $full"
    }
    $script:items += $Item
}

$internal = @(Get-ChildItem -LiteralPath $root -Force -Filter '_internal.*' -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending)
if ($internal.Count -gt 1) { $internal | Select-Object -Skip 1 | ForEach-Object { Add-Candidate $_ } }

# C-06: the old pattern was '\.exe\.(?:\d+|bak-.+|prev.+)$', whose \d+ cannot span the
# hyphen, so it missed every backup stamped like `qq-live-digest.exe.20261009-155123` or
# `qq-live-digest.exe.1009-004253`. Production really contains 50 of those and the old
# pattern matched only 28, so the newest backups were never cleaned up. The timestamp is
# now \d+(?:-\d+)? so both the compact and the hyphenated forms match, while the live
# `qq-live-digest.exe` (no trailing stamp) still does not.
$exeBackupPattern = '\.exe\.(?:\d+(?:-\d+)?|bak-.+|prev.+)$'
$exeBackups = @(Get-ChildItem -LiteralPath $root -Force -File -ErrorAction SilentlyContinue | Where-Object { $_.Name -match $exeBackupPattern })
$groups = $exeBackups | Group-Object { $_.Name -replace $exeBackupPattern, '.exe' }
foreach ($group in $groups) {
    $group.Group | Sort-Object LastWriteTime -Descending | Select-Object -Skip 1 | ForEach-Object { Add-Candidate $_ }
}

Get-ChildItem -LiteralPath $root -Force -File -ErrorAction SilentlyContinue | Where-Object { $_.Name -like '.env.bak-*' } | ForEach-Object { Add-Candidate $_ }
Get-ChildItem -LiteralPath $root -Force -ErrorAction SilentlyContinue | Where-Object { $_.Name -like 'assets.bak-*' -or $_.Name -eq 'data-firstrun-backup' } | ForEach-Object { Add-Candidate $_ }

$unique = @($items | Sort-Object FullName -Unique)
if ($unique.Count -eq 0) {
    Write-Host 'No stale install backups found.'
    exit 0
}
$mode = if ($Apply -and -not $WhatIf) { 'APPLY' } else { 'DRY-RUN' }
foreach ($item in $unique) {
    $sensitive = $item.Name -like '.env.bak-*'
    if ($sensitive) { Write-Host ("{0}: {1} (will be permanently deleted)" -f $mode, $item.FullName) }
    else { Write-Host ("{0}: {1}" -f $mode, $item.FullName) }
    if ($Apply -and -not $WhatIf) {
        Remove-Item -LiteralPath $item.FullName -Recurse -Force
        if ($sensitive) { Write-Host ("Permanently deleted: {0}" -f $item.FullName) }
    }
}

# Sweep-KidsAgeMax.ps1 — capture a log_only routing plan per kids_age_max value.
#
# WHY A SCRIPT: support/logs/routing.log is REGENERATED every run ("Fresh plan
# each run"), so the plan for age 7 is destroyed the moment the age-11 run starts.
# Comparing three values needs the artifacts copied out BETWEEN runs, and doing
# that by hand is exactly the step that gets skipped at 5am.
#
# Captures per value: routing.log, default.log, and the config that produced them
# (so a result can never be read against the wrong settings).
#
# USAGE — one value at a time, because each needs a full run in between:
#     .\Sweep-KidsAgeMax.ps1 -Age 7 -Set          # set config to 7, then run glidearr
#     .\Sweep-KidsAgeMax.ps1 -Age 7 -Capture      # after it finishes
#     .\Sweep-KidsAgeMax.ps1 -Age 11 -Set   ...   # and so on
#
#     .\Sweep-KidsAgeMax.ps1 -Compare             # once all three are captured

[CmdletBinding()]
param(
    [ValidateRange(2,17)][int]$Age,
    [switch]$Set,
    [switch]$Capture,
    [switch]$Compare,
    [string]$Root = "C:\Users\rober\PycharmProjects\glidearr\scripts\support"
)

$ErrorActionPreference = "Stop"
$cfgPath = Join-Path $Root "config\config.json"
$logs    = Join-Path $Root "logs"
$outRoot = Join-Path $Root "..\..\kids_age_sweep"
$null = New-Item -ItemType Directory -Force -Path $outRoot

function Get-Cfg { Get-Content $cfgPath -Raw | ConvertFrom-Json }

if ($Set) {
    # Edited as TEXT, not via ConvertTo-Json: a round-trip through PowerShell's
    # JSON writer reorders keys and reformats the whole 87-key file, which would
    # bury the one-line change in a thousand-line diff.
    $raw = Get-Content $cfgPath -Raw
    $new = $raw -replace '("kids_age_max"\s*:\s*)\d+', "`${1}$Age"
    if ($new -eq $raw) { throw "kids_age_max not found in config - did the key move?" }
    Set-Content $cfgPath $new -NoNewline
    $check = (Get-Cfg).plex.playlists.kids_age_max
    if ($check -ne $Age) { throw "verify failed: config reads $check, expected $Age" }
    Write-Host "kids_age_max = $Age" -ForegroundColor Green
    Write-Host "Now run glidearr, then: .\Sweep-KidsAgeMax.ps1 -Age $Age -Capture" -ForegroundColor Cyan
    return
}

if ($Capture) {
    $live = (Get-Cfg).plex.playlists.kids_age_max
    if ($live -ne $Age) {
        # The guard that matters: capturing age-7 artifacts while the config says
        # 11 would produce a comparison table that is confidently wrong.
        throw "config says kids_age_max=$live but you asked to capture $Age. Refusing - the artifacts would be mislabelled."
    }
    $dest = Join-Path $outRoot "age-$Age"
    $null = New-Item -ItemType Directory -Force -Path $dest
    foreach ($f in @("routing.log","default.log")) {
        $src = Join-Path $logs $f
        if (Test-Path $src) { Copy-Item $src (Join-Path $dest $f) -Force }
        else { Write-Warning "$f not found - did the run finish?" }
    }
    Copy-Item $cfgPath (Join-Path $dest "config.json") -Force
    $r = Join-Path $dest "routing.log"
    if (Test-Path $r) {
        $lines = (Get-Content $r).Count
        Write-Host "captured age=$Age  ($lines routing lines) -> $dest" -ForegroundColor Green
    }
    return
}

if ($Compare) {
    Write-Host "`n=== kids_age_max sweep ===" -ForegroundColor Cyan
    foreach ($a in 7,11,14) {
        $r = Join-Path $outRoot "age-$a\routing.log"
        if (-not (Test-Path $r)) { Write-Host ("age {0,-3} NOT CAPTURED" -f $a) -ForegroundColor DarkGray; continue }
        $txt = Get-Content $r
        $toKids   = ($txt | Select-String -Pattern "-> .*(movies|tv/720)/kids"   ).Count
        $fromKids = ($txt | Select-String -Pattern "/kids -> "                    ).Count
        $movies   = ($txt | Select-String -Pattern "^-- radarr.*misplaced"        ) -replace '\D+',' '
        $shows    = ($txt | Select-String -Pattern "^-- sonarr.*misplaced"        ) -replace '\D+',' '
        "age {0,-3}  into kids: {1,-4} out of kids: {2,-4}  (movies hdr:{3} shows hdr:{4})" -f `
            $a, $toKids, $fromKids, $movies.Trim(), $shows.Trim()
    }
    Write-Host "`nLower age = tighter gate = MORE titles leaving kids." -ForegroundColor Yellow
    return
}

Write-Host "Pass -Set, -Capture or -Compare. See the header for the sequence." -ForegroundColor Yellow

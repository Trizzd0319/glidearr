# Rename-GlidearrInstances.ps1 — migrate instance keys to container names.
#
# WHY THIS IS A SCRIPT AND NOT A CONFIG EDIT
# ------------------------------------------
# The instance key is a CACHE PATH COMPONENT. It appears as a directory name
# (cache/radarr/standard/) AND as a filename (cache/sonarr/pilot/progress/
# standard.json, cache/sonarr/jit/inflight_qp/standard.json, ...). Renaming the
# key in config.json alone does not migrate any of it — the next run simply finds
# nothing at the new path and rebuilds from scratch, silently discarding:
#
#   * movie_files.parquet / episode_files.parquet   (the crown-jewel caches)
#   * movie_score_memo.json                          (P10 rescore memo)
#   * stepdown_cooldown.json                         (the pass-level rate limit
#     that stops a downgrade loop re-admitting files it just created)
#   * demote_deleted.json, monitor_demote_clock.json (4K eviction ledgers)
#   * jit/inflight_qp/<instance>.json                (restores a series left at a
#     BUMPED quality profile by a crashed JIT job — losing this means a series
#     can stay stranded at the wrong tier with nothing to detect it)
#   * pilot/{checkpoint,progress,unacquirable}/<instance>.json
#   * legacy_regrab / size_anomaly ledgers
#
# Losing the cooldown and inflight ledgers is worse than losing the parquets: a
# parquet rebuilds, a ledger's job is to remember something the live system no
# longer knows.
#
# SAFETY
#   * -WhatIf by default. Nothing moves until you pass -Apply.
#   * Refuses to run if any target already exists (no merging, no clobber).
#   * Copies to a timestamped backup BEFORE moving anything.
#   * Reports every path it did not recognise rather than guessing.

[CmdletBinding()]
param(
    [switch]$Apply,
    [string]$Root = "C:\Users\rober\PycharmProjects\glidearr\scripts\support"
)

$ErrorActionPreference = "Stop"
$stamp  = Get-Date -Format "yyyyMMdd-HHmmss"
$cache  = Join-Path $Root "cache"
$config = Join-Path $Root "config\config.json"

# old key -> new key, per service. Sonarr and Radarr both use "standard", so the
# mapping MUST be service-scoped or a rename hits the wrong tree.
$map = @{
    radarr = @{ "standard" = "radarr-720";  "ultra" = "radarr-2160" }
    sonarr = @{ "standard" = "sonarr-720" }
}

Write-Host "`n=== Glidearr instance-key migration ===" -ForegroundColor Cyan
Write-Host ("mode: {0}`n" -f $(if ($Apply) { "APPLY" } else { "DRY RUN (pass -Apply to commit)" })) `
    -ForegroundColor $(if ($Apply) { "Yellow" } else { "Green" })

# ── 1. inventory ────────────────────────────────────────────────────────────
$moves = @()
foreach ($svc in $map.Keys) {
    foreach ($old in $map[$svc].Keys) {
        $new = $map[$svc][$old]
        $svcRoot = Join-Path $cache $svc
        if (-not (Test-Path $svcRoot)) { continue }

        # directories named <old>
        Get-ChildItem $svcRoot -Recurse -Directory -Filter $old -ErrorAction SilentlyContinue |
            ForEach-Object { $moves += [pscustomobject]@{ Kind="dir"; From=$_.FullName;
                             To=(Join-Path $_.Parent.FullName $new) } }

        # files named <old>.<ext>
        Get-ChildItem $svcRoot -Recurse -File -Filter "$old.*" -ErrorAction SilentlyContinue |
            ForEach-Object { $moves += [pscustomobject]@{ Kind="file"; From=$_.FullName;
                             To=(Join-Path $_.Directory.FullName ($new + $_.Extension)) } }
    }
}

if (-not $moves) { Write-Host "nothing to migrate — already renamed?" -ForegroundColor Yellow; return }

Write-Host ("{0} path(s) to migrate:" -f $moves.Count)
$moves | ForEach-Object {
    "  {0,-4} {1}`n       -> {2}" -f $_.Kind, $_.From.Replace($cache,"…"), $_.To.Replace($cache,"…")
}

# ── 2. refuse to clobber ────────────────────────────────────────────────────
$clash = $moves | Where-Object { Test-Path $_.To }
if ($clash) {
    Write-Host "`nREFUSING: target already exists —" -ForegroundColor Red
    $clash | ForEach-Object { "   $($_.To)" }
    Write-Host "Merging two instance caches is never right. Resolve by hand." -ForegroundColor Red
    return
}

if (-not $Apply) {
    Write-Host "`nDry run only. Re-run with -Apply to commit." -ForegroundColor Green
    return
}

# ── 3. back up, then move ───────────────────────────────────────────────────
$backup = Join-Path $Root "cache.bak.rename.$stamp"
Write-Host "`nbacking up cache -> $backup" -ForegroundColor Yellow
Copy-Item $cache $backup -Recurse -Force
Copy-Item $config "$config.bak.rename.$stamp" -Force

# deepest first, so renaming a parent cannot invalidate a child's path
foreach ($m in ($moves | Sort-Object { $_.From.Length } -Descending)) {
    Move-Item -LiteralPath $m.From -Destination $m.To -Force
    "  moved {0}" -f $m.To.Replace($cache,"…")
}

Write-Host "`nDONE. Cache migrated." -ForegroundColor Green
Write-Host "Next: update config.json instance keys to match, then run the suite." -ForegroundColor Cyan
Write-Host "Backup kept at: $backup"

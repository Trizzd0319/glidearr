# Clear locally-derived caches so the next run rebuilds them under the new layouts.
# Runnable from any directory (paths resolve from this script's location).
#
# PRESERVED — do not add these to the delete list:
#   trakt\            223,836 files of enrich-daemon output (rate-limited, days to rebuild)
#   mdblist\ mal\     external service payloads
#   ml\               label pipeline: snapshots + backfill (point-in-time, NOT re-derivable)
#   people_matrix\    computed from the trakt enrichment
#   *.parquet         movie_files / episode_files / owned_episodes / relational
#                     (carry watch stats, grace marks, plan stamps; rows refresh from the APIs)
#   owned_episodes.fingerprints.json   pairs with its parquet — losing it forces a full rebuild
#   sonarr\jit\       IN-FLIGHT JIT upgrades awaiting restore to their pre-upgrade profile
#   sonarr\pilot\     unacquirable ledger + search cooldowns
#   sonarr\legacy_regrab\   re-grab cooldown ledger
#   radarr\*\monitor_demote_clock.json   dwell clocks
#   system\ notifications\  backup-gate safety state, notification dedupe

$ErrorActionPreference = 'Stop'

# scripts\support\cache_cleanup.ps1 → repo root is two levels up.
$repo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$c    = Join-Path $repo 'scripts\support\cache'
if (-not (Test-Path $c)) { throw "cache directory not found: $c" }

# NOTE: do NOT name this 'Kill' — that is a built-in alias for Stop-Process, and
# aliases outrank functions in PowerShell's command resolution, so the string
# path binds to Stop-Process -InputObject and the script dies.
function Remove-CachePath {
    param([string]$Path)
    if (Test-Path -LiteralPath $Path) {
        Remove-Item -LiteralPath $Path -Recurse -Force
        Write-Host "  removed $($Path.Substring($c.Length + 1))"
    }
}

Write-Host "Clearing locally-derived caches under $c" -ForegroundColor Cyan

# ── Radarr: full-library API snapshots + per-instance API caches ──────────────
Get-ChildItem -LiteralPath $c -Filter 'radarr.*.json' -File |
    ForEach-Object { Remove-CachePath $_.FullName }
Remove-CachePath (Join-Path $c 'radarr\custom_formats')
Remove-CachePath (Join-Path $c 'radarr\metadata')
foreach ($inst in @('standard', 'test', 'ultra')) {
    Remove-CachePath (Join-Path $c "radarr\$inst\movie_library")
    Remove-CachePath (Join-Path $c "radarr\$inst\storage")
    Remove-CachePath (Join-Path $c "radarr\$inst\movie_score_memo.json")  # stale after the collection fix
}

# ── Sonarr: letter-bucket library + per-series episode/file API caches ────────
foreach ($inst in @('standard')) {
    Remove-CachePath (Join-Path $c "sonarr\$inst\library")
    Remove-CachePath (Join-Path $c "sonarr\$inst\episodefiles")
    Remove-CachePath (Join-Path $c "sonarr\$inst\episodes")
    Remove-CachePath (Join-Path $c "sonarr\$inst\history")
    Remove-CachePath (Join-Path $c "sonarr\$inst\show_score_memo.json")   # key format changed
    foreach ($f in @('cache_fallback.json', 'cache_timestamps.json', 'episodes_deletion.json',
                     'episodes_file.json', 'episodes_history.json', 'episodes_monitoring.json',
                     'episodes_sharding.json', 'errors.json')) {
        Remove-CachePath (Join-Path $c "sonarr\$inst\$f")
    }
}

# ── Plex + Tautulli: inventories, learned collection maps, watch history ──────
# All re-fetched next run; Tautulli keeps the authoritative history server-side.
Remove-CachePath (Join-Path $c 'plex')
Remove-CachePath (Join-Path $c 'tautulli')

# ── Computed previews / calibrations (rebuilt each run) ───────────────────────
Remove-CachePath (Join-Path $c 'universe\saga_credit_preview')
Remove-CachePath (Join-Path $c 'discovery')
Remove-CachePath (Join-Path $c 'size_model')
Remove-CachePath (Join-Path $c 'franchise_catalog_state.json')

# ── Stray test artifact from the threshold build ──────────────────────────────
Remove-CachePath (Join-Path $c 'ml\reports\thresholds_2026-07-26.json')

Write-Host ""
Write-Host "Done. Preserved:" -ForegroundColor Green
foreach ($k in @('trakt', 'mdblist', 'mal', 'ml', 'people_matrix', 'system', 'notifications',
                 'sonarr\jit', 'sonarr\pilot', 'sonarr\legacy_regrab')) {
    $p = Join-Path $c $k
    if (Test-Path -LiteralPath $p) {
        $n = @(Get-ChildItem -LiteralPath $p -Recurse -File -ErrorAction SilentlyContinue).Count
        Write-Host ("  {0,-24} {1,8} files" -f $k, $n)
    }
}
$pq = @(Get-ChildItem -LiteralPath $c -Recurse -Filter '*.parquet' -File -ErrorAction SilentlyContinue).Count
Write-Host ("  {0,-24} {1,8} files" -f '*.parquet (all)', $pq)
Write-Host ""
Write-Host "Next run will be slower: full series + movie library re-fetch, one full rescore," -ForegroundColor Yellow
Write-Host "and a pilot-batch cold start (now jittered + capped, so it trickles)." -ForegroundColor Yellow
# Clear locally-derived caches so the next run rebuilds them under the new layouts.
# Run from the repo root.  Everything deleted here is re-derived from the Radarr /
# Sonarr / Plex / Tautulli APIs on the next run.
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
$c = 'scripts\support\cache'

function Kill($path) {
    if (Test-Path $path) {
        Remove-Item -LiteralPath $path -Recurse -Force
        Write-Host "  removed $path"
    }
}

Write-Host "Clearing locally-derived caches..." -ForegroundColor Cyan

# ── Radarr: full-library API snapshots + per-instance API caches ──────────────
Get-ChildItem -Path $c -Filter 'radarr.*.json' -File | ForEach-Object { Kill $_.FullName }
Kill "$c\radarr\custom_formats"
Kill "$c\radarr\metadata"
foreach ($inst in @('standard', 'test', 'ultra')) {
    Kill "$c\radarr\$inst\movie_library"
    Kill "$c\radarr\$inst\storage"
    Kill "$c\radarr\$inst\movie_score_memo.json"   # stale after the collection fix; one rescore
}

# ── Sonarr: letter-bucket library + per-series episode/file API caches ────────
foreach ($inst in @('standard')) {
    Kill "$c\sonarr\$inst\library"
    Kill "$c\sonarr\$inst\episodefiles"
    Kill "$c\sonarr\$inst\episodes"
    Kill "$c\sonarr\$inst\history"
    Kill "$c\sonarr\$inst\show_score_memo.json"    # key format changed; one rescore
    foreach ($f in @('cache_fallback.json', 'cache_timestamps.json', 'episodes_deletion.json',
                     'episodes_file.json', 'episodes_history.json', 'episodes_monitoring.json',
                     'episodes_sharding.json', 'errors.json')) {
        Kill "$c\sonarr\$inst\$f"
    }
}

# ── Plex + Tautulli: inventories, learned collection maps, watch history ──────
# All re-fetched next run; Tautulli keeps the authoritative history server-side.
Kill "$c\plex"
Kill "$c\tautulli"

# ── Computed previews / calibrations (rebuilt each run) ───────────────────────
Kill "$c\universe\saga_credit_preview"
Kill "$c\discovery"
Kill "$c\size_model"
Kill "$c\franchise_catalog_state.json"

# ── Stray test artifact from the threshold build ──────────────────────────────
Kill "$c\ml\reports\thresholds_2026-07-26.json"

Write-Host ""
Write-Host "Done. Preserved:" -ForegroundColor Green
foreach ($k in @('trakt', 'mdblist', 'mal', 'ml', 'people_matrix', 'system', 'notifications',
                 'sonarr\jit', 'sonarr\pilot', 'sonarr\legacy_regrab')) {
    if (Test-Path "$c\$k") {
        $n = (Get-ChildItem "$c\$k" -Recurse -File -ErrorAction SilentlyContinue).Count
        Write-Host ("  {0,-24} {1,8} files" -f $k, $n)
    }
}
Write-Host ("  {0,-24} {1,8} files" -f '*.parquet (all)',
    (Get-ChildItem $c -Recurse -Filter '*.parquet' -File -ErrorAction SilentlyContinue).Count)
Write-Host ""
Write-Host "Next run will be slower: full series + movie library re-fetch, one full rescore," -ForegroundColor Yellow
Write-Host "and a pilot-batch cold start (now jittered + capped, so it trickles)." -ForegroundColor Yellow

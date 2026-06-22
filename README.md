<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/glidearr_wordmark_dark.svg">
  <img alt="Glidearr" src="assets/glidearr_wordmark.svg" width="420">
</picture>

### Glide forward. The view's always ahead.

**A self-hosted companion for Plex + Sonarr/Radarr that keeps your media library gliding forward with you.**

<!-- TODO: wire these up once the repo + CI exist -->
![License](https://img.shields.io/badge/license-AGPL--3.0-blue)
![Status](https://img.shields.io/badge/status-approaching%20first%20release-yellow)
![Docker](https://img.shields.io/badge/docker-coming%20soon-2496ED)
![Built for](https://img.shields.io/badge/built%20for-Plex%20%2B%20*arr%20%2B%20Tautulli-e5a00d)

</div>

---

> **Glidearr** treats your media library like a glider riding the wind: it moves slowly forward, the world opening up ahead a little at a time, while the rearview gently lets go. As you watch, Glidearr pulls in the next thing on your horizon and releases what's already behind you — a rolling window that follows each viewer through shows, franchises, and whole universes. Reclaimed disk space isn't the goal; it's just what's left in the wake.

**Contents:** [What is Glidearr?](#what-is-glidearr) · [How it works](#how-it-works--the-rolling-window) · [Features](#features) · [Safe by default](#safe-by-default) · [Integrations](#integrations) · [Getting started](#getting-started) · [Security](#security) · [Roadmap](#roadmap) · [Contributing](#contributing) · [License](#license)

## What is Glidearr?

Most library tools treat your collection as a pile to *hoard* or a mess to *clean up*. Glidearr treats it as a **rolling window that moves with you**.

It watches where each viewer is in a show, a franchise, or a shared universe, and keeps the library centered on that position — bringing the **next** thing into reach and quietly letting go of what's already been seen. The result is a library that always shows the road ahead instead of the one behind, and stays lean without you ever thinking about disk space.

Where rule-based cleanup tools wait for media to go stale and then prune it, Glidearr is **forward-looking**: it's always setting up your next watch, and shedding the past is simply the trailing edge of that motion.

## How it works — the rolling window

Glidearr's window has two edges:

- **🧭 The Horizon** *(leading edge)* — acquires what's next: the next episode, the next film in the saga, the next step in a viewer's progression — at the right quality, just before it's needed, instead of grabbing everything up front.
- **🪞 The Rearview** *(trailing edge)* — releases what's behind: already-watched or no-longer-relevant titles drift out of the window, **guarded and restorable**, so nothing important is ever lost.

As you watch, the whole window glides forward. Freed space is simply the wake it leaves behind.

> **↩️ Resumption** *(planned)* — the window doesn't only move forward. When you're about to pick a series back up — or the world drops something new — Glidearr **circles back**, pulling the episodes (or the earlier film) you'll need back in and re-queueing them so you're caught up before you sit down, with priority ramping as the moment approaches:
>
> - **You come back to a show** — add a series (or just its pilot) to your watchlist, and Glidearr re-acquires the last *N* episodes you'd watched so you can pick up exactly where you left off.
> - **A new season drops** *(House of the Dragon, Silo, …)* — the previous *N* episodes are re-acquired and slotted back into your **Up Next** playlist ahead of the premiere — in order, ready to binge — with priority rising as the air date nears.
> - **A sequel from cast or crew you love** *(Spider-Man: Brand New Day, …)* — Glidearr re-acquires the previous film in the best available quality and queues it up, ramping priority toward release day.
> - **No calendar-watching required** — Glidearr compares what's coming soon against your flight-path history and lines up the catch-up automatically, so you're ready without ever tracking a release date.


## Features

- **🪟 Per-viewer rolling window** — every viewer gets their own horizon; the library follows each person's progress, not a single global state.
- **📺 Personalized "Up Next"** — a spoiler-safe, taste-ranked playlist for each viewer, written back to that member's own Plex account (opt-in, default-off).
- **🧠 Taste-aware** — scores every title for *watchability* (your history, recency, ratings) so the window keeps what you'll actually watch next.
- **🎬 Franchise & universe aware** — follows you in order through series, franchises, and shared universes (think the MCU or Star Trek in sequence).
- **⏭️ Just-in-time acquisition** — fetches the next item as you approach it, at a sensible quality, instead of stockpiling.
- **♻️ Guarded, restorable pruning** — releases watched/low-value media only as needed, with protections so it never removes the wrong thing — and every removal can be undone.
- **💾 Space as a side-effect** — set the free-space headroom you want; Glidearr keeps you above it automatically.
- **📦 Right-sized quality** — avoids oversized files and keeps quality sane across the library.
- **🗂️ Library auto-organization** — sorts TV and movies into anime / kids / docs / 4K / standard for new adds *and* reconciles titles already filed in the wrong place (dubbed anime stays anime even when the metadata claims English).
- **🔌 Fits your stack** — works with Plex, Sonarr/Radarr (multi-instance), Tautulli, Trakt, MyAnimeList, MDBList and more; every integration is optional and degrades gracefully.
- **🛩️ Your pilot is always aboard** — pilot episodes stay in Plex at the best available quality, so you can jump back into any show at any time.
- **🗓️ Runs on your schedule** — hourly, every few hours, or whatever cadence you set, so your library stays constantly fresh.

## Safe by default

Glidearr removes media — so it's built to never surprise you:

- **Dry-run first.** Preview every decision before a single file is touched.
- **Off until you opt in.** Nothing is ever deleted unless you explicitly set a free-space target.
- **Backed up first.** Before any destructive change, Glidearr triggers and verifies a native Sonarr/Radarr backup — and if one can't be created, it automatically degrades to dry-run instead of touching your data.
- **Deeply guarded.** Pilots, franchise entries, recently-watched, soon-to-air, keep-tagged, and multi-episode files are never removed.
- **Always reversible.** Every removal is ledgered and can be re-acquired if its value recovers.

## Integrations

Glidearr slots into the stack you already run. **None of these are hard requirements** — connect what you have and Glidearr uses it; leave one out and it degrades gracefully and keeps working adequately. The more you connect, the smarter the window.

- **Plex** — your media server: what's been watched, and by whom.
- **Sonarr** & **Radarr** — TV and movie library management + acquisition. **Multiple instances of each are supported** (e.g. separate Radarr instances for standard and 4K/ultra libraries).
- **Tautulli** — playback history and watch statistics.
- **Trakt**, **MyAnimeList**, and **MDBList** — taste signals, ratings, and list/availability data.
- **TheTVDB** and **Common Sense Media** — episode metadata and content/age ratings.
- **Discord** *(optional)* — end-of-run report card and run notifications.

> Your API keys never get committed to git — see **[Security](#security)** for how Glidearr stores them.

## Getting started

> ⚠️ **Approaching first public release.** Setup instructions and published images are on the way. The snippet below is a placeholder to adapt.

**You'll need:** a running **Plex** server and **Sonarr** and/or **Radarr** — Tautulli, Trakt, MyAnimeList, and MDBList are all optional. Glidearr runs as a **Docker** container (recommended) or directly with **Python**.

```yaml
# docker-compose.yml (placeholder — TODO: finalize image, env, and volumes)
services:
  glidearr:
    image: ghcr.io/youruser/glidearr:latest   # TODO
    container_name: glidearr
    environment:
      - TZ=Etc/UTC                            # TODO
    volumes:
      - ./config:/config                      # TODO
    restart: unless-stopped
```

Full installation and configuration docs: _coming soon._

## Configuration

Glidearr connects to your existing Plex + *arr services and reads your viewing activity to drive the window. A **guided onboarding** walks you through connecting each service and writes a complete config for you — and it can generate that config non-interactively, so a Docker container can come up pre-configured. Detailed configuration (service URLs/keys, headroom targets, protection rules) will be documented as the project firms up. <!-- TODO -->

## Security

**Your credentials never get committed to git.** How Glidearr stores your API keys and tokens depends on how you run it:

- **In Docker (the default):** secrets live in your **config file** on the mounted `/config` volume — git-ignored and written owner-only (`0600`). For stricter setups, inject them as `GLIDEARR_*` environment variables (Docker secrets, Kubernetes, CI) and leave the file blank — the environment value always wins.
- **On bare-metal, if you'd rather not keep keys in a file at all:** onboarding offers to store them in your **OS keystore** for whichever platform you're on — **Windows Credential Manager**, **macOS Keychain**, or the **Linux Secret Service** — encrypted at rest and never written to a readable file. Glidearr detects whether it's running inside a container and only offers this when it isn't, so the choice is yours when leakage to a file (or anywhere else) is a concern.

Either way, resolved secret values are **scrubbed from all logs**, so a debug log you share will never leak them.

## Roadmap

A snapshot after ~6 months of development. Glidearr is already a working engine — here's what's in place, what's landing next, and where it's headed.

### ✅ Available now

**The rolling window**
- **Just-in-time acquisition** — prefetches your next unwatched episodes at the right quality, budgeted by runtime, before you reach them.
- **Pilot always aboard** — every show's pilot stays in Plex at the best available quality (and is never deleted), so you can sample or resume any series anytime.
- **Per-episode quality targeting** — each episode earns its own quality tier by watch-likelihood, so the window never over-fetches.
- **Guarded, restorable reclamation** — when disk gets tight, the least-valuable *watched* media is released first (it tries quality downgrades before deleting), with deep protections (keep tags, franchises, pilots, recently-watched, soon-to-air, multi-episode files) and full restore.
- **Pre-flight service backups** — before any destructive change, a native Sonarr/Radarr backup is triggered and validated; if it can't be verified, the run degrades to dry-run. Recent backups are reused, so it doesn't dump a new archive every run.
- **Hard safety floor** — nothing is ever deleted unless you explicitly set a free-space target.

**Taste & curation**
- **Watchability scoring** — a 0–100 score per title from your household's habits, cast/crew/genre affinity, collection completeness, device fit, ratings, and recency — driving every keep / grab / delete / upgrade decision.
- **Recommendation acquisition** — pulls from your Trakt watchlist + recommendations and MyAnimeList plan-to-watch / seasonal charts, scores candidates, and adds them at the right library and quality (opt-in, with thresholds and per-run caps).
- **Related-title affinity** — "if you liked X, you'll like Y," via watched-neighbour graphs.

**Library & quality**
- **Automatic library routing** — classifies and sorts TV and movies into anime / kids / docs / reality / 4K / standard, for both new adds and existing libraries.
- **Kids-safe routing & age-gating** — soft certificate gating with a Common Sense Media fallback for uncertified titles.
- **English-audio prioritization** — across all quality profiles, with dedicated English-locked upgrade ladders for foreign films.
- **Quality right-sizing** — a calibrated, codec-aware size model so files are never over- or under-sized.
- **Size-anomaly remediation** — flags files far outside their expected size profile and acts on the worst offenders: re-grabs the bloated, rescans the mis-graded.

**Per-user & multi-instance**
- **Personalized playlists** — a deterministic, spoiler-safe "Up Next" ordering engine ranked by each viewer's taste, with opt-in write-back to each member's *own* Plex account (default-off, fail-closed, per-server token; movies fully end-to-end, TV built and gated on episode-data coverage).
- **Multi-instance Sonarr/Radarr** — routes each title to the right instance and root folder (standard / 4K / anime).

**Integrations & automation**
- **Connected services** — Plex, Sonarr, Radarr, Tautulli, Trakt, MyAnimeList, MDBList, TheTVDB, Common Sense Media — all optional, all degrade gracefully.
- **Guided onboarding** — an interactive setup walks you through connecting each service and writes a complete config for you; it can also generate that config non-interactively, so a Docker container comes up pre-configured.
- **Write-back** — keeps your Trakt collection/history and MyAnimeList progress in sync.
- **Background enrichment daemon** — continuously enriches metadata out-of-band (opt-in).
- **End-of-run report** — one consolidated grid of every decision and movement (with optional Discord notification).
- **Scheduled runs** — Task Scheduler / cron baseline, with an optional Tautulli "Watched" hook for near-instant freshness.
- **Dry-run everywhere** — preview every decision before a single write happens.

### 🚧 In progress
- [ ] **First public release** — published Docker image + setup & configuration docs.
- [ ] **Watchlist-driven acquisition** — the multi-user watchlist union is already snapshotted and a forward-validation harness now measures whether it actually predicts what you watch; wiring it in as a live next-watch signal follows once it proves a real lift.
- [ ] **Smarter decisions under pressure** — a space-aware quality ceiling and scoring-order fixes, building on the recency- and likelihood-aware delete ranker already in place.
- [ ] **Sharper acquisition tuning** — runtime-scaled episode budgets and recency-ordered prefetch.
- [ ] **Fresh Arrivals playlist** — a per-profile, taste-ranked list of *genuinely* new acquisitions. Its edge over Plex's built-in *Recently Added*: **churn-immune** (Glidearr's own upgrades / re-grabs / library moves bump Plex's `addedAt`, so that row is noisy — Fresh Arrivals keys on real acquisitions) and a **kid-safe**, age-gated "new for you" surface (kids see playlists, not collections).
- [ ] **Hybrid universe acquisition** — start *any* corner of a shared universe (the MCU, Star Trek, Star Wars, the Arrowverse, One Chicago…) and Glidearr backfills the **rest of that saga in in-universe timeline order** — films via Radarr, shows via Sonarr — filling from the **start** first (watch Clone Wars → it pulls Episodes I–III up front), weaving movies and episodes onto one axis. Household-driven, extend-only, default-off, dry-run-safe. The cross-media timeline data + acquire-plan are landed and tested; the cross-service coordinator + grab paths are next. *(Design & detailed plan: [`services/coordinator/universe_acquisition.md`](scripts/managers/services/coordinator/universe_acquisition.md).)*

### 🔭 Planned / exploring
- [ ] **Learned next-watch re-ranker** — a household-specific model trained on what you actually watched next, layered on top of the deterministic scorer (with a calibrated 0–1 "watch probability"); the forward-validation harness is the first piece of groundwork. Cheap wins first — diversity passes and broader candidate sourcing.
- [ ] **Cast & crew discovery** — a people co-occurrence matrix so "you love this actor/director" surfaces their other work, for both scoring and acquisition.
- [ ] **Series & saga resumption** — circle back to catch you up: re-acquire and re-queue the last *N* episodes when you add a show to your watchlist or a new season is announced, and re-grab the prior film when a favoured sequel is dated — priority ramping as the release nears. *(Detailed under **How it works** above.)*
- [ ] **"This Week in History" discovery** — a per-user, opt-in weekly shelf of movies and episodes that aired or released *this* calendar week in **any** past year (anniversary picks). It grabs the most watchable into a self-cleaning weekly trial, learns from what you actually watch (engage a saga → it backfills the rest), and reclaims the unwatched every Saturday. The acquisition budget adapts to your watch rate + free space, yields to franchise backfill, and is fully isolated from the deletion path. *(Design & detailed plan: [`services/coordinator/this_week_in_history.md`](scripts/managers/services/coordinator/this_week_in_history.md).)*
- [ ] **Per-device codec-aware quality** — prefer the codecs each viewer's devices direct-play (no transcoding), tuned per title.
- [ ] **Finish the Saga + full TV Up Next** — resume a part-watched series or film franchise, and complete TV *Up Next*, once episode-data coverage is in place.
- [ ] **Per-user taste throughout** — personal re-ranking that can override household defaults, plus per-profile kids-safe gating.
- [ ] **Plex, deepened** — on-deck / continue-watching, per-user ratings, an orphan/missing reconciler, and collections — all opt-in and validated before they influence any decision.
- [ ] **Performance** — vectorized batch scoring for very large libraries.

## Contributing

Glidearr is approaching its first public release — full contribution guidelines, a dev-setup guide, and the test / brain-purity conventions are on the way. Issues, ideas, and feedback are welcome any time. <!-- TODO: CONTRIBUTING.md -->

## Acknowledgements

Glidearr stands on the shoulders of the self-hosted media community — especially the **Servarr** project (Sonarr, Radarr, Prowlarr), **Plex**, **Tautulli**, **Trakt**, **MyAnimeList**, and **MDBList**. Glidearr orchestrates these tools; it doesn't replace them.

## License

Copyright © 2026 Robert Adams

Glidearr is released under the **GNU Affero General Public License v3.0** — see [`LICENSE`](LICENSE). AGPL keeps Glidearr open: anyone who runs a modified version — including as a hosted or networked service — must make their source available under the same terms. *(Once Glidearr ships a web UI, it will offer its corresponding source in-app, per AGPL §13.)*

---

<div align="center">

*New horizons in. Watched ones out. Always moving.*

</div>

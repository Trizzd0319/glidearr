# acquisition

> Breadcrumb: [glidearr](../../../..) › [scripts](../../../README.md) › [managers](../../README.md) › [machine_learning](../README.md) › **acquisition**

**Package** — `scripts.managers.machine_learning.acquisition`
**Run position** — Sonarr episode planning during phase 2; Phase-3 acquisition; enrichment prioritisation for the daemon.
**One-liner** — What to fetch next: demand-weighted breadth, release-proximity resumption, next-episode budgeting, pilot profile stepping, and enrichment ordering. Planning, never adding.

---

## Purpose

From [`__init__.py`](./__init__.py):

> **what to fetch next (planning, not adding).** Next-episode budgeting, pilot
> profile stepping, monitored-missing search policy, and Trakt enrichment
> prioritisation. **Services do the monitor/search APPLY.**

The governing insight, from [`demand.py`](./demand.py):

> A downloaded file is **SHARED**, so the honest value of a grab is **how many
> people will watch it.**

Watchability answers *how much is this worth?* Demand answers *to how many?* —
and which of those should dominate depends on how much disk is left.

---

## Script inventory

| Script | Size | Role | Tests |
|---|---|---|---|
| [`pilot_stepping.py`](./pilot_stepping.py) | 24.3 KB | Pilot profile-ladder stepping | ✅ 18.4 KB |
| [`next_episode_planner.py`](./next_episode_planner.py) | 13.3 KB | Next-episode budgeting | ✅ 14.0 KB |
| [`enrichment_prioritizer.py`](./enrichment_prioritizer.py) | 7.0 KB | Trakt enrichment ordering | ✅ 7.6 KB |
| [`resumption_planner.py`](./resumption_planner.py) | 4.4 KB | `ramp`, `priority`, `resumption_priority` | ✅ 4.5 KB |
| [`demand.py`](./demand.py) | 2.8 KB | `demand_score`, `demand_priority` | ✅ 2.6 KB |

Also [`test_pilot_interactive.py`](./test_pilot_interactive.py) (4.6 KB), which
has no source twin here — the interactive path lives in
`sonarr/cache/pilot_interactive.py`.

**Every module has a matching test**, and the test files are consistently as
large as or larger than their sources.

---

## Demand — breadth as currency

```
demand   = Σ over active users of P(user watches)
             per user: genre_match(genres, their affinity), kept only if ≥ 0.15
             a user with NO affinity contributes the POPULARITY prior, not zero

priority = watchability × demand^t          t from space/tightness
```

| `t` | Regime | Effect |
|---|---|---|
| `0` | Roomy | Demand-neutral — `priority == watchability`, grab broadly |
| `1` | At the floor | `watchability × demand` — *"a 3-user title outranks a 1-user title 3:1"* |

> A 0-demand candidate is neutral when roomy but **falls to 0 as space tightens** —
> grabbed only in genuine abundance.

The cold-start branch is the careful part: *"a user with NO affinity (cold start)
contributes the `popularity` prior (0–1) instead of a flat zero, so a no-history
account doesn't drag the breadth signal down."*

---

## Resumption — the release-proximity ramp

> Resumption circles the rolling window **BACK**: when you're about to return to a
> show, or the world dates a new season / a sequel from cast & crew you love, it
> re-acquires the recent prior content and **RAMPS priority as the release nears**
> so you're caught up before you sit down.

```
R(d) ∈ [0, 100],  d = days until release (NEGATIVE once released)

d > W                →  0                          too far out
R₀ < d ≤ W           →  100·(W − d)/(W − R₀)        approaching — linear rise
−G ≤ d ≤ R₀          →  100                        ready window + grace
d < −G               →  100·exp(−(−d − G)/τ)       released a while ago — decay
```

| Constant | Default | Meaning |
|---|---|---|
| `W` `ramp_window_days` | 60 | Beyond this, ignore |
| `R₀` `ready_by_days` | 7 | Peak — *"be caught up a little EARLY so the re-grab can finish"* |
| `G` `grace_window_days` | 14 | *"you can still catch up if it just dropped"* |
| `τ` `decay_tau_days` | 30 | Half-life ≈ `τ·ln2` ≈ 21 days |

Final blend:

```
P = clamp(w_affinity·S_prior + w_proximity·R, 0, 100)      defaults 0.5 / 0.5
```

`days_to_release is None` is the trigger-1 *"return now"* case — `d := R₀`, so the
ramp sits at maximum.

---

## Navigation

- **Up:** [`machine_learning/`](../README.md) · **Design:** [`DESIGN.md`](./DESIGN.md)
- **Design note:** [`DESIGN_series_saga_resumption.md`](../DESIGN_series_saga_resumption.md) §4
- **Inputs:** [`space/tightness.py`](../space/README.md) · [`playlists/per_user.genre_match`](../playlists/README.md)
- **Consumers:** [`services/acquisition/`](../../services/acquisition/README.md) · [`services/sonarr/`](../../services/sonarr/README.md) · the enrichment daemon

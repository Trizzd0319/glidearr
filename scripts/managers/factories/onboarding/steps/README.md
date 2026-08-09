# onboarding / steps

> Breadcrumb: [glidearr](../../../../..) › [scripts](../../../../README.md) › [managers](../../../README.md) › [factories](../../README.md) › [onboarding](../README.md) › **steps**

**Package** — `scripts.managers.factories.onboarding.steps`
**Run position** — Inside `OnboardingManager.run()`, in the order returned by `build_steps()`. Lazily imported, so a configured install never pays the cost.
**One-liner** — One step object per configuration domain; each mutates the config in place, returns `StepResult` rows, and fails in isolation.

---

## Purpose

The wizard covers roughly a dozen configuration domains. As a single function it
would be ~800 lines with no way to run one domain in isolation, no way to test a
domain without stubbing every other prompt, and no way to let one flaky service
fail without taking the rest with it.

Each domain is therefore a **step object** with a uniform contract:

```python
step.run(prompter, cfg, ctx) -> list[StepResult]
```

- `prompter` — abstracts interactive / headless / forced input
- `cfg` — the merged config canvas, mutated **in place**
- `ctx` — shared dict for step-to-step handoff (e.g. discovered root folders)
- returns — result rows for the summary table

---

## Script inventory

| Script | Domain | Notes | Status |
|---|---|---|---|
| [`base.py`](./base.py) | — | `StepResult` + the step base class / protocol | ✅ Implemented |
| [`arr.py`](./arr.py) | Sonarr + Radarr | Instance count, URLs, API keys; **publishes discovered root folders into `ctx`** | ✅ Implemented |
| [`library.py`](./library.py) | Library paths | Consumes `ctx["root_folders"]` from [`arr.py`](./arr.py) | ✅ Implemented |
| [`media.py`](./media.py) | Plex, Tautulli, TVDB | Watch-history and metadata sources | ✅ Implemented |
| [`routing.py`](./routing.py) | Instance routing | Which instance receives what | ✅ Implemented |
| [`trakt.py`](./trakt.py) | Trakt | Device-flow OAuth via [`../oauth.py`](../oauth.py) | ✅ Implemented |
| [`mal.py`](./mal.py) | MyAnimeList | OAuth; self-disables if unauthorized | ✅ Implemented |
| [`mdblist.py`](./mdblist.py) | MDBList | Age / certification data source | ✅ Implemented |
| [`nextep.py`](./nextep.py) | Next-episode planning | Resumption behaviour | ✅ Implemented |
| [`english_dub.py`](./english_dub.py) | English dub preference | Anime dub handling | ✅ Implemented |
| [`deletions.py`](./deletions.py) | Deletion policy | Thresholds, protected tags | ✅ Implemented |
| [`daemons.py`](./daemons.py) | Background daemons | Enables enrichment / pilot-search daemons | ✅ Implemented |
| [`notifications.py`](./notifications.py) | Discord | Webhook | ✅ Implemented |
| [`extras.py`](./extras.py) | Misc tunables | Free space, dry-run, genres, scoring defaults | ✅ Implemented |
| [`__init__.py`](./__init__.py) | — | `build_steps(logger, only_service)` — the ordered list | ✅ Implemented |

## Test coverage

| Test | Covers |
|---|---|
| [`test_phases.py`](./test_phases.py) | Step ordering and phase grouping |
| [`test_arr_cf_sync.py`](./test_arr_cf_sync.py) | Custom-format sync during arr setup |
| [`test_routing_step.py`](./test_routing_step.py) | Routing configuration |
| [`test_deletions_step.py`](./test_deletions_step.py) | Deletion policy capture |
| [`test_english_dub_step.py`](./test_english_dub_step.py) | English-dub preference |
| [`test_nextep_step.py`](./test_nextep_step.py) | Next-episode planning |
| [`test_plex_pins.py`](./test_plex_pins.py) | Plex pin/auth flow |

---

## Ordering matters

`build_steps()` returns a **deliberately ordered** list. Two dependencies are
real rather than cosmetic:

| Step | Depends on | Via |
|---|---|---|
| [`library.py`](./library.py) | [`arr.py`](./arr.py) | `ctx["root_folders"]` — offers discovered paths instead of asking the user to retype them |
| [`routing.py`](./routing.py) | [`arr.py`](./arr.py) | Instances must exist before they can be routing targets |

Everything else is ordered for a sensible operator narrative — connect the
downloaders, then the watch-history sources, then the policies — not because of
a data dependency.

---

## Data in / data out

| Direction | Source/Sink | Payload |
|---|---|---|
| IN | `prompter` | Operator answers, or `RECOMMENDARR_*` values |
| IN | Live service APIs | Connectivity probes via [`../validators.py`](../validators.py) |
| IN | Trakt / MAL OAuth | Tokens via [`../oauth.py`](../oauth.py) |
| IN/OUT | `cfg` | Mutated in place |
| IN/OUT | `ctx` | Step-to-step handoff |
| OUT | `list[StepResult]` | Summary table rows |

Steps do **not** save. Persistence is `OnboardingManager._save()`'s job — which
is what makes abort-without-save possible.

---

## Navigation

- **Up:** [`onboarding/`](../README.md)
- **Design:** [`DESIGN.md`](./DESIGN.md)
- **Related:** [`../schema.py`](../schema.py) · [`../env_map.py`](../env_map.py) · [`../prompts.py`](../prompts.py) · [`../validators.py`](../validators.py) · [`../oauth.py`](../oauth.py)

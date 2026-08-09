# onboarding — Design

> Breadcrumb: [glidearr](../../../..) › [scripts](../../../README.md) › [managers](../../README.md) › [factories](../README.md) › **onboarding**

**Package** — `scripts.managers.factories.onboarding`
**Status** — ✅ Implemented
**Related** — [README.md](./README.md) · [`steps/`](./steps/README.md) · [`config/DESIGN.md`](../config/DESIGN.md)

---

## 1. Problem statement

A fresh Glidearr install needs credentials for six services, root-folder paths,
genre classifications, space thresholds and a few dozen scoring tunables — before
a single manager can be constructed. That produces four hard constraints:

1. **Ordering.** Secrets must be in the keyring *before* `ConfigManager` exists,
   or `SecretBootstrap` inside it will prompt a second time for values the user
   just supplied.

2. **Two environments, one flow.** An interactive desktop run should ask
   questions. A headless unRAID container has no TTY and must be configured
   entirely from `RECOMMENDARR_*` environment variables. Maintaining two setup
   paths guarantees they drift.

3. **Partial state is worse than no state.** A wizard abandoned halfway that
   persists what it collected leaves an install that *looks* configured and
   fails obscurely later.

4. **Never nag a working install.** The shipped `default_config.json` has
   `firstRunCompleted: false`. A naive check on that flag alone would re-run the
   wizard on every launch of a perfectly good install.

Constraint 4 is the subtle one, and it is why first-run detection is a
**two-condition** test rather than a flag read.

---

## 2. Design goals & non-goals

### Goals

| # | Goal |
|---|---|
| G1 | The keyring is provisioned before `ConfigManager` is built. |
| G2 | One flow serves interactive and headless, with no duplicated logic. |
| G3 | Abandonment saves nothing. |
| G4 | A genuinely configured install is never re-onboarded. |
| G5 | One flaky service never traps the user. |
| G6 | Headless failures name the exact environment variables to set. |
| G7 | A single service can be reconfigured without touching global setup state. |

### Non-goals

| # | Non-goal | Why |
|---|---|---|
| N1 | A GUI installer | Terminal + env vars covers both real deployments. The web layer may supersede this — see §9 P8. |
| N2 | Config migration between versions | Additive keys with defaults have sufficed. |
| N3 | Validating every tunable | Connectivity is validated; scoring weights are recorded as given. |
| N4 | Being a `BaseManager` | It must run before the manager tree exists. |

---

## 3. Architecture

### 3.1 Component map

```
main.py __main__
   │  OnboardingManager.run_if_needed(logger)      ← FIRST action, before ConfigManager
   ▼
OnboardingManager        (__init__.py — NOT a BaseManager, registers nothing)
   │
   ├── schema.py         empty_config(), deep_merge()  — the full canvas
   ├── prompts.py        make_prompter(mode) → interactive | headless | forced
   ├── env_map.py        RECOMMENDARR_* → config path mapping
   ├── validators.py     live connectivity probes
   ├── oauth.py          Trakt device flow, MAL OAuth
   └── steps/            ordered step objects, lazily imported
          arr · library · media · routing · trakt · mal · mdblist
          nextep · english_dub · deletions · notifications · daemons · extras
   │
   ▼
ConfigLoader.save(cfg)   ← strips secrets to keyring, blanks on disk
SecretStore.set(SENTINEL_PATH, "1")   ← marks store provisioned (G1)
```

### 3.2 Control flow

```
run_if_needed:
    load existing config
    RECOMMENDARR_SKIP_ONBOARDING set? → "skipped"
    needs_onboarding(cfg)?            → no  → "skipped"
    OnboardingManager(...).run()      → True → "ok"
                                      → False→ "incomplete" → main exits 1

run:
 1. cfg = deep_merge(schema.empty_config(), existing)   ← every key exists
 2. ctx = {"root_folders": []}                           ← shared across steps
 3. for step in steps.build_steps(only_service=…):
        try:    results += step.run(prompter, cfg, ctx)
        except KeyboardInterrupt / EOFError:  raise      ← abort, NO save (G3)
        except Exception as e:  warn + failed StepResult ← continue (G5)
 4. usable = not _looks_fresh(cfg)
    if only_service is None:  cfg["firstRunCompleted"] = bool(usable)
 5. _save: loader.save(cfg); store.set(SENTINEL_PATH, "1")
 6. _summary: log_table of StepResult rows + missing env var names (G6)
```

Two exception classes, two behaviours. `KeyboardInterrupt` / `EOFError` mean the
operator left or there is no usable stdin — abort without saving (G3). Anything
else means one service misbehaved — record it and keep going (G5).

### 3.3 The first-run gate

```python
needs_onboarding(cfg) == (not cfg["firstRunCompleted"]) and _looks_fresh(cfg)

_looks_fresh(cfg) == (no sonarr/radarr instance carrying url|base_url,
                      skipping the "default_instance" marker key)
                 and (not cfg["trakt"]["client_id"])
```

The second condition exists purely to defend against constraint 4: the shipped
default has `firstRunCompleted: false`, so the flag alone is not evidence of a
fresh install.

### 3.4 The shared `ctx`

Steps mutate `cfg` in place and additionally thread a `ctx` dict. Its purpose is
step-to-step handoff: the `arr` step publishes root folders it discovered from
Sonarr/Radarr, and the `library` step later offers those as choices rather than
asking the user to retype paths. Without `ctx`, either the steps would have to
re-query, or the user would type paths the system already knew.

---

## 4. Key decisions & rationale

| # | Decision | Rationale | Alternative rejected |
|---|---|---|---|
| D1 | Runs before `ConfigManager` | G1 — the only ordering that prevents a double prompt | Lazy prompt inside `SecretBootstrap` |
| D2 | Not a `BaseManager` | The manager tree does not exist yet | Make it a manager |
| D3 | `deep_merge(empty_config(), existing)` | Guarantees every nested key exists while preserving user values — no half-populated dicts | Patch keys as encountered |
| D4 | Step objects, lazily imported | Each service is independently testable; the import cost is only paid on a first run | One monolithic wizard |
| D5 | Prompter abstraction (`make_prompter(mode)`) | G2 — one flow, three modes (interactive / headless / forced) | Two code paths |
| D6 | `KeyboardInterrupt`/`EOFError` propagate; others are caught | G3 vs G5 — abandonment and service flakiness are different events | Catch everything |
| D7 | Two-condition first-run gate | G4 — the shipped default carries `firstRunCompleted: false` | Flag alone |
| D8 | `only_service` never touches `firstRunCompleted` | G7 — a targeted reconfigure must not flip global state | Recompute always |
| D9 | Headless collecting nothing leaves the flag **false** | Auto-onboarding re-runs next launch rather than trapping the user in a broken install | Stamp complete regardless |
| D10 | `SENTINEL_PATH` written to the secret store | Positive evidence the store is provisioned, so `SecretBootstrap` stays quiet | Infer from secret presence |
| D11 | `dry_run` defaults to `True` in the skeleton | A new install rehearses before it deletes | Default `False` |
| D12 | Missing headless values reported with exact env var names | G6 — "trakt.client_id missing" is not actionable; `RECOMMENDARR_TRAKT_CLIENT_ID` is | Generic error |

---

## 5. Invariants

| # | Invariant |
|---|---|
| I1 | Onboarding completes before `ConfigManager` is constructed. |
| I2 | No secret is ever written to `config.json` in plaintext. |
| I3 | An aborted wizard persists nothing. |
| I4 | A config with a real arr instance or Trakt creds is never re-onboarded. |
| I5 | `only_service` leaves `firstRunCompleted` untouched. |
| I6 | Every key in `schema.empty_config()` exists after `run()`. |
| I7 | A single step's failure never aborts the wizard. |
| I8 | Onboarding performs no FETCH/CACHE/APPLY against the media stack — only setup-time connectivity probes and OAuth. |

---

## 6. Failure modes & degradation

| Failure | Detection | Behaviour | Blast radius |
|---|---|---|---|
| Ctrl-C mid-wizard | `KeyboardInterrupt` | "Onboarding cancelled — no changes saved" | None (I3) |
| Headless, no TTY, no env vars | `EOFError` on a required field | Aborts, `firstRunCompleted` stays false, `"incomplete"` → exit 1 with env-var guidance | Install unusable until configured |
| One service unreachable | Step raises | Warning + failed `StepResult`, wizard continues | That service unconfigured |
| Trakt device flow times out | OAuth step | Failed `StepResult` | Trakt unconfigured |
| Keyring unavailable | `ConfigLoader.save` | Secret stays inline in `config.json` per the blanking guard | 🟡 Secret on disk |
| `firstRunCompleted: false` on a real install | `_looks_fresh` → `False` | Skipped (I4) | None |
| Partial arr config (URL, no key) | **Not validated as a pair** | `_looks_fresh` is `False` (URL present) → considered configured | 🟡 Silently broken instance |
| Config path unwritable | `_save` raises | Wizard work lost | Full re-run needed |

**Row 7 is the gap.** `_looks_fresh` tests only for a `url`/`base_url`. An
instance with a URL and a missing or invalid API key satisfies the "not fresh"
test, so onboarding is skipped and the failure surfaces later as an auth error
during the run.

---

## 7. Configuration surface

Onboarding **writes** essentially the whole `config.json`. Blocks it populates:

| Block | Contents |
|---|---|
| `sonarr_instances` / `radarr_instances` | Per-instance URL, API key, `default_instance` pointer |
| `tautulli` / `plex` / `trakt` / `mal` / `tvdb` | URLs, keys, OAuth tokens |
| `rootFolders` | Discovered or entered library paths |
| `animeGenres` / `documentaryGenres` | Classification genre lists |
| `free_space_limit` | Space floor |
| `dry_run` | Defaults `True` (D11) |
| `notifications.discord` | Webhook |
| owned-movie / space-pressure / watch-likelihood / scoring tunables | Brain defaults |
| `firstRunCompleted` | The only key this manager sets directly |

**Read** from the environment:

| Variable | Effect |
|---|---|
| `RECOMMENDARR_SKIP_ONBOARDING` | Short-circuits to `"skipped"` |
| `RECOMMENDARR_*` | Headless values, mapped by [`env_map.py`](./env_map.py) |

---

## 8. Implemented capabilities

- ✅ Full first-run wizard across Sonarr, Radarr, Trakt, Tautulli, Plex, TVDB, MAL, MDBList
- ✅ Trakt device-flow OAuth and MAL OAuth token generation
- ✅ Live connectivity validation (`system_status`, `rootfolder`, `get_server_info`, `/identity`, `/users/me`)
- ✅ Root-folder discovery published through `ctx` to later steps
- ✅ Headless configuration from `RECOMMENDARR_*` with exact-name reporting for missing values
- ✅ Interactive / headless / forced prompter modes behind one flow
- ✅ Two-condition first-run gate defending the shipped default
- ✅ Abort-without-save on cancel
- ✅ Per-step failure isolation with a summary table
- ✅ Single-service reconfigure (`only_service`) that preserves global state
- ✅ Secret persistence to the OS keyring plus a provisioning sentinel
- ✅ Skeleton merge guaranteeing every key exists

## 9. Planned additions

| # | Addition | Value | Effort | Depends on |
|---|---|---|---|---|
| P1 | **Validate URL + API key as a pair** in `_looks_fresh` | Closes §6 row 7 — a URL with a broken key currently reads as configured | S | — |
| P2 | **Re-run a single step** without the full wizard (`--step trakt`) | `only_service` exists but is coarse | S | — |
| P3 | **Config doctor** — validate an existing install and report exactly what is broken | Diagnoses the §6 row 7 class of failure after the fact | M | P1 |
| P4 | **Secret rotation** — re-prompt one credential | Currently needs the whole wizard | S | [`config/`](../config/README.md) P6 |
| P5 | **Emit a `.env` / Docker-compose template** from an interactive run | Makes migrating a desktop setup to unRAID mechanical | S | [`env_map.py`](./env_map.py) |
| P6 | **Schema-driven step generation** — derive prompts from a typed schema | Adding a config key currently means editing a step | M | Typed schema |
| P7 | **Dry-run onboarding** — show what would be written without writing | Safe inspection before committing | S | — |
| P8 | **Web onboarding** — the same steps rendered as browser forms | Removes the terminal requirement entirely; the schema work in P6 is shared with the web config editor | L | [`web/`](../web/DESIGN.md), P6 |
| P9 | **Re-validate on demand** — probe all configured services and report | Confirms an install still works after a network change | S | [`validators.py`](./validators.py) |
| P10 | **Guided `movieRootFolders` setup** — currently `[]`, so movie classification buckets are computed then discarded | Closes the live config gap in [`scripts/DESIGN.md`](../../../DESIGN.md) §9 P3 | S | Schema decision |
| P11 | **Import from an existing Recommendarr config** | Migration path for the old mirror layout | S | — |
| P12 | **Token expiry warnings** — flag OAuth tokens nearing expiry | Trakt/MAL tokens expire silently today | M | — |

## 10. Open questions

| # | Question | Blocking |
|---|---|---|
| Q1 | Should a URL-without-key instance be treated as fresh (re-onboard) or as broken (fail loudly)? Failing loudly seems right. | P1 |
| Q2 | Does the web layer replace this wizard, or wrap the same steps? Wrapping preserves the headless path. | P8 |
| Q3 | Should scoring tunables be in onboarding at all, or deferred to a tuning UI? They inflate a first run considerably. | P8 |
| Q4 | Where should `movieRootFolders` be captured — here, or derived from classification? | P10 |

## 11. Related designs

- [`steps/DESIGN.md`](./steps/DESIGN.md) — the step protocol
- [`config/DESIGN.md`](../config/DESIGN.md) — the loader/secret machinery this drives
- [`config/DESIGN_secrets_backend.md`](../config/DESIGN_secrets_backend.md)
- [`web/DESIGN.md`](../web/DESIGN.md) — P8
- [`support/setup/onboarding.py`](../../../support/setup/onboarding.py) — the CLI entry point

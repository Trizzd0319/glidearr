# OnboardingManager

- **File** — `scripts/managers/factories/onboarding/__init__.py`
- **One-liner** — The first-run / full-setup wizard that interrogates the operator (interactively at a TTY or from `RECOMMENDARR_*` env vars headlessly) and writes a complete, secret-free `config.json` plus keyring-stored secrets before any service manager is built.

## What it does (for a senior Python engineer)

`OnboardingManager` is the entry point for establishing an entire Glidearr setup in one flow: root folders, the count of Sonarr/Radarr sessions and their API keys, Trakt OAuth (including device-flow token generation), Tautulli, Plex, TVDB, MAL, genres, free-space limit, dry-run, and Discord notifications. Secrets are persisted to the OS keyring via the existing `ConfigLoader` machinery; nothing secret is written to disk.

Note on the manager tree: despite living under `factories/`, `OnboardingManager` is **NOT** a `BaseManager` subclass and is **not** part of the singleton manager tree. It has no parent manager, registers nothing in `RegistryManager`, and loads no submanagers via `load_components`. It is a plain class instantiated once, very early, directly from `main.py`'s `__main__` block — before `ConfigManager` exists. The actual setup work is decomposed into **step objects** loaded lazily from the `steps/` subpackage (a separate, out-of-scope work item); this manager is the orchestrator that builds the prompter, runs the steps in order, and saves the result.

Key public surface:

- `__init__(self, logger=None, config_path=None, loader=None, existing=None, prompter=None, mode=None, reconfigure=False, only_service=None)` — wires up a `LoggerManager`, resolves the config path (defaults to `scripts/support/config/config.json`), creates (or accepts an injected) `ConfigLoader`, grabs the loader's `_secret_store`, loads the existing config, and builds a `Prompter` via `make_prompter(mode, …)`. `only_service` scopes a single-service reconfigure; `reconfigure` is stored but interpreted by the steps. Heavy DI surface so tests can inject fakes.
- `run(self) -> bool` — the main flow (see *How it functions*). Returns `True` only when a **usable** config was collected (at least one Sonarr/Radarr instance with a URL, or Trakt credentials). Lazily imports `steps` and `steps.base.StepResult`.
- `needs_onboarding(cls, config) -> bool` (classmethod) — first-run detection. Returns `True` only when `firstRunCompleted` is falsey **and** the config "looks fresh" (no configured Sonarr/Radarr instance and no Trakt `client_id`). This guards against the shipped default state where `firstRunCompleted: false` rides along with an already-populated config.
- `run_if_needed(cls, logger=None, config_path=None) -> str` (classmethod) — the auto first-run hook `main.py` calls. Returns `"skipped"` (already configured, or `RECOMMENDARR_SKIP_ONBOARDING` is set), `"ok"` (ran and produced a usable config), or `"incomplete"` (ran but collected nothing usable — e.g. headless with no env vars and no TTY).
- Internal helpers: `_config_dict` (normalizes dict / `.raw_data` / `.config` shapes), `_looks_fresh`, `_save`, `_summary`.

FETCH / CACHE / APPLY: this manager does **none** of the three service verbs against the media stack at runtime. It performs setup-time HTTP only indirectly — the step objects call `validators.py` (live Sonarr/Radarr/Tautulli/Plex connectivity checks) and `oauth.py` (Trakt/MAL token flows). It does not touch `global_cache` or any Parquet keys; its only persistence target is `config.json` + the secret store. The closest analogue to APPLY is `_save`, which writes the config file and stamps the secret-store sentinel.

External endpoints touched (indirectly, via sibling helper modules the steps drive): Trakt `https://api.trakt.tv` (`/oauth/device/code`, `/oauth/device/token`, `/oauth/token`, `/users/me`), MAL `https://myanimelist.net/v1/oauth2/{authorize,token}` and `https://api.myanimelist.net/v2/users/@me`, Tautulli `/api/v2?cmd=get_server_info`, Plex `/identity`, and Sonarr/Radarr `system_status` + `rootfolder` via `arrapi`.

Config keys: writes essentially the entire `config.json` skeleton defined in `schema.empty_config()` — `sonarr_instances`, `radarr_instances`, `tautulli`, `trakt`, `mal`, `plex`, `tvdb`, `rootFolders`, `animeGenres`, `documentaryGenres`, `free_space_limit`, `dry_run`, `notifications.discord`, plus the large block of owned-movie / space-pressure / watch-likelihood / scoring tunables. The one key this manager itself sets is `firstRunCompleted` (see below). Headless reads come from `RECOMMENDARR_*` env vars via `env_map.py`.

`dry_run`: not directly relevant — onboarding never mutates a live service. The wizard *sets* the `dry_run` config value (defaulting `True` in the skeleton) that downstream managers honour.

Concurrency / singleton notes: not a singleton and not thread-aware. It is intended to run exactly once, single-threaded, at process start (before `ConfigManager`), so the keyring is provisioned and `SecretBootstrap` inside `ConfigManager` does not double-prompt.

## How it functions

Lifecycle: `__init__` → (lazy) `steps.build_steps(...)` → iterate steps → `_save` → `_summary`.

1. **Build the canvas.** `run` merges any existing config over the full skeleton: `schema.deep_merge(schema.empty_config(), self._existing or {})`. This guarantees every expected key exists (no half-populated nested dicts) while preserving what the user already had. A shared `ctx` dict (`{"root_folders": []}`) is threaded through every step so, e.g., the arr step can publish discovered root folders that the library step later offers as choices.
2. **Run the steps.** `steps.build_steps(logger=…, only_service=…)` returns the ordered step list; each `step.run(prompter, cfg, ctx)` mutates `cfg` in place and returns a list of `StepResult` rows. Per-step exceptions are caught, logged as a warning, and recorded as a failed `StepResult` — one flaky service never traps the user. `KeyboardInterrupt`/`EOFError` propagate up: a forced-interactive run with no usable stdin aborts **without saving**, so a half/empty config is never persisted and completion is never stamped.
3. **Decide completion.** `usable = not self._looks_fresh(cfg)`. For a full run (`only_service` unset), `cfg["firstRunCompleted"]` is set to `bool(usable)`. A single-service reconfigure never touches the flag. A headless run that collected nothing leaves `firstRunCompleted` false on purpose, so auto-onboarding re-runs next launch rather than trapping the user.
4. **Persist.** `_save` calls `loader.save(cfg)` (which strips secrets out to the keyring/env and blanks them on disk), then `store.set(SENTINEL_PATH, "1")` to mark the store provisioned so `SecretBootstrap` won't re-prompt.
5. **Report.** `_summary` renders a `log_table` of the per-step result rows (icon / service / detail), and any required-but-missing headless values are logged with the exact env var names to set, plus guidance to run `python scripts/support/setup/onboarding.py --interactive` or supply `RECOMMENDARR_*` vars.

`needs_onboarding` is the gate `run_if_needed` consults; `run_if_needed` builds its own `LoggerManager`/`ConfigLoader`, loads the existing config, and only constructs and runs the manager when the install looks fresh.

Brain delegation: none. `OnboardingManager` makes no value judgements and delegates no decision to `machine_learning/`. (The watch-likelihood and scoring tunables it *writes* are later consumed by the ML brain, but that is downstream — the wizard merely records the operator's chosen defaults.)

## Criteria & examples

- **First-run gate (`needs_onboarding`).** Returns `True` iff `firstRunCompleted` is falsey AND `_looks_fresh` is true. `_looks_fresh` is true only when there is no Sonarr/Radarr instance carrying a `url`/`base_url` (the `default_instance` key is skipped) and no `trakt.client_id`.
  - *Example A:* a freshly shipped `config.json` with `firstRunCompleted: false`, empty `sonarr_instances`/`radarr_instances`, and blank Trakt → fresh → onboarding runs.
  - *Example B:* the same `firstRunCompleted: false` but with `sonarr_instances: {"default_instance": {"name": "sonarr"}, "sonarr": {"url": "192.168.1.110", …}}` → not fresh → onboarding **skipped**, because a real install must never be re-onboarded just because the flag was left false.
  - *Example C:* `firstRunCompleted: true` → skipped immediately, regardless of content.
- **Usable-config gate.** `run` returns `True` and stamps `firstRunCompleted: true` only when at least one arr instance has a URL or Trakt creds exist. A headless container started with **zero** `RECOMMENDARR_*` vars and no TTY produces only the empty skeleton → `usable` is false → flag stays false → `run_if_needed` returns `"incomplete"` → `main.py` logs the remediation guidance and exits with status 1.
- **Single-service reconfigure.** With `only_service="sonarr"`, the flag is left exactly as-is even if the resulting config happens to look fresh — a targeted reconfigure must not flip global setup state.
- **Cancel safety.** Pressing Ctrl-C (or hitting `EOFError` on a required field in a forced-interactive run) aborts before `_save` runs: "Onboarding cancelled — no changes saved."

## In plain English

Think of this as the "Welcome to Netflix, let's set up your account" screen — but for the whole home-media brain. The very first time you launch Glidearr, this wizard walks you through plugging in your movie/TV downloaders (Sonarr/Radarr), your watch-history sources (Trakt, Tautulli, Plex), where your shows and anime live on disk, how much free space to keep, and whether it should actually make changes or just rehearse ("dry run"). Your passwords and API keys go into the operating system's secure keychain — never written into a plain file — like a password manager rather than a sticky note. It is careful in two ways: if you bail out partway through, it throws away everything so you never end up half-configured; and it is smart about *not* nagging — once you're genuinely set up (it checks whether you actually have a Sonarr/Radarr server or Trakt login), it stays out of your way on every future launch. For a headless box like an unRAID server with no keyboard attached, the same wizard reads everything from environment variables in your Docker template instead of asking questions on screen.

## Interactions

- **Caller / "parent":** `scripts/main.py` `__main__` block invokes `OnboardingManager.run_if_needed(logger=…)` as the very first action, before `ConfigManager` is constructed. There is no `BaseManager` parent link.
- **Sibling helper modules in this package (each its own file, separately documented):**
  - `schema.py` — `empty_config()` skeleton, `deep_merge`, `build_base_url`, `instance_block`. Provides the canonical config shape the wizard fills in.
  - `prompts.py` — `make_prompter`, the `Prompter` interface, and the `InteractivePrompter` / `HeadlessPrompter` implementations used for all I/O.
  - `oauth.py` — pure-HTTP Trakt device-flow + token refresh and MAL PKCE helpers; driven by the Trakt/MAL steps (and reused by the runtime `TraktInstanceManager`).
  - `validators.py` — live connectivity checks (`arr_status`, `tautulli_ping`, `plex_ping`) used by steps to confirm credentials and surface root folders.
  - `env_map.py` — the `RECOMMENDARR_*` headless contract plus `.env.example` / markdown-table generators.
- **`steps/` subpackage (out of scope here):** `build_steps()` returns the ordered step objects (arr, trakt, tautulli/plex media, library, MAL, notifications, daemons, next-episode, extras) that do the actual per-service collection. `OnboardingManager` only orchestrates them.
- **External infrastructure:** `ConfigLoader` (config read/save + secret stripping), the secret store / keyring (`SENTINEL_PATH` provisioning so `SecretBootstrap` doesn't re-prompt), and the third-party services reached during validation/OAuth (Trakt, MAL, Tautulli, Plex, Sonarr, Radarr).
- **Brain modules:** none directly. The tunables it persists are later read by `machine_learning/` planners, but no decision is delegated at setup time.

## Planned enhancement — Radarr instance categorization (TODO)

> **Status:** ✅ **PHASE 1 BUILT** (the categorization role map) — `RadarrStep.categorized=True`,
> `_collect_categorized` generalised off the hardcoded `sonarr_*` (uses `self.service` +
> `categorize_labels`, Radarr adds an optional `anime` tier), `radarr_instances_categorized` in
> `schema.empty_config()`, the `RECOMMENDARR_RADARR_INSTANCES_CATEGORIZED_*` headless contract, and
> `gateway.categorized_instance` is service-aware. **Remaining:** resolver routing + safe migration.

Today `RadarrStep` sets `categorized = False` (`steps/arr.py`), so a multi-instance Radarr
setup captures each instance's name/host/port/api but **not its purpose/role**. (Sonarr is now
a **single, un-tiered** instance — `SonarrStep` has `categorized = False` — so it has no
resolution-tier role map at all; per-episode JIT governs Sonarr quality instead. The
categorization role map below is a Radarr-only concern.)

Because Radarr lacks a role map, **every newly-acquired movie routes to `default_instance`** (e.g.
`standard`) regardless of whether a 4K-worthy watchability score should send it to a UHD
instance (e.g. `ultra`) — `gateway.categorized_instance` reads *only* the
`radarr_instances_categorized` key. (Confirmed live: a household with `standard` + `ultra`
Radarr instances has no role map, so the acquisition path is resolution-blind for movies.)

**Onboarding should ask the same question for Radarr** — which instance fills each role:
  * standard / HD (720p–1080p)
  * UHD / 4K
  * *(optional)* anime — if the user runs a dedicated anime Radarr instance

### To implement (when picked up)
1. `steps/arr.py` — set `RadarrStep.categorized = True`; generalise `_collect_categorized`
   to write `f"{service}_instances_categorized"` (it currently hardcodes `sonarr_…`) and to
   offer an optional **anime** category alongside the resolution tiers.
2. `schema.py` — add `radarr_instances_categorized` to `empty_config()` (the Radarr role map; Sonarr is single-instance and has no categorized key).
3. `env_map.py` — add the headless `RECOMMENDARR_RADARR_INSTANCES_CATEGORIZED_*` contract.
4. `acquisition/gateway.py::categorized_instance` — read `f"{self.service}_instances_categorized"`
   instead of the hardcoded `sonarr_instances_categorized`, so Radarr routing becomes role-aware.
5. Acquisition resolver — pick the categorized instance from the movie's target resolution tier
   (watchability score → quality profile) rather than `default_instance()`.

### (Related) Offer to mirror quality profiles / Custom Formats across instances

During Radarr/Sonarr validation the wizard already reaches each instance's API
(`validators.arr_status` → arrapi `system_status` + root folders). Extend it to also read each
instance's **quality profiles** and **Custom Formats** (arrapi `quality_profile` / `custom_format`;
REST `/api/v3/qualityprofile`, `/api/v3/customformat`) and, when **2+ instances** are configured
with **divergent** sets — e.g. TRaSH / Notifiarr-managed Custom Formats + profiles present on one
instance but not the other — **prompt whether to mirror them by NAME across instances.**

**Why:** profile/CF **IDs differ per instance**, but **names can be made to match**. Name-parity is
what lets cross-instance tier migration map *"this movie's profile in `standard`"* → *"the same-named
profile in `ultra`"* with no hardcoded id map (see the radarr/sonarr *"Multi-instance … migration"*
TODOs → **profile-parity, not profile-copy**).

**Scope notes:**
- Mirroring = create the missing **same-named** profiles/CFs in the target instance (copy the
  definition, mint a new local id); never silently overwrite an existing same-named one without consent.
- Opt-in prompt, default off; headless flag e.g. `RECOMMENDARR_RADARR_MIRROR_PROFILES` /
  `RECOMMENDARR_SONARR_MIRROR_PROFILES` (applies to **both** services' instances).
- Detection is source-agnostic — it reads whatever profiles/CFs the API exposes, however they were
  created (TRaSH, Notifiarr sync, Recyclarr, or hand-rolled).
- **Don't fight the sync tool:** if the user manages CFs via Recyclarr/Notifiarr, those re-assert on
  a schedule — so detect-and-prompt (or apply once on request), never *continuously enforce*, or you
  get a tug-of-war. Pick a **source-of-truth instance** and copy only *missing* same-named items to
  the others. Mirror **CFs before profiles**, then remap the profile's CF references name→new-id.
- This is a convenience that makes name-based routing reliable; it does **not** replace the
  categorization role map — the two compose.

> **Schema dependency (answers "does this touch schema too?" — yes).** Onboarding writes the config
> from `schema.empty_config()`, and `deep_merge` only preserves keys that exist in that skeleton, so
> **every new config key these enhancements collect must be added to `empty_config()`** (and given an
> `env_map.py` mapping for headless):
> - `radarr_instances_categorized: {}` — the Radarr role map (Sonarr has no categorized key; it is single-instance).
> - the mirror opt-in preference (e.g. `*_mirror_profiles` flag) — new key, if the choice is persisted
>   rather than act-and-forget.
>
> The act of mirroring itself mutates the Radarr/Sonarr instances via their APIs; only the **preference
> flag** (and the categorized role map) live in `config.json`/schema — the profiles/CFs do not.

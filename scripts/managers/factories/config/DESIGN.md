# config — Design

> Breadcrumb: [glidearr](../../../..) › [scripts](../../../README.md) › [managers](../../README.md) › [factories](../README.md) › **config**

**Package** — `scripts.managers.factories.config`
**Status** — ✅ Implemented
**Related** — [README.md](./README.md) · [DESIGN_secrets_backend.md](./DESIGN_secrets_backend.md) · [`factories/DESIGN.md`](../DESIGN.md)

---

## 1. Problem statement

Glidearr's configuration has three properties that conflict:

1. **It must be editable.** Thresholds, instance maps, feature gates, root
   folders — all hand-tuned over time.
2. **It contains secrets.** Six authenticated APIs, each with keys or OAuth
   tokens, scattered as leaves throughout a nested tree.
3. **It must be shareable.** Diagnosing a problem means pasting config. Doing so
   must not leak credentials.

The naive resolution — "keep secrets in a separate file" — fails because the
secrets are structurally *inside* the config tree (each Radarr instance has its
own `api_key`), and splitting them means maintaining two parallel trees.

The resolution here: **secrets are identified by key name anywhere in the tree,
overlaid from a secure store at load, and stripped again at save.** The file on
disk keeps the shape and loses the values.

Secondary problem: a first run has no secrets anywhere, and prompting for them
mid-run is unacceptable. Hence the ordering constraint that
[`onboarding/`](../onboarding/README.md) runs *before* `ConfigManager` exists.

---

## 2. Design goals & non-goals

### Goals

| # | Goal |
|---|---|
| G1 | One config object, injected everywhere. |
| G2 | Secrets never persist to disk in plaintext once a safe store exists. |
| G3 | A secret is never lost — blanking only happens when it is safely stored. |
| G4 | Config is shareable after a `log_safe_config()` dump. |
| G5 | Headless runs never block on an interactive prompt. |
| G6 | A crash mid-save never corrupts `config.json`. |

### Non-goals

| # | Non-goal | Why |
|---|---|---|
| N1 | Dotted-path access (`get("a.b.c")`) | `get()` is a flat top-level lookup. Nested access is explicit. |
| N2 | Schema migration framework | Additive keys with defaults have sufficed. |
| N3 | Config validation on load | [`validator.py`](./validator.py) exists but is not wired into the load path. See §9 P1. |
| N4 | Multi-environment profiles | Single host, single config. |

---

## 3. Architecture

### 3.1 Component map

`ConfigManager` is a thin facade — notably **not** a `BaseManager` subclass,
because it must exist before the manager tree does.

```
ConfigManager  (__Init__.py)
  ├── ConfigLoader      config_loader.py    load / save / secret overlay
  ├── ConfigResolver    config_resolver.py  instance maps, default instance
  ├── ConfigSanitizer   config_sanitizer.py redacted logging
  └── SecretBootstrap   secret_bootstrap.py audit + first-run wizard
                            └── SecretStore  secret_store.py
                                   env (RECOMMENDARR_*) → OS keyring

  config_constants.py  — key groups, sensitive-key set, templates
  config_cli.py        — CLI entry
  validator.py         — schema validation (⚠️ not wired into load)
```

### 3.2 Control flow — load

```
ConfigManager(logger, config_path="support/config/config.json")
  │
  ├─ ConfigLoader.load()
  │     read config.json
  │     walk the tree; for each leaf whose KEY NAME is_secret_key():
  │         resolve via SecretStore:  env → keyring → inline
  │         overlay the resolved value in memory
  │     LoggerManager.register_secrets(resolved)   ← scrubs ALL future log output
  │     warn on inline plaintext, suggest migrate_secrets.py
  │
  ├─ SecretBootstrap.ensure(config)      [try/except — never fatal, G5]
  │     audit: for each expected secret, report env | keyring | inline | missing
  │     if interactive TTY and anything missing → getpass wizard
  │
  ├─ ConfigSanitizer(logger)
  └─ ConfigResolver(config, logger)
```

### 3.3 Control flow — save

```
ConfigManager.save()  →  ConfigLoader.save(config)
     deep-copy the tree
     for each secret leaf:
         stored      = SecretStore.persist(value)
         env_present = the corresponding RECOMMENDARR_* var exists
         if stored or env_present:  blank it on disk      ← G2
         else:                      keep it inline        ← G3
     tempfile.mkstemp() → write → os.replace()            ← G6, atomic
     chmod 0600
```

`set(key, value)` writes through to disk immediately. `set_bulk(dict)` mutates
memory **only** — the caller must follow with `save()`. That asymmetry is a real
footgun; see §6.

### 3.4 Data contracts

| Contract | Shape |
|---|---|
| Secret identification | By key name via `is_secret_key()` — position-independent |
| Secret precedence | env `RECOMMENDARR_*` → OS keyring → inline plaintext → missing |
| Instance map | `{"default_instance": {"name": "standard"}, "standard": {...}}`; bare string tolerated for legacy |
| Redaction grouping | `ConfigGroups.SERVICE_KEYS`, masked per `SensitiveKeys.DEFAULT` |

---

## 4. Key decisions & rationale

| # | Decision | Rationale | Alternative rejected |
|---|---|---|---|
| D1 | Not a `BaseManager` | Must exist before the manager tree; `BaseManager.__init__` needs a config | Make it a manager |
| D2 | Secrets identified by **key name**, anywhere in the tree | A new nested `api_key` is protected automatically, with no registration step | Explicit path list |
| D3 | Precedence env → keyring → inline | Env for containers/CI, keyring for interactive hosts, inline as legacy escape | Keyring-only |
| D4 | Blank on disk **only if** `stored or env_present` | G3. Blanking an unstored secret destroys it with no recovery | Always blank |
| D5 | Atomic save via `mkstemp` + `os.replace`, mode `0600` | G6 + owner-only | In-place write |
| D6 | `register_secrets()` with the logger at load | Central scrubbing; no call site can forget to redact | Per-site redaction |
| D7 | `SecretBootstrap` wrapped in try/except | G5 — a keyring failure must not block a headless run | Let it raise |
| D8 | Flat `get()`, no dotted paths | Nested access stays explicit and greppable | Dotted resolver |
| D9 | Legacy bare-string `default_instance` tolerated | Old configs keep working; passing the `{"name": …}` marker straight to `.get()` would raise `TypeError: unhashable type: 'dict'` | Hard migration |

---

## 5. Invariants

| # | Invariant |
|---|---|
| I1 | A secret is blanked on disk only when safely stored elsewhere. |
| I2 | `config.json` is written atomically, mode `0600`. |
| I3 | A resolved secret never appears in log output. |
| I4 | `SecretBootstrap` failure is never fatal. |
| I5 | `get()` is a flat top-level lookup. |
| I6 | `set()` persists; `set_bulk()` does not. |
| I7 | The config layer performs no FETCH, no CACHE, no APPLY. |
| I8 | `get_default_*_instance()` falls back to the first real instance, returning `{}` only when none exist. |

---

## 6. Failure modes & degradation

| Failure | Detection | Behaviour | Blast radius |
|---|---|---|---|
| No keyring backend | `SecretStore` yields nothing | Falls back to env, then inline; secret stays on disk (I1) | Secret remains in `config.json` |
| Secret missing entirely | `SecretBootstrap.audit` | Reported `missing`; interactive TTY → wizard | Service unauthorized |
| `SecretBootstrap` raises | try/except | `log_warning("[SecretBootstrap] skipped: …")` | None |
| Malformed `config.json` | JSON parse error | Raises at construction — correct, config is not optional | Whole run |
| `default_instance` points at an unknown name | Resolver lookup | Falls back to the first real instance | Wrong instance, silently |
| **`set_bulk()` without `save()`** | **None** | Changes live in memory, lost at exit | Silent config loss |
| Typo'd config key | **None** | `get()` returns the default; misconfiguration surfaces phases later | Subtle |
| Crash mid-save | — | `os.replace` is atomic — old file intact | None (I2) |

**Rows 6 and 7 are the live hazards.** `set_bulk()` silently discarding changes
is a genuine footgun, and unvalidated keys mean a typo behaves exactly like an
intentional default. Both are addressed in §9.

---

## 7. Configuration surface

This package *is* the configuration surface. Keys it handles structurally:

| Key | Type | Effect |
|---|---|---|
| `dry_run` | bool | Read by `Main`, propagated to every manager |
| `sonarr_instances` | dict | Instance map + `default_instance` pointer |
| `radarr_instances` | dict | Same for Radarr |
| `movieRootFolders` | list | 🔴 Currently `[]` — see §9 P3 |
| any `*api_key*`, `*token*`, `*secret*`, `*password*` | secret | Overlaid at load, stripped at save |
| `RECOMMENDARR_*` | env | Highest-precedence secret source |

---

## 8. Implemented capabilities

- ✅ Load with position-independent secret overlay (env → keyring → inline)
- ✅ Atomic `0600` save with secrets stripped
- ✅ Loss-proof blanking guard (`stored or env_present`)
- ✅ Central secret scrubbing via `LoggerManager.register_secrets()`
- ✅ `SecretBootstrap` audit + interactive first-run wizard, headless-safe
- ✅ Inline-plaintext detection with migration guidance
- ✅ Instance resolution with legacy-format tolerance and first-instance fallback
- ✅ Redacted config logging grouped by service
- ✅ `reload()`, `set()`, `set_bulk()`, `save()`, `raw_data`
- ✅ CLI entry ([`config_cli.py`](./config_cli.py))

## 9. Planned additions

| # | Addition | Value | Effort | Depends on |
|---|---|---|---|---|
| P1 | **Wire [`validator.py`](./validator.py) into the load path** with a declared schema | Turns a typo'd key from a silent default into a startup error (§6 row 7) | M | Schema definition |
| P2 | **Warn or auto-save on `set_bulk()` without `save()`** | Closes the silent-config-loss footgun (§6 row 6) | S | — |
| P3 | **🔴 Resolve `movieRootFolders`** — currently `[]`, so `classify_movie` bucket assignments are computed and then discarded; every movie lands in `/data/media/movies/standard` | Movie classification is effectively inert today | S | Schema decision |
| P4 | **Dotted-path `get()`** as an additive helper | Nested reads are verbose today | S | — |
| P5 | **Config hot-reload** — watch the file, re-propagate via `RegistryConfigSync` | Edit without restart; prerequisite for the web editor | M | [`web/`](../web/DESIGN.md) |
| P6 | **Secret rotation helper** — re-prompt and re-store one secret | Currently needs the full onboarding wizard | S | [`onboarding/`](../onboarding/README.md) |
| P7 | **Config diff/history** — snapshot on each save | "What changed since the run that worked?" | S | — |
| P8 | **Typed accessors** (`get_int`, `get_bool`, `get_path`) with coercion | Catches string-vs-int at the read site | S | P1 |
| P9 | **Schema export for the web layer** so forms generate themselves | G3 of the web design | M | P1, [`web/`](../web/DESIGN.md) |
| P10 | **Unused-key report** at startup — keys in the file that nothing reads | Surfaces dead config accumulated over time | S | P1 |
| P11 | **Per-instance secret namespacing** so two Radarrs can't collide in the keyring | Latent multi-instance hazard | S | — |

## 10. Open questions

| # | Question | Blocking |
|---|---|---|
| Q1 | Should an unknown config key be fatal, or warn? Warn is safer given accumulated history. | P1 |
| Q2 | Should `set_bulk()` auto-save, or keep the explicit two-step and just warn? | P2 |
| Q3 | Should `movieRootFolders` be derived from classification, or explicitly configured per bucket? | P3 |
| Q4 | Where does config-diff history live — git, Parquet, or a rotating JSON? | P7 |

## 11. Related designs

- [`DESIGN_secrets_backend.md`](./DESIGN_secrets_backend.md)
- [`onboarding/README.md`](../onboarding/README.md) — runs before this package
- [`hooks/DESIGN.md`](../../../hooks/DESIGN.md) — `config.json` is on the commit blocklist
- [`web/DESIGN.md`](../web/DESIGN.md) — the config editor

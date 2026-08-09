# backup

> Breadcrumb: [glidearr](../../../..) › [scripts](../../../README.md) › [managers](../../README.md) › [services](../README.md) › **backup**

**Manager** — `ServiceBackupManager`
**Run position** — Once at the **top of `Main.run`**, real runs only.
**One-liner** — Takes and *validates* a native Radarr/Sonarr backup before anything destructive happens, and degrades the entire run to dry-run if it cannot.

---

## Purpose

From [`__init__.py`](./__init__.py):

> Before a REAL run (`dry_run=false`) makes any destructive change, this triggers
> each Radarr/Sonarr instance's **NATIVE `Backup` command** (the *arr-blessed,
> restorable DB+config zip), waits for it to finish, downloads the freshest one,
> and **VALIDATES it is loadable** — a valid zip whose CRCs check out and which
> contains the service DB + `config.xml`.
>
> The result arms or **DISARMS** the run-scoped backup gate. On failure **the run
> DEGRADES TO DRY-RUN** … so **nothing is ever deleted/re-grabbed without a
> validated rollback point sitting on disk**.

---

## Script inventory

| Script | Size | Role | Tests |
|---|---|---|---|
| [`__init__.py`](./__init__.py) | 14.3 KB | `ServiceBackupManager` — create, validate, arm/disarm | ✅ 7.2 KB |

---

## The gate

`system/backup_gate` in the shared cache. Destructive primitives read it through
`support/utilities/backup_gate.effective_dry_run`.

| Outcome | Gate | `reason` |
|---|---|---|
| Backup created + validated | **armed** | `"ok"` |
| Already-fresh backup reused + validated | **armed** | `"ok"` |
| `dry_run` — nothing destructive to guard | **armed** | `"dry_run"` |
| `backup_before_destructive: false` | **armed** | `"disabled"` |
| **Any instance failed or was unloadable** | **DISARMED** | `"backup_failed"` |

Armed = destructive writes permitted. Disarmed = every delete and re-grab logs
*"would …"* instead.

---

## Validation is three-layered

Not an existence check:

| Layer | Check |
|---|---|
| Size | `MIN_BACKUP_BYTES = 64 KB` — *"below this a 'backup' is empty/garbage, not a real DB dump"* |
| Integrity | Valid zip, **CRCs check out** |
| Content | Contains a `.db` **and** `config.xml` |

---

## Freshness reuse

```python
DEFAULT_MAX_AGE_HOURS = 24.0
```

> reuse a recent, valid backup instead of dumping a new ~300 MB one every run — a
> library barely changes between short scheduled runs (e.g. every 3h)… Picks the
> newest backup of **ANY** kind (our manual **OR** the *arr's own scheduled
> backup), so churn drops further.

A reused backup that fails validation falls through and a fresh one is created.

---

## Deliberately dependency-light

> Talks to the *arr REST API **DIRECTLY** (config `base_url` + `api` key) rather
> than through the validated api stack, so it is **dependency-light and can be
> exercised standalone**.

The safety net does not depend on the manager graph being healthy — which is the
point, since it runs before everything else and guards against everything else.

---

## Navigation

- **Design:** [`DESIGN.md`](./DESIGN.md)
- **Gate reader:** `support/utilities/backup_gate.effective_dry_run`
- **Consumers:** [`coordinator/`](../coordinator/README.md) · Radarr/Sonarr delete and re-grab paths

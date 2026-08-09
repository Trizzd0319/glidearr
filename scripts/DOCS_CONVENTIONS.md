# Glidearr Documentation Conventions

**Status** — Normative. Every `README.md` and `DESIGN.md` in this repo follows this spec.
**Scope** — `scripts/**`
**Owner** — Robert

---

## 1. The three document types

Glidearr uses exactly three kinds of Markdown doc. Do not invent a fourth.

| Type | Filename | Lives at | Answers |
|---|---|---|---|
| **Module doc** | `<module>.md` | Beside `<module>.py` | *What does this one file do?* |
| **Folder README** | `README.md` | Every package folder | *What is in here, and where do I go next?* |
| **Folder DESIGN** | `DESIGN.md` | Every package folder | *Why is it shaped this way, and what's next?* |

A folder that contains Python gets **both** `README.md` and `DESIGN.md`.
A Python file gets a `<module>.md` when it carries non-obvious behaviour (nearly all of them do).
`test_*.py` files never get their own `.md` — they are inventoried in the folder `README.md` under **Test coverage**.

---

## 2. Naming rules

- Folder docs are exactly `README.md` and `DESIGN.md` — uppercase, no suffix.
- Module docs are `<module_stem>.md`, lowercase, matching the `.py` stem exactly
  (`quality.py` → `quality.md`).
- Cross-cutting designs that span folders keep the `DESIGN_<topic>.md` prefix
  (e.g. `DESIGN_secrets_backend.md`). These are linked from the owning folder's
  `DESIGN.md` under **Related designs**.
- No spaces, no dates, no version numbers in filenames.

---

## 3. Linking rules

**Every doc links to the code it describes.** Links are relative POSIX paths so they
resolve in GitHub, PyCharm, and any Markdown viewer.

```markdown
[`scorer.py`](./scorer.py)                  <- sibling code
[`scorer.md`](./scorer.md)                  <- sibling module doc
[`DESIGN.md`](./DESIGN.md)                  <- sibling folder doc
[`../README.md`](../README.md)              <- parent folder
[`../../services/radarr/README.md`](../../services/radarr/README.md)  <- cross-tree
```

Rules:
1. The **first mention** of any file in a doc must be a link.
2. Every folder `README.md` opens with a **breadcrumb** back to the repo root.
3. Every folder `README.md` closes with **Navigation** (parent + children).
4. Never link to a path that does not exist. If it is planned, mark it
   `` `web/` *(planned — see [DESIGN.md](./DESIGN.md))* `` without a link.

---

## 4. Standard section order

### 4.1 Module doc (`<module>.md`)

This is the existing house format — preserved verbatim. Do not reorder.

```markdown
# <ClassOrModuleName>

**File** — `scripts/path/to/module.py`
**One-liner** — <one sentence, no line break>

## What it does (for a senior Python engineer)
### Responsibilities
### Key public methods
### Internal helpers
### Where it sits in the manager tree
### FETCH / CACHE / APPLY
### External API endpoints touched
### Config keys read
### global_cache keys read / written
### dry_run behavior
### Singleton / concurrency / threading notes

## How it functions
### Lifecycle
### Control flow
### Brain delegation

## Criteria & examples

## In plain English

## Interactions
```

Not every subsection applies to every module. Omit a subsection rather than
writing "N/A" — except **FETCH / CACHE / APPLY**, **dry_run behavior**, and
**Brain delegation**, which are *always* present because their absence is itself
meaningful information (and a frequent source of bugs).

### 4.2 Folder README (`README.md`)

**Two legitimate shapes.** Which one applies depends on whether the folder is a
package that exports a class from its `__init__.py`.

#### Shape A — Package-with-a-class (e.g. `registry/`, `config/`, `cache/`)

Where `__init__.py` exports the package's headline class, the `README.md` **is**
that class's module doc and uses the §4.1 module-doc format verbatim, with the
inventory and navigation sections appended:

```markdown
# <ClassName>

**File** — `scripts/path/to/pkg/__init__.py`
**One-liner** — ...

## What it does (for a senior Python engineer)
... (§4.1 sections) ...
## Interactions

---

## Script inventory      ← appended
## Test coverage         ← appended
## Navigation            ← appended
```

Several of these already exist and are high quality. **Do not rewrite them.**
Append the missing inventory/navigation sections and leave the analysis intact.

#### Shape B — Plain folder (e.g. `hooks/`, `tools/`, `steps/`)

```markdown
# <Folder Name>

> Breadcrumb: [glidearr](/) › [scripts](../..) › ... › **<folder>**

**Package** — `scripts.path.to.folder`
**Run position** — <where this executes in the Main lifecycle, or "on demand">
**One-liner** — <one sentence>

## Purpose

## Script inventory
| Script | Doc | Role | Status |
|---|---|---|---|

## Subpackages
| Folder | Role |
|---|---|

## Entry points
<the functions/classes an outside caller is allowed to touch>

## Data in / data out
| Direction | Source/Sink | Payload |
|---|---|---|

## Test coverage
| Test | Covers |
|---|---|

## Navigation
```

### 4.3 Folder DESIGN (`DESIGN.md`)

```markdown
# <Folder Name> — Design

> Breadcrumb: [glidearr](/) › [scripts](../..) › ... › **<folder>**

**Package** — `scripts.path.to.folder`
**Status** — Implemented | Partial | Planned
**Related** — [README.md](./README.md)

## 1. Problem statement

## 2. Design goals & non-goals

## 3. Architecture
### 3.1 Component map
### 3.2 Control flow
### 3.3 Data contracts

## 4. Key decisions & rationale
| # | Decision | Rationale | Alternative rejected |
|---|---|---|---|

## 5. Invariants
<the things that must never break; each one testable>

## 6. Failure modes & degradation
| Failure | Detection | Behaviour | Blast radius |
|---|---|---|---|

## 7. Configuration surface
| Key | Type | Default | Effect |
|---|---|---|---|

## 8. Implemented capabilities
<checklist of what is actually shipped today>

## 9. Planned additions
| # | Addition | Value | Effort | Depends on |
|---|---|---|---|---|

## 10. Open questions

## 11. Related designs
```

---

## 5. Status vocabulary

Use exactly these tokens. They are machine-greppable.

| Token | Meaning |
|---|---|
| `✅ Implemented` | Shipped, tested, in the run path |
| `🟡 Partial` | Works but has a known gap, documented in §9 |
| `🔵 Planned` | Designed, not built |
| `⚪ Speculative` | Idea captured, not yet designed |
| `🔴 Deprecated` | Still present, scheduled for removal |
| `🧪 Tooling` | Operator script, not in the automated run path |

---

## 6. The FETCH / CACHE / APPLY taxonomy

Every module is classified against three verbs. This is the single most useful
line in any Glidearr doc because it predicts blast radius.

| Verb | Means | Risk |
|---|---|---|
| **FETCH** | Reads from an external API (Radarr/Sonarr/Plex/Tautulli/Trakt/MAL/MDBList) | Rate limits, latency |
| **CACHE** | Reads or writes `global_cache` / Parquet / on-disk snapshots | Staleness, corruption |
| **APPLY** | Mutates external state — adds, deletes, tags, moves, upgrades | **Destructive** |

State them explicitly, e.g.:

> **FETCH** — yes, `GET /api/v3/movie` per instance.
> **CACHE** — writes `radarr/movies/<instance>`; reads `tautulli/watch_history`.
> **APPLY** — yes, `DELETE /api/v3/moviefile/{id}`. Gated on `dry_run`.

A module that APPLYs **must** document its `dry_run` behaviour explicitly.

---

## 7. Domain invariants to respect in docs

These are project-wide truths. Docs must not contradict them.

| Invariant | Detail |
|---|---|
| **Quality floor** | HD-720p is the minimum. SD is never a valid target. (SD is absorbed into 720p.) |
| **4K eligibility** | ⚠️ **DISPUTED — do not repeat until resolved.** This was recorded as "score ≥ 70 on the 100-point scale." That matches neither live threshold: the watchability ladder's 4K entry rung is **38** (p99.5), and acquisition is gated by `watch_likelihood.uhd_cutoff` (**75**) and `routing.movies.4k_dual_min_score` (**75**), which `SCORING_GROUPS.md` states *"live on a different scale."* See [`scoring/DESIGN.md`](./managers/machine_learning/scoring/DESIGN.md) §3.3, `GLD-SCO-01`, decision D22. |
| **Unknown score** | `score is None` ≠ eligible. Unknown defaults to the **safe mid-tier** profile (HD Bluray + WEB), never the highest. |
| **`keep-universe` tag** | Never deleted. Quality-change only. |
| **bare `universe` tag** | Deletable as a last resort. |
| **Cursor persistence** | Save after each pool operation, inside `finally`. Never only at cycle completion. |
| **Logging** | One summary line per manager: `[ManagerName] ✅ N/N: comp1✅ comp2✅`. All per-component detail at `log_debug`. |

> **Lesson recorded.** The 4K figure above was asserted across six `DESIGN.md`
> files during the documentation sweep before anyone checked it against the code.
> A convention note is a *claim*, not a source. Verify a numeric invariant
> against the implementation before repeating it — see
> [`ENHANCEMENTS.md`](./ENHANCEMENTS.md) §8 **P-G**.

---

## 8. Writing style

- Lead with the mechanism, not the motivation.
- Prefer a table over a paragraph when there are ≥3 parallel items.
- Name real files, real config keys, real cache keys. No placeholders.
- Every threshold gets a number and a worked example.
- The **In plain English** section is written for a non-engineer household member;
  it uses a concrete title from the library as an example.
- Never document aspirational behaviour as current. Use §9 **Planned additions**.

---

## 9. Doc maintenance

- Changing a module's public surface obliges you to update its `.md` in the same commit.
- Adding a `.py` obliges you to add its row to the folder `README.md` inventory.
- Adding a folder obliges you to create both `README.md` and `DESIGN.md`.
- [`support/tools/mirror_docs.py`](./support/tools/mirror_docs.py) can be extended to
  assert doc/code parity in CI — see the docs-lint entry in the root
  [`DESIGN.md`](./DESIGN.md) §9.

---

## 10. Enhancement registration — normative

Enhancements are identified **in place** and indexed **centrally**. Both, always.

### The rule

Every row in a `DESIGN.md` §9 *Planned additions* table **must** carry a global
ID and **must** have a matching row in [`ENHANCEMENTS.md`](./ENHANCEMENTS.md).

```markdown
## 9. Planned additions

| ID | Addition | Value | Effort | Depends on |
|---|---|---|---|---|
| GLD-CACHE-01 | Thread-safe MemoryManager | Closes the prefetch/main-thread race | S | — |
```

### Why both

| Location | Answers |
|---|---|
| `DESIGN.md` §9 | *While I'm reading this module, what's outstanding here?* |
| `ENHANCEMENTS.md` | *Across the whole repo, what should I do next?* |

A reader of one folder needs local context. A person planning work needs the
whole backlog. Neither view substitutes for the other — and a §9 table that
exists only locally is effectively invisible, which is exactly the failure this
section exists to prevent.

### ID format

`GLD-<AREA>-<nn>` — area prefix from the table in
[`ENHANCEMENTS.md`](./ENHANCEMENTS.md) §1, zero-padded two-digit sequence.

- IDs are **stable**. Never renumber, even when an item is dropped.
- A new area needs a new prefix registered in `ENHANCEMENTS.md` §1 first.
- Duplicates across areas are fine and expected — mark the secondary row as a
  dup and point at the canonical ID.

### Status vocabulary

`🔵 Open` · `🟢 Doing` · `✅ Done` · `⏸ Tabled` · `❌ Dropped`

A confirmed defect — something that is wrong now, not merely improvable — is
prefixed `🔴` and also listed in `ENHANCEMENTS.md` §4.1.

### Blocking decisions

When an item is blocked on a decision rather than on effort, record the question
in the folder's `DESIGN.md` §10 *and* in `ENHANCEMENTS.md` §5, with the IDs it
blocks. Effort estimates on a decision-blocked item are meaningless until the
decision lands.

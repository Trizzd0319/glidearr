# Plex playlist-ranker evaluation

**A data-collection instrument for an open research question: how should a personalized Plex
"Up Next" playlist actually be ranked — and what signals make it better?**

An automatic "Up Next" generator builds each profile's list by scoring every owned title and
sorting. That score blends a few signals — how well a title's **genres** match *your* taste, the
title's overall **quality/popularity**, and whether you're **actively watching** it — then weights
them and adds them up. Today those weights (and the genre-matching formula itself) are essentially
**guesswork**, tuned at best on one person's library. This kit exists to replace that guesswork
with evidence.

What makes this a genuine research problem: **there is no labelled "correct" playlist.** You can't
directly optimize "a good Up Next list", so we use a measurable proxy — a **temporal holdout**
(§4): hide your most-recent watches, rebuild the ranker from only the older ones, and measure how
highly it ranked those hidden watches among everything in your library. That converts a fuzzy
design choice into a number we can compare across weights, modes, and signals.

Run on a single library, that sweep already showed the answer is **not a constant** — the best
weighting depends on a profile's *taste breadth*, on *how good the quality signal is*, and even on
*which success metric you pick* (§5). So rather than ship a guessed default, the goal is two-fold:
**(1)** learn the right way to combine the signals we already have across many real, diverse
households, and **(2)** discover which *additional* signals (recency, continuation, popularity
reliability, …) are worth **generating** to make the playlists measurably better (§8). This script
is how you contribute one household's worth of evidence to that dataset — **with nothing
identifying ever leaving your machine.**

- **One file, no dependencies:** [`playlist_eval_report.py`](playlist_eval_report.py) is pure
  Python standard library — nothing to `pip install`.
- **Pulls data live from Plex + Tautulli**, so *any* Plex + Tautulli user can contribute.
- **Privacy-first:** the report contains hashed user ids, genre-name statistics, and a metrics
  matrix — **no titles, usernames, ids, or timestamps**. Credentials never leave your machine.

> **TL;DR contributor flow:**
> `python playlist_eval_report.py` → answer four prompts (Tautulli + Plex URL/key) →
> review `playlist_eval_report.json` → share it. That's it.

---

## 1. The ranking model

For each Plex profile, every candidate title gets one score and the list is sorted by it:

```
priority_score = affinity_weight · A   +   jit_weight · J   +   household_weight · h
```

| term | symbol | range | meaning |
|------|--------|-------|---------|
| **affinity** | `A` | 0–1 | how well the title's **genres** match THIS profile's taste (`genre_match`, §2) |
| **JIT** | `J` | 0/1 | 1 if the profile is **actively watching** this title (a "continue watching" boost) |
| **household** | `h` | 0–1 | a per-title **quality/popularity** score, normalised to its library's max |

The default weights put a heavy thumb on personal genre taste — `affinity_weight = 0.9` vs
`household_weight = 0.1`, a **9 : 1** dominance. Whether that's a good idea is exactly what this
study measures (§5 — early answer: probably not).

`h` is a per-title quality signal: here it's the **Plex `audienceRating`** for the title,
normalised to 0–1. (The report notes this under `household_source` so results from different
quality signals are never blindly pooled.)

---

## 2. `genre_match` — the affinity term `A`

`A ∈ [0,1]` measures how on-taste a title's genres are. Affinity is `{genre: weight}` = how many
times the profile watched each genre; each of a title's genres contributes a normalised weight
`w_g = affinity[g] / max_weight` (`0` for a genre the profile never watches).

The catch: **genre tagging is wildly uneven** — an animated kids show may carry 10+ tags
(`Animation, Children, Comedy, Family, Adventure, Fantasy, …`) while a live-action drama carries 1–2.
So "how on-taste is this title" depends on *how you aggregate* across its genres. Four modes:

| `mode` | formula | character |
|--------|---------|-----------|
| **`precision`** (default) | mean of `w_g` over **all** the title's genres | "what fraction of the SHOW is on-taste" — an extra off-taste tag **dilutes** it; penalises many-tag shows |
| **`soft`** | `Σ w_g / (n_match + soft_lambda·n_zero)` | like precision, but a zero-affinity genre counts only `soft_lambda` (0.5) — off-taste tags hurt **less** |
| **`coverage`** | `Σ_{g∈show∩user} affinity[g] / Σ_{all user genres} affinity[g]` (+ a `1e-3·precision` tiebreak) | affinity-weighted **recall** — "how much of YOUR taste the show covers"; ignores off-taste tags; rewards many-tag shows |
| **`blend`** | `blend_weight·coverage + (1−blend_weight)·precision` (0.75) | middle ground; the two opposite tag-count biases partly cancel |

### Worked example

Profile affinity `{Animation, Comedy, Family, Adventure}` (all weight 1); the show **Bluey** is
tagged `[Animation, Children, Comedy, Family]`:

- **precision** `= (1+0+1+1)/4 = 0.75` — the single off-taste tag **`Children`** drags Bluey *below*
  a show tagged `[Animation, Comedy, Family]` (`3/3 = 1.0`), despite matching the same liked genres.
- **coverage** `= 3 covered / 4 user genres = 0.75` — and `[Animation, Comedy, Family]` is *also*
  `0.75`, so they **tie**; a show that *also* covers `Adventure` scores higher. The `Children` tag is
  ignored.

`precision` punishes breadth, `coverage` rewards it. Which is "right" is empirical — hence §4.

---

## 3. The knobs

| parameter | default | effect |
|-----------|---------|--------|
| `affinity_weight` | `0.9` | weight of the genre term `A` — **the single biggest lever** (§5) |
| `household_weight` | `0.1` | weight of the quality term `h` |
| `jit_weight` | `0.65` | weight of the active-watching boost `J` |
| `genre_match_mode` | `precision` | `precision` \| `soft` \| `coverage` \| `blend` |
| `soft_lambda` | `0.5` | off-taste-genre denominator weight in `soft` |
| `blend_weight` | `0.75` | coverage share in `blend` |

In the eval tool these are CLI flags (`--aff-w`, `--hh-w`, `--blend-weight`, …) plus a sweep grid at
the top of the script.

---

## 4. How the evaluation works — temporal holdout

There's no labelled "correct" ranking, so we grade against **what people actually watched next**:

1. Pick a cutoff in time and **hide** every watch after it.
2. Build the profile's genre affinity from **only the watches before** the cutoff.
3. Score **every** candidate title in the library under each config.
4. **Reveal** the hidden (post-cutoff) watches — a good config ranked those **high**.

It must be **temporal**, not random: random hiding leaks "future" watches into the affinity and
flatters every config. Hiding by time mirrors real use (on Tuesday you only know up to Monday).

**Metrics** (higher = better; *random ≈ 0.500*):

- **meanPct** — mean tie-fair percentile of the held-out watches across all candidates (1.0 = top).
- **recall@K** — share of held-out watches ranked in the top K.
- **MRR** — mean reciprocal rank.
- **household baseline** — rank by `h` **alone** (genre term off); the gap to a config = the value the
  personal genre tilt actually adds.

**One honest caveat baked in:** holdout rewards predicting what you *did* watch, and popular titles
get watched a lot, so `h` has a built-in edge. That's why "pure household" isn't the goal — it gives
every profile the **same** list. Personalisation is what differentiates profiles; the question is
*how much* of it helps.

---

## 5. Preliminary findings (small sample — this is why your data matters)

Below is a **real anonymized run from this tool** — one household, 2 high-power profiles (a
broad-taste adult and a kids/animation profile), 25% temporal holdout, TV + movies, household
signal = Plex `audienceRating`. Watches for titles no longer in the library are excluded. Mean over
the high-power profiles, averaged across the four `genre_match` modes:

| affinity_weight | meanPct | recall@50 | MRR |
|---|---|---|---|
| household-only (baseline) | 0.663 | 0.013 | 0.003 |
| 0.1 | 0.696 | 0.090 | 0.005 |
| **0.3** | **0.732** | 0.117 | 0.011 |
| 0.5 | 0.714 | 0.118 | 0.012 |
| 0.7 | 0.689 | 0.118 | 0.012 |
| **0.9 (current default)** | 0.676 | **0.121** | **0.016** |

1. **Personalization clearly helps** — *every* affinity weight beats the pure-quality (household-only)
   baseline on every metric. With a weak baseline signal (a single critic rating), the genre tilt adds
   real value.
2. **The best weight depends on the metric.** Overall ordering (`meanPct`) peaks at **≈ 0.3**; but the
   top-of-list metrics (`recall@50`, `MRR`) keep drifting *up* toward 0.9. So ~0.3 is best if you care
   about whole-list quality, higher if you only care about nailing the very top few.
3. **The `genre_match` mode barely matters** — the four modes land within ~0.01 of each other at every
   weight (`aff0.3|blend` 0.7359 ≈ `aff0.3|coverage` 0.7357). It's the **weight**, not the flavour.
4. **Absolute numbers track the household signal.** An internal run using a much richer quality score
   (ratings + recency + popularity) scored far higher overall (baseline ~0.90) and made ~0.3 the clear
   winner on *all* metrics — i.e. **the better your quality signal, the less personalization you need.**

### How the weights interact — two real profiles, opposite answers

The aggregate hides the most important effect. `hh_w = 1 − aff_w`, so dialing affinity up dials
household down. Here is each profile swept across the weight (precision mode; `meanPct` / `recall@50`):

| `aff_w` (`hh_w`) | broad-taste adult — 16 genres | kids / animation — Animation-led |
|---|---|---|
| household-only | 0.683 / 0.03 | 0.643 / 0.00 |
| 0.1 (0.9) | 0.705 / 0.03 | 0.692 / 0.18 |
| **0.3 (0.7)** | 0.706 / 0.00 | **0.750 / 0.24** |
| 0.5 (0.5) | 0.666 / 0.00 | 0.739 / 0.24 |
| 0.7 (0.3) | 0.625 / 0.00 | 0.730 / 0.24 |
| 0.9 (0.1) | 0.587 / 0.00 | 0.750 / 0.24 |

- **Broad-taste adult:** a heavy genre tilt *actively hurts* — `meanPct` falls 0.71 → 0.59 as `aff_w`
  rises, and their actually-watched shows get *pushed out* of the top 50 (`recall@50` 0.03 → 0.00).
  This watch-everything profile wants **more household weight** (`aff_w ≈ 0.1`).
- **Kids/animation:** the opposite — pure quality finds **none** of their next-watches in the top 50
  (`recall@50` 0.00), but even a light tilt lifts that to 0.18, and a strong tilt holds 0.24. A narrow,
  dominant taste wants **more affinity weight** (`aff_w ≈ 0.3–0.9`).

**The two profiles pull in opposite directions**, so the aggregate "sweet spot" (~0.3) is really a
*compromise* — and the right default may need to adapt to taste breadth (and to how good your quality
signal is). Two profiles can't settle that; a community dataset can.

---

## 6. Run it / contribute

Pulls your library (genres + ratings) from **Plex** and your watch history from **Tautulli** over
HTTP. **Standard library only** — nothing to `pip install`.

```bash
python playlist_eval_report.py
#   prompts:  Tautulli URL [http://localhost:8181] / API key
#             Plex URL     [http://localhost:32400] / token
#   non-interactive instead of prompts:
#     --tautulli-url --tautulli-apikey --plex-url --plex-token
#     or env: TAUTULLI_URL TAUTULLI_APIKEY PLEX_URL PLEX_TOKEN
```

- **Tautulli API key:** *Settings → Web Interface → API*.
- **Plex token:** open any item → *Get Info → View XML* and copy `X-Plex-Token` from the URL
  (Plex support: "Finding an authentication token / X-Plex-Token").
- **Your credentials are used only to fetch your own data and are never written to the report.**

It walks **every** user, runs the holdout sweep (affinity weights × modes), and writes
`playlist_eval_report.json` (**share this**) + a readable `playlist_eval_report.md` summary to the
current directory.

### Drill into one profile

```bash
python playlist_eval_report.py --user <user_id> --cutoff-days 90 --aff-w 0.3 --hh-w 0.7
```

### Tune the sweep

Edit the constants at the top of the script, or pass `--holdout-frac 0.3`, `--jit-days 14`,
`--blend-weight 0.85`, `--out my_report.json`.

```python
AFFINITY_WEIGHTS = [0.1, 0.3, 0.5, 0.7, 0.9]   # household weight = 1 - affinity weight
HOLDOUT_FRAC     = 0.25                          # hide the most-recent 25% of watches
MIN_POSITIVES    = 5                             # users below this are flagged low_power
```

---

## 7. Privacy — what the report does and does **not** contain

| ✅ included (aggregate / non-identifying) | ❌ never included |
|---|---|
| `sha256(user_id)[:12]` — a hash of a **number**, not a name | usernames, emails, server/host, IPs |
| watch **counts** (pre/post/positives), library **size**, affinity **breadth** | watched **titles**, ratingKeys, tmdb/imdb ids |
| affinity **genre distribution** (generic names like `Action`, `Drama`) | absolute **timestamps**, dates, file paths |
| the **metrics matrix** (meanPct / recall@K / MRR per weight × mode) | API keys / Plex tokens / anything tied to a person or item |

The JSON is small and human-readable — **open it and check** before sharing. Its exclusions are also
listed in its own `_privacy` field.

---

## 8. What this research is trying to answer

Every report feeds one goal: **replace the hand-guessed playlist ranker with one designed from
evidence** — by tuning the signals it already uses *and* by discovering new signals worth generating.
A single household can't settle any of these (its profiles even contradict each other, §5); pooled
across many households, each anonymized report turns an opinion below into a measurement.

**Tuning the ranker we already have**

1. **Is there a single best `affinity_weight`, or must it adapt?** Our two profiles wanted *opposite*
   weights. The first thing the dataset settles is whether a constant default exists at all — or
   whether the weight has to depend on the profile.
2. **Does any `genre_match` mode actually win?** So far they're within noise. If that holds across
   libraries we keep the simplest and stop tuning it; if one mode wins for a particular *kind* of
   profile, that itself is a finding.

**Learning a *personalized* weighting (the main prize)**

3. **Can we predict the right weight from a profile's *shape*?** The report deliberately ships the
   features such a model would need — affinity **breadth**, the genre **distribution**, watch
   **counts**, **library size**, `audienceRating` **coverage**. If a rule like "narrow taste → heavier
   genre tilt, broad taste → lighter" holds at scale, the generator can **set each profile's weights
   automatically** instead of using one global number — the difference between a one-size guess and an
   adaptive ranker.

**Finding *additional* signals to generate better playlists**

4. **What is the best "quality" signal `h`?** Here it's Plex `audienceRating` (weak); a richer score
   (ratings + recency + popularity) scored far higher (§5). The data tells us how much a better quality
   feature is worth — i.e. whether it's worth *computing* one.
5. **Does *recency* matter?** Lifetime genre counts treat a show you binged two years ago like last
   week's. A temporal-decay version of affinity is an obvious feature to test against the holdout.
6. **How much does the *active-watching* / continue-watching signal help** — measured, not assumed?
7. **Which *other* derivable signals move the needle?** Franchise/sequence continuation, genre
   *co-occurrence*, rating *reliability* (vote count), household co-watching context, time-of-day. The
   holdout is the referee that decides whether any candidate feature earns a place before it ships.

**And the objective itself**

8. **Which metric should the ranker even optimize?** Overall ordering (`meanPct`) and top-of-list
   quality (`recall@K`, `MRR`) *disagree* in our data (§5) — so deciding what "good" means for an Up
   Next list is itself a question the aggregated data helps answer.

The report `schema` is versioned: as we identify a promising new signal, we add the feature needed to
test it and re-run the *same* study across the contributed dataset. **Evidence first, defaults
second** — that's the whole point of collecting this.

---

## Requirements

- **Python 3.9+.** No third-party packages — the tool uses only the standard library.

## License

No license is set yet — **add one (e.g. MIT) before publishing** if you want others to reuse it.

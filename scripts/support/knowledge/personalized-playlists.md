# Personalized Playlists & Family Libraries — How It Works

*A plain-language guide for the people who run the server **and** the families who use it.*
*If you just want "what will my kid see and is it safe?", jump to [What each profile sees](#what-each-profile-sees) and [Safety & privacy](#safety--privacy).*

---

## Table of contents

1. [The big picture](#the-big-picture)
2. [How a show or movie gets sorted into a library](#how-a-show-or-movie-gets-sorted-into-a-library)
3. [Personal "Up Next" playlists (one per profile)](#personal-up-next-playlists-one-per-profile)
4. [Family "Up Next" collections (age-tiered rows on Home)](#family-up-next-collections-age-tiered-rows-on-home)
5. [What each profile sees](#what-each-profile-sees)
6. [How age restrictions are decided](#how-age-restrictions-are-decided)
7. [Safety & privacy](#safety--privacy)
8. [Turning it on (for the server operator)](#turning-it-on-for-the-server-operator)
9. [Known limits (and why)](#known-limits-and-why)
10. [Glossary](#glossary)

---

## The big picture

This system does two related things, automatically, every time the server runs:

- **It keeps your libraries tidy.** Every show and movie is sorted into the right library — TV, Anime, Documentaries, Reality, Kids, and the matching movie buckets — based on its genre, its age rating, and whether it's anime. (No more crime dramas landing in "Documentaries.")
- **It builds a personal "Up Next" for everyone.** Each Plex profile gets its own playlist of *what to watch next*, ordered for that person. Grown-ups also get age-appropriate **family rows** on the Home screen so a parent can quickly hand a kid something safe.

Two design promises that matter to a family:

- **Nothing is written to Plex unless the operator deliberately turns it on.** Out of the box the system only *previews* what it would do (in the server log). See [Turning it on](#turning-it-on-for-the-server-operator).
- **A child's playlist is created on the child's own account, never the parent's,** and the system can only ever touch playlists *it* created — it will never delete or overwrite a playlist someone made by hand.

---

## How a show or movie gets sorted into a library

Every title is classified into exactly one library "bucket." The decision is made in a fixed order — the **first rule that matches wins** — so the result is predictable.

### TV shows

Order of checks:

1. **Preschool** — a show tagged with the *Preschool* genre is toddler content and always goes to **Kids**.
2. **Anime** — genuine anime (the *anime* genre, or Japanese/Korean/Chinese animation, or a confirmed anime source) goes to the **Anime** library, even if it's also a kids show.
3. **Kids by genre** — a *Children* / *Kids* / *Family* genre routes to **Kids** (the soft *Family* tag only when the rating is kid-safe, so adult "family dramas" don't sneak in).
4. **Kids by network** — a show that airs on a genuine **kids network** (Disney Junior, Nickelodeon, Cartoon Network, PBS Kids, …) goes to **Kids**. *This is why Star Trek: Prodigy (a Nickelodeon kids show) correctly lands in Kids while the grown-up Star Treks do not.*
5. **Reality** — only a show that actually carries a **reality** genre goes to the **Reality** library.
6. **Documentary** — only a show that actually carries a **documentary** genre goes to the **Documentaries** library. Crime, war, history, and sport *dramas* are **not** documentaries and stay in regular TV.
7. **Kids by certificate** — a kid-safe rating (TV-Y/TV-Y7/TV-G) routes leftover shows to **Kids**.
8. Otherwise → the standard **TV** library.

### A note on "Common Sense" age and why Star Trek isn't a kids show

Common Sense Media ratings tell you *the youngest age a title is appropriate for* — **not** whether something is a children's program. Star Trek: Deep Space Nine is rated "appropriate for about age 10," but it's an adult drama, not a kids' show.

So the system treats a Common Sense age as a **ceiling, never a floor**:

- A Common Sense age that's **too old** for kids will *keep a title out* of the Kids library.
- A **young** Common Sense age, on its own, will **not** pull a title *into* Kids. Something only becomes "kids" with a real kids signal — a kids genre, a kid-safe rating, or a kids network.

That's the difference between *"appropriate for a 10-year-old"* and *"made for children,"* and it's why the whole Star Trek franchise stays in regular TV while only the genuine kids spin-off goes to Kids.

### Movies

Movies use a smaller set of buckets — **Kids → Anime → 4K → Standard** — and lean on the **studio** (Pixar, Disney Animation, Nickelodeon Movies, …) plus the Common Sense ceiling, because a movie's genre tags are too noisy to trust for kid-routing.

---

## Personal "Up Next" playlists (one per profile)

Each Plex profile gets a personal **"Up Next"** playlist — a single, mixed list of movies and TV the system thinks that person should watch next.

### What's in it, and what's left out

- **Only things you own** (already in the library) and **haven't watched yet** — your own watch history is used to skip what you've already seen, so the list stays fresh.
- **Age-appropriate for that profile** — a kid's playlist only ever contains content allowed by that profile's parental tier (see [How age restrictions are decided](#how-age-restrictions-are-decided)).

### How it's ordered

The list is ranked by a blend of signals, strongest first:

1. **Your taste** — how well a title's genres match *your* viewing history (your "affinity").
2. **What you're actively watching** — a show you've been keeping up with gets a lift.
3. **What the household watches** — a gentle baseline so popular family content surfaces.

On top of that ranking sits a **"new season of a show you finished" boost**: if you're **caught up** on a show you like (you watched the last episode that was available) and a **new** episode or season has just landed — measured by a blend of *when it aired* and *when it arrived in your library* — that show floats toward the top. It's the "there's finally a new season!" nudge, and it never breaks up a show into spoiler order.

### Cold start: a brand-new kid profile

A young child is usually watched *for* — the parent presses play on their *own* profile. So a new kid profile often has no history of its own, and a plain ranking would just show "whatever the whole house watches most" (which skews adult).

Instead, when a kid profile has no history yet, the system can **seed that child's playlist from the household's engagement with age-appropriate content** — effectively, "the kid shows this family actually watches." So a freshly-created kid profile starts with a sensible, kid-relevant list instead of a generic one. *(This is an opt-in setting, off by default — see [Turning it on](#turning-it-on-for-the-server-operator).)*

> **Honest limit:** this can only *rank* content you already own. If the library genuinely owns *nothing* appropriate for a young tier, no amount of cleverness can fill the list — that's a sign to **acquire** more kid content, not a playlist bug.

### Where you see it

A per-user playlist shows up under **Playlists** in that profile, and in the **"Recent Playlists"** row on that profile's Home screen. **This is the one curated row that reaches a kid's screen** (see the limits section for why collections don't).

---

## Family "Up Next" collections (age-tiered rows on Home)

For the **grown-up / family** view, the system also builds **collections** — the rows you see across the top of the Plex Home screen — so a parent can glance at Home and grab something appropriate for whichever child is asking.

It builds:

- **"Up Next - Household"** — everything, all ratings (the adult view of "what we're into").
- **One collection per restricted tier that exists in your household**, each filtered to that tier and named by the tier:
  - **"Up Next - Little Kids"** — G / TV-Y / TV-G only
  - **"Up Next - Older Kids"** — adds PG / TV-PG
  - **"Up Next - Teens"** — adds PG-13 / TV-14 (no R / TV-MA)

If your household has no restricted profiles at all, only the Household collection is built. If you have a little kid, an older kid, and a teen, you get all four. The set **adjusts automatically** as you add or change profiles.

These rows are **pinned to the top of the Home screen** and **floated to the top of the Collections tab** (so they don't get buried among hundreds of other collections), sitting just below Plex's own *Continue Watching* and *On Deck*.

> **Important:** these collections are a **parental aid on the adult/family Home** — they help a grown-up pick something safe. **Kids do not see collections on their own profiles** (a Plex limitation, explained below). A child's own curated row is their **playlist**.

---

## What each profile sees

| | Grown-up / owner profile | Managed kid / teen profile |
|---|---|---|
| **Their own "Up Next" playlist** | ✅ Yes (Playlists + Recent Playlists on Home) | ✅ Yes (Playlists + Recent Playlists on Home) |
| **The family "Up Next" collection rows on Home** | ✅ Yes — Household + each tier | ❌ **No** — managed profiles don't show promoted collections |
| **Content filtered to their age** | Sees everything | Sees only what their parental tier allows |

So in practice: **kids get a personal playlist**; **parents get both a personal playlist and the age-tiered collection rows** to choose from on the family TV.

---

## How age restrictions are decided

Every profile resolves to one of four tiers: **Little Kid → Older Kid → Teen → Adult**.

The tier comes from **Plex's own parental-controls setting** on each profile (the "restriction profile" you set in Plex Home), with an optional operator override in config. A profile being "managed" is *not* the same as being a kid — only a profile with an actual age restriction set in Plex is treated as restricted.

Content is matched to a tier by its **rating**, with this mapping:

| Rating | Allowed from tier |
|---|---|
| G, TV-Y, TV-G | Little Kid and up |
| PG, TV-Y7, TV-PG | Older Kid and up |
| PG-13, TV-14 | Teen and up |
| R, TV-MA, NC-17, unrated | Adult only |

If a title has **no rating at all**, the system uses the Common Sense age as a fallback, and if even that is unknown it **errs on the side of caution and hides the title from kids** ("if we can't vouch for it, a child doesn't see it").

When several kids of different ages share the household, the single shared kids surfaces use the **strictest** tier present, so the youngest is always safe.

---

## Safety & privacy

This system handles family accounts and children's profiles, so the safety posture is deliberate:

- **Off by default.** With write-back disabled (the default) or while the server is in dry-run mode, the system **previews** what it would do in the log and **writes nothing** to Plex. The operator has to explicitly opt in.
- **Children's playlists are written to the child's own account** using a token scoped to *that* member — **never** the owner/parent token. If the system can't get the right per-user token, it **skips that profile** rather than risk writing to the wrong account.
- **It only ever touches playlists it created.** Each managed playlist is tracked by an internal marker; the system will **never** delete or overwrite a playlist a family member made by hand, even if it has the same name.
- **Tokens are never saved to disk.** Per-user access tokens live in memory for one run only and are scrubbed from all logs. Profile PINs are likewise redacted everywhere they could appear.
- **No surprise deletions.** A profile that's temporarily unavailable (e.g. a PIN wasn't supplied this run) is **left alone** — its playlist is only removed if the profile genuinely no longer exists in your Plex Home.
- **Every change to a child's account is audit-logged** (who, which profile, what playlist, how many items).

---

## Turning it on (for the server operator)

Everything below lives under the `plex.playlists` section of your config, plus a couple of top-level Plex flags. **All of it is off by default.**

**Step 1 — build the data the playlists need** (read-only scans; safe):

- `plex.episodes.enabled = true` — builds the owned-episode → Plex map (TV playlists).
- `plex.movies.enabled = true` — builds the owned-movie → Plex map (movie + combined playlists).

**Step 2 — preview before you write.** Leave `dry_run = true` and set `plex.playlists.writeback.enabled = true`. On the next run the log shows a **disarmed banner** and a per-profile preview of exactly what *would* be created, updated, or deleted — read it and confirm it looks right.

**Step 3 — go live.** Set `dry_run = false` (with `writeback.enabled = true`). Now the system actually creates/updates the playlists. Tip: use `plex.playlists.exclude_users` to leave specific profiles untouched while you build confidence.

**Useful knobs (all optional):**

| Setting | What it does | Default |
|---|---|---|
| `plex.playlists.writeback.enabled` | Actually write playlists to Plex (still needs `dry_run = false`) | `false` |
| `plex.playlists.recency_boost.enabled` | Turn on the "new season of a show you finished" lift | `false` |
| `plex.playlists.cold_start_kids_prior` | Seed a no-history kid profile from the household's kid taste | `false` |
| `plex.playlists.exclude_users` | Profiles (by name) to leave completely untouched | empty |
| `plex.playlists.profile_ages` | Override a profile's age tier (when Plex's own setting isn't right) | empty |
| `plex.playlists.max_items` | Longest a playlist can get | `100` |

The same settings are available headlessly (Docker/unraid) as `RECOMMENDARR_PLEX_PLAYLISTS_*` environment variables.

---

## Known limits (and why)

- **Kids can't see collections on their own profile.** This is a Plex platform limitation — managed/restricted profiles simply don't render promoted collections on their Home screen (or in their Collections browse), regardless of how kid-safe the content is. *That's why the kid-facing surface is a **playlist**, and the tiered collections are a parental aid on the adult Home.*
- **Plex always pins *Continue Watching* and *On Deck* above everything.** Our family rows sit at the top of the *promoted* rows, just below those two — they can't go above Plex's built-ins.
- **An empty playlist is treated as "nothing to surface."** If a profile has genuinely nothing owned/unwatched/age-appropriate, the system removes any stale playlist rather than leaving an empty one — and that's a cue to acquire more content for that tier.
- **Collections are per-library.** A single collection can't mix the Movies and TV libraries, and Plex collections group *shows*, not individual episodes — so the family collections are movie-focused, while the per-user playlists are the place mixed movie+TV lives.

---

## Glossary

- **Profile / managed user** — a person's account inside your Plex Home. "Managed" profiles are the ones the owner controls (typically kids).
- **Restriction profile / parental tier** — Plex's per-profile age setting (Little Kid / Older Kid / Teen), which we read to decide what each profile may see.
- **Affinity** — a profile's genre taste, learned from its viewing history.
- **Watchability** — a household-level score for how worth-watching a title is, learned from what the house watches.
- **Collection** — a curated *row* in a Plex library / on Home (e.g. "Marvel Cinematic Universe"). Visible to adults; not to managed kids.
- **Playlist** — a personal, ordered list on a profile's account. Visible to that profile, including kids.
- **Write-back** — the (opt-in) step where computed playlists are actually written into Plex, as opposed to only previewed.
- **Dry run** — a mode where the system plans and previews but changes nothing.

---

*This document describes the personalized-playlist and family-library system as implemented. For the technical design and the per-component decisions, see the developer design notes alongside the code.*

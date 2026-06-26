# Reducing Plex transcoding — a viewer's guide

Transcoding is when the Plex **server** has to re-encode a file on the fly because your **player**
can't play it as-is. It's the #1 cause of buffering, heat, and "why is the server slow." The good
news: **most transcoding is a player/device setting, not a problem with the files** — so you can fix
the bulk of it yourself in a couple of minutes, no re-downloading required.

This guide is ordered by how much transcoding each fix typically removes.

---

## TL;DR checklist

- ✅ **Use the real Plex app, not a web browser.** Browsers transcode almost all audio.
- ✅ **On a TV/console, pick the standard audio track**, not the "TrueHD/DTS/Atmos" one. Plex remembers your choice.
- ✅ **Set "Remote Quality" to Original/Maximum** if your upload can handle it; otherwise expect remote playback to transcode.
- ✅ **Prefer text (SRT) subtitles**, not image subtitles (PGS/VOBSUB), which force a transcode to burn them in.

---

## 1. Audio — the biggest one (≈40% of transcodes here)

**Why it happens:** your file usually has *several* audio tracks — a high-end one (TrueHD / DTS-HD /
Atmos, 5.1 or 7.1) **and** a standard one (AC3 / EAC3 / AAC). Plex defaults to the *highest-quality*
track. If your device can't play that one, the server transcodes it — even though a track it *could*
direct-play is sitting right there in the same file.

Two device cases cover almost all of it:

### Samsung / LG TVs (Tizen / webOS)
These TVs **cannot pass through DTS or TrueHD** (a licensing limitation), so Plex transcodes those.
But your files already carry an AC3/EAC3 track these TVs play fine. The fix is to tell Plex to use it:

- **During playback:** open the player controls → **Audio** → choose the **AC3 / Dolby Digital / "5.1"
  or "Stereo"** track instead of the TrueHD/DTS one. **Plex remembers this per show/movie and per user.**
- **Make it the default:** Plex app → **Settings → make a habit of the above**, or set
  **Settings → Audio → "Preferred audio language"** and, on the server, **Settings → Player →
  Audio** preferences so the standard track is chosen automatically.
- **Cap the channels (most reliable):** in the TV's Plex app **Settings → (Advanced) → Maximum audio
  channels → Stereo (2.0)** — this stops Plex from grabbing the 7.1 TrueHD track at all on that device.

### Web browsers (Chrome / Edge / Firefox)
The Plex **web player transcodes essentially all multichannel/AC3/EAC3 audio to Opus stereo** — there
is no setting that fully fixes this. **Use the native Plex app** (TV app, mobile app, Plexamp, or a
device like an Apple TV / Shield / Roku) instead of watching in a browser.

> **Why the *arr / glidearr can't fix this:** the compatible audio track already exists in the file.
> This is purely Plex picking the wrong one for the device — a player setting, not something a
> different download would change.

---

## 2. Video bitrate / resolution (≈36%)

**Why it happens:** the file's bitrate is higher than the player is *allowed* to stream — either by a
**quality setting** or by **actual network bandwidth** (remote viewers).

- **Local (same house):** this is almost always a **quality cap** in the app. Set
  **Settings → Video Quality / "Internet Streaming"** (and the app's per-session **Quality**) to
  **Original / Maximum**. On a wired/strong-WiFi LAN there's no reason to cap it.
- **Remote (over the internet):** limited by your **upload bandwidth**. A 40–80 GB 4K remux *will*
  transcode for a remote viewer no matter what — there isn't enough bandwidth to direct-play it. Either
  accept the transcode, lower the remote quality to a sane bitrate, or keep a smaller (1080p) copy for
  remote use. *(This is the one area the server library can genuinely help — see the note below.)*

---

## 3. Subtitles (≈14%)

**Why it happens:** **image-based** subtitles (PGS / VOBSUB, common on Blu-ray rips) can't be overlaid
by many clients, so Plex **burns them in**, which forces a full video transcode.

- **Prefer text subtitles (SRT)** — they overlay without transcoding. Plex can often download an SRT
  match: in playback **Subtitles → search**, pick a text (SRT) version.
- Turn off "burn subtitles" where the client offers the option, or just disable subtitles you don't need.

---

## 4. Per-client step-by-step

> Menu wording shifts between Plex app versions; the **path and the setting** are what matter.
> Two settings fix almost everything: **streaming quality = Original/Maximum** (stops bitrate
> transcodes) and **picking the standard audio track** (stops audio transcodes).

### Samsung / LG TV (Tizen / webOS) — the biggest source of transcoding
1. **Quality:** Plex app → **Settings (gear) → Video / Quality** → set **Home/Local** *and*
   **Remote/Internet** streaming to **Maximum / Original**. (These TVs are often shipped capped, which
   is why good 1080p was downscaling to SD.)
2. **Audio (the important one):** start any movie → bring up the player bar → **Audio** icon →
   choose the **"AC3 / Dolby Digital / 5.1"** (or Stereo) track, **not** the TrueHD/DTS/Atmos one.
   Plex **remembers this per title and per user** — do it once on a show that defaults wrong.
   *(Samsung/LG can't pass through DTS or TrueHD, so Plex transcodes them unless you pick the AC3 track.)*

### PlayStation (PS4 / PS5)
1. **Quality:** Plex app → **Settings → Video → Remote Quality** and **Local Quality** → **Original / Maximum**.
2. **Audio:** **Settings → Audio** — match your TV/receiver setup, or pick the compatible track during
   playback. (PS5 handles more codecs than a Samsung TV, so audio is less of an issue here; quality is.)

### Windows
- **Best fix: install the Plex desktop app** (Plex / Plex HTPC for Windows) instead of watching at
  **app.plex.tv in a browser** — the browser transcodes all audio (see below).
  - **Quality:** **Settings → Quality → Original / Maximum** (local and remote).
  - **Audio:** **Settings → Audio** → enable **passthrough** if you have a receiver/soundbar; otherwise
    select the AC3/stereo track. The desktop app *can* play DTS/TrueHD if your hardware does.

### Chrome / Edge / any web browser
- **No browser setting stops audio transcoding** — the web player re-encodes all multichannel/AC3/EAC3
  audio to Opus. **Use a native app** (desktop, TV, mobile, Apple TV, Shield, Roku). If you truly can't,
  at least set **Settings → Quality → Maximum** to avoid the bitrate transcode.

### Android (phone / tablet / Android TV)
1. **Quality:** Plex app → **Settings → Video → Remote/Local Quality → Original / Maximum**.
2. **Audio:** **Settings → Audio → Audio Passthrough** (set to your device's capability) and
   **Max audio channels** — on a phone, set **Stereo (2.0)** so it never grabs the 7.1 track; on Android
   TV through a receiver, enable passthrough instead.

---

## 5. Quick reference: what each cause looks like in Tautulli

Open **Tautulli → History → a play → "Stream Data"** (or the play details):

| You see | Cause | Fix |
|---|---|---|
| `Audio: Transcode (truehd/dts → ac3/aac/opus)` | device can't play that audio track | pick the standard audio track / use the app not a browser |
| `Video: Transcode` + same codec, lower bitrate | bitrate cap or bandwidth | raise the quality setting (local) / accept or lower (remote) |
| `Video: Transcode (hevc → h264)` | device can't decode the codec | use a newer client; rarely worth changing the file |
| `Subtitle: Burn` | image subtitle burned in | switch to an SRT text subtitle |
| `Container: Transcode` only | container remux (cheap) | usually fine — minimal CPU |

---

## What this means for the library (the part the server *can* help with)

Most transcoding above is **player/device settings** — the library can't change it. The **one**
place the library helps is **remote-viewer bitrate**: a giant remux always transcodes over the
internet, so keeping an appropriately-sized copy for content watched remotely avoids the transcode
*and* saves disk space. Everything else on this list is a five-minute settings change on the player.

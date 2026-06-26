# Device → codec direct-play matrix (transcode-avoidance tiers)

Goal: for a **wide prod audience**, grab the codec/bit-depth/HDR a target tier of players can
**direct-play**, so the server never transcodes. Video codec + bit-depth + HDR drives the video
transcode; container/audio are cheaper. Tiers are defined on **video codec / bit-depth / HDR**.

**Tier key** — **T0** = H.264/SDR only (grab x264 8-bit SDR) · **T1** = HEVC 10-bit + HDR10 (no
reliable HW AV1) · **T2** = AV1 hardware decode. Borderline/model-dependent cases resolve **down**
to the safer tier.

> Built from a multi-agent research sweep (Samsung · LG · Roku · Fire TV · Chromecast/Google TV/Shield ·
> Apple · browsers+consoles · Vizio/Hisense/TCL/Sony). **Re-verify the AV1/DV rows before each prod
> rollout** — codec support moves yearly. Sources at the bottom.

## 1. Master matrix (grouped by tier)

### T0 — H.264 / SDR only (grab x264 8-bit SDR)

| Device | Years | HEVC10 | HDR10 | HDR10+ | DolbyVision | AV1 | MaxRes | Notes |
|---|---|---|---|---|---|---|---|---|
| Samsung pre-2015 (Orsay/legacy, non-Tizen) | ≤2014 | No | No | No | No | No | 2160p | Not Tizen; Plex Tizen app unavailable. H.264/SDR. |
| LG webOS 1.x/2.x | 2014-2015 | model-dep | model-dep | No | No | No | 2160p/1080p | Weak/absent HEVC-HDR. No DV. SDR grab. |
| Roku non-4K players (Express HD/SE/1080p, Stick non-4K incl. 2025 Lakeport 3840X, orig 1080p Premiere, Roku 2/3) | 2015-2025 | No | No | No | No | No | 1080p | **NO HEVC decoder at all.** HEVC = audio-only/fail. H.264+VP9 only. |
| Roku non-4K TVs (TCL/Hisense/Sharp/Onn HD) | 2014-now | No | No | No | No | No | 1080p | Same SoC gating: no HEVC, no HDR. |
| Fire TV gen1 box | 2014 | No | No | No | No | No | 1080p | H.264/SDR only. |
| Fire TV box gen2 | 2015 | model-dep | No | No | No | No | 2160p | 4K HEVC decode but NO HDR pipeline → SDR grab. |
| Fire TV Stick gen1 | 2014 | No | No | No | No | No | 1080p | H.264/SDR. |
| Fire TV Stick gen2 | 2016-2017 | model-dep | No | No | No | No | 1080p | Flaky 8-bit HEVC, no HDR/4K. SDR grab. |
| Chromecast 1st/2nd/3rd gen | 2013-2018 | No | No | No | No | No | 1080p | H.264+VP8 only. Gen3 adds 1080p60. No HEVC/HDR/VP9. |
| Apple TV HD (4th gen, A8) | 2015 | No | No | No | No | No | 1080p | No HW HEVC, SDR only. H.264 SDR. |
| iPhone/iPad A8/A9 (iPhone 6/6s) | 2014-2015 | No | No | No | No | No | 1080p | No HW HEVC, SDR. |
| iPhone/iPad A9X/A10/A10X (iPhone 7, iPad Pro '16-17) | 2016-2017 | model-dep | No | No | No | No | 2160p | HW HEVC but SDR screens → SDR tier. |
| All web browsers — Chrome/Edge/Firefox/Safari | — | model-dep | No | No | No | sw | 2160p/1080p | **Plex web tone-maps HDR→SDR; no DV.** Even Safari → x264 SDR grab. |
| PlayStation 4 (base/Slim) | 2013+ | No | No | No | No | No | 1080p | Weak HEVC, no 4K/HDR. Only PS4 **Pro** is T1. |
| Vizio D-Series / non-4K / 1080p | 2018-2024 | model-dep | model-dep | No | model-dep | No | 1080p | No HDR pipeline on entry sets. SDR grab. |
| TCL/Hisense Roku TV non-4K (3-Series class) | 2018-2024 | model-dep | No | No | No | No | 1080p | No HDR, unreliable HEVC. SDR grab. |

### T1 — HEVC 10-bit + HDR10 (grab HEVC Main10 + HDR10/DV; **never AV1**)

| Device | Years | HEVC10 | HDR10 | HDR10+ | DolbyVision | AV1 | MaxRes | Notes |
|---|---|---|---|---|---|---|---|---|
| Samsung 2015 Tizen SUHD/UHD (JS/JU) | 2015 | Yes | model-dep | No | **No** | No | 2160p | First Tizen; HEVC10. **No DV ever.** |
| Samsung 2016 SUHD/UHD (KS/KU) | 2016 | Yes | Yes | model-dep | **No** | No | 2160p | First Plex-supported year. **No DV.** |
| Samsung 2017-2019 QLED/Premium UHD (Q9F/Q90R, MU/NU/RU) | 2017-2019 | Yes | Yes | Yes | **No** | No | 2160p | Canonical Samsung T1. HDR10+ yes, **DV never.** |
| Samsung 2020 4K QLED/Crystal (Q60T-Q90T, TU) | 2020 | Yes | Yes | Yes | **No** | model-dep | 2160p | 4K AV1 unreliable → stay T1. **No DV.** |
| Samsung 2021 4K entry AU/Crystal | 2021 | Yes | Yes | Yes | **No** | model-dep | 2160p | AV1 unconfirmed on entry → T1. **No DV.** |
| LG 2016 OLED (B6/C6/E6/G6) + Super UHD | 2016 | Yes | Yes | **No** | **Yes** (OLED/SUHD) | No | 2160p | LG = DV brand. **No HDR10+ ever.** |
| LG 2017 OLED (B7-G7/W7) + Super UHD/UJ | 2017 | Yes | Yes | **No** | Yes (OLED/SUHD) | No | 2160p | HEVC10 4K@60. No AV1. |
| LG 2018 OLED (B8-W8) + SK/UK | 2018 | Yes | Yes | **No** | Yes (OLED/SK; UK6x HDR10-only) | No | 2160p | webOS 4.0, no AV1. |
| LG 2019 OLED (B9-W9/Z9 8K) + NanoCell/UM | 2019 | Yes | Yes | **No** | Yes (OLED/NanoCell) | No | 2160p/4320p | **Last pre-AV1 LG gen.** |
| Roku 4 (4400X) | 2015 | Yes | No | No | No | No | 2160p | First 4K Roku, predates HDR → HEVC SDR. |
| Roku Premiere 4K (4620/4630), Premiere '18 (3920/3921) | 2016-2018 | Yes | Yes | No | No | No | 2160p | HDR10, no DV/AV1. |
| Roku Streaming Stick+ (3810X) | 2017-2019 | Yes | Yes | No | No | No | 2160p | HDR10, no DV/AV1. |
| Roku Ultra 2017/2018 (4660/4661) | 2017-2018 | Yes | Yes | No | No | No | 2160p | HDR10, **no DV** (DV from 2019). |
| Roku Ultra 2019 (4670/4662) | 2019 | Yes | Yes | No | **Yes** | No | 2160p | First DV Roku. No AV1. |
| 4K Roku TVs — pre-AV1 (TCL/Hisense/Onn) | 2016-2020 | Yes | Yes | model-dep | model-dep | No | 2160p | DV/HDR10+ panel-dependent. No AV1. |
| Fire TV pendant 3rd gen | 2017 | Yes | Yes | No | No | No | 2160p | HDR10/HLG, no DV/AV1. |
| Fire TV Stick Lite / 3rd gen / HD | 2020-2024 | Yes | Yes | Yes | No | No | **1080p** | HEVC10 + HDR10/HDR10+ @1080p60. No 4K/DV/AV1. |
| Fire TV Stick 4K (1st gen) | 2018 | Yes | Yes | Yes | **Yes** | No | 2160p | Full HDR incl. DV. No AV1. |
| Fire TV Cube 1st gen | 2018 | Yes | Yes | **No** | **No** | No | 2160p | **HDR10-ONLY landmine.** |
| Fire TV Cube 2nd gen | 2019 | Yes | Yes | Yes | Yes | No | 2160p | Full DV/HDR10+, no AV1. |
| Fire TV 4-Series (built-in) | 2021+ | Yes | model-dep | model-dep | **No** | model-dep | 2160p | No DV. AV1 panel-dep → default T1. |
| Fire TV Omni LED | 2021+ | Yes | Yes | model-dep | model-dep (DV only 65"/75") | model-dep | 2160p | DV by size; default T1. |
| Chromecast Ultra | 2016 | Yes | Yes | No | **Yes** | No | 2160p | Cheapest 4K DV Cast. No HDR10+/AV1. |
| Chromecast w/ Google TV **4K** (S905X3/D3) | 2020 | Yes | Yes | Yes | Yes | **No** | 2160p | **AV1 inversion: the 4K model has NO AV1.** |
| NVIDIA Shield TV 2015/2017 (Tegra X1) | 2015-2017 | Yes | Yes | No | No | No | 2160p | HEVC10/HDR10. No DV/AV1. |
| NVIDIA Shield TV / Pro 2019 (Tegra X1+) | 2019 | Yes | Yes | No | **Yes** | **sw** | 2160p | AV1 **software-only** → treat as NO AV1, T1. |
| Apple TV 4K gen1 (A10X) | 2017 | Yes | Yes | No | Yes | **No** | 2160p | HEVC10 + HDR10/DV. No AV1. |
| Apple TV 4K gen2 (A12) | 2021 | Yes | Yes | No | Yes | **No** | 2160p | Adds 4K60 HDR. No AV1. |
| Apple TV 4K gen3 (A15) | 2022 | Yes | Yes | **Yes** | Yes | **No** | 2160p | Only ATV with HDR10+. **Still no AV1.** |
| iPhone A11-A16 (iPhone 8/X → 14/15 non-Pro) | 2017-2023 | Yes | Yes | No | Yes | **No** | 2160p | HEVC10 + HDR10/DV. No AV1. |
| iPad non-Pro + iPad Pro M1/M2 | 2018-2024 | Yes | Yes | No | Yes | **No** | 2160p | **M1/M2 = no AV1** (arrives M4). |
| Xbox Series X / Series S | 2020+ | Yes | Yes | No | model-dep (S converts DV→HDR10) | **No** | 2160p | **No AV1.** 4K must be HEVC (no 4K H.264). |
| Xbox One X / One S | 2016-2017 | Yes | Yes | No | model-dep | **No** | 2160p | 4K HEVC HDR10. No AV1. |
| PlayStation 5 | 2020+ | Yes | Yes | No | **No** | **No** | 2160p | HDR10 only, **never DV**, no AV1. |
| PlayStation 4 Pro | 2016+ | Yes | Yes | No | **No** | **No** | 2160p | 4K HEVC/HDR10. No DV/AV1. |
| Sony Bravia X80x entry (X80J/K/L, BRAVIA 3) | 2021-2024 | Yes | Yes | **No** | Yes | model-dep | 2160p | Below AV1 cutoff. **No HDR10+.** |
| Vizio SmartCast 4K (V/M/P/Quantum/OLED) | 2020-2024 | Yes | Yes | **Yes** | Yes | **No** | 2160p | Full DV+HDR10+ but **NEVER AV1.** Hard T1 ceiling. |
| TCL Roku TV 4K (4/5/6-Series, R655) | 2020-2024 | Yes | Yes | model-dep | Yes | **No** | 2160p | **Roku-TV AV1 landmine: transcodes AV1→480p.** |
| Hisense Roku TV 4K (US budget R6/R7) | 2021-2024 | Yes | Yes | model-dep | model-dep | **No** | 2160p | Same no-AV1 landmine as TCL Roku TVs. |
| Hisense U6H/U7H/U8H (2022 Google TV) + VIDAA intl | 2022-2024 | Yes | Yes | Yes | Yes | model-dep | 2160p | 2022 SoC AV1 unconfirmed → default T1. |

### T2 — AV1 hardware decode (grab AV1 for SDR/HDR10; keep HEVC for DV)

| Device | Years | HEVC10 | HDR10 | HDR10+ | DolbyVision | AV1 | MaxRes | Notes |
|---|---|---|---|---|---|---|---|---|
| Samsung 2020 8K QLED (Q800T/Q900T/Q950TS) | 2020 | Yes | Yes | Yes | **No** | **HW** | 2160p (8K) | First AV1 Samsung. **No DV ever.** |
| Samsung 2021 4K Neo QLED/QLED (QN90A/85A/Q80A) | 2021 | Yes | Yes | Yes | **No** | HW | 2160p | Mainstream AV1 from 2021. **No DV.** |
| Samsung 2021 8K Neo QLED (QN900A/800A/700A) | 2021 | Yes | Yes | Yes | **No** | HW | 2160p (8K) | AV1 to 8K. **No DV.** |
| Samsung 2022+ 4K QLED/Neo/Crystal/QD-OLED (QN90B/S95B+) | 2022-2025 | Yes | Yes | Yes | **No** | HW | 2160p | AV1 lineup-wide. **No DV even on OLED. DTS unsupported → AC3/EAC3/AAC.** |
| Samsung 2022+ 8K Neo QLED (QN900B/800B+) | 2022-2025 | Yes | Yes | Yes | **No** | HW | 2160p (8K) | AV1+HEVC to 8K. **No DV. No DTS.** |
| LG 2020 OLED (BX/CX/GX) + ZX/NanoCell/UN | 2020 | Yes | Yes | **No** | Yes (OLED/Nano) | **HW** (4K@60) | 2160p/4320p | webOS 5.0 — first LG AV1. 4K120 → HEVC. No HDR10+. |
| LG 2021 OLED (A1-G1/Z1) + QNED/UP | 2021 | Yes | Yes | **No** | Yes (OLED/QNED) | HW | 2160p/4320p | AV1 4K@60. No HDR10+. |
| LG 2022 OLED (A2-G2/Z2) + QNED | 2022 | Yes | Yes | **No** | Yes (OLED/QNED) | HW | 2160p | **UQ70-90 LCD = HDR10/HLG only, NO DV.** |
| LG 2023 OLED (B3/C3/G3) + QNED | 2023 | Yes | Yes | **No** | Yes (OLED/QNED; entry UR HDR10-only) | HW | 2160p | AV1 4K@60. No HDR10+. |
| LG 2024 OLED (B4/C4/G4) + QNED | 2024 | Yes | Yes | **No** | Yes (OLED/QNED; UT/UR HDR10-only) | HW | 2160p | No HDR10+. |
| LG 2025 OLED (B5/C5/G5) + QNED evo | 2025 | Yes | Yes | **No** | Yes (OLED/QNED; UA77/73 HDR10-only) | HW | 2160p | Still no HDR10+. |
| Roku Ultra 2020/2021/2024 (4800/4801/4850) | 2020-2024 | Yes | Yes | Yes | **Yes** | **HW** | 2160p | First AV1 Roku line. Full stack incl. DV. |
| Roku Express 4K / 4K+ (3940/3941) | 2021 | Yes | Yes | Yes | **No** | HW | 2160p | AV1 yes, **no DV** (Express never DV). |
| Roku Streaming Stick 4K / 4K+ (3820/3821) | 2021-2022 | Yes | Yes | Yes | **Yes** | HW | 2160p | Full stack incl. DV + AV1. |
| Roku Streaming Stick **Plus** 2025 (3830) / Streambar SE | 2024-2025 | Yes | Yes | Yes | **No** | HW | 2160p | AV1, no DV. **Naming trap: "Plus"/"4K" ≠ the 1080p T0 "Streaming Stick".** |
| 4K Roku TVs — AV1-era (**only if AV1 confirmed for that model**) | 2021+ | Yes | Yes | model-dep | model-dep | model-dep | 2160p | **If AV1 unconfirmed → default T1.** |
| Fire TV Stick 4K (2nd gen) | 2023 | Yes | Yes | Yes | Yes | **HW** | 2160p | 2023 refresh added AV1. Full DV. |
| Fire TV Stick 4K Max (1st/2nd gen) | 2021/2023 | Yes | Yes | Yes | Yes | HW | 2160p | First AV1 stick. Best for AV1+DV. |
| Fire TV Cube 3rd gen | 2022 | Yes | Yes | Yes | Yes | HW | 2160p | AV1 + full DV/HDR10+. Top Fire TV. |
| Fire TV Omni QLED | 2022+ | Yes | Yes | Yes | Yes | HW | 2160p | DV IQ + HDR10+ Adaptive + AV1. |
| Chromecast w/ Google TV **HD** (S805X2) | 2022 | Yes | Yes | Yes | **No** | **HW** | **1080p** | **AV1 inversion: cheap HD model HAS AV1** (1080p cap). No DV. |
| Google TV Streamer 4K (MT8696) | 2024 | Yes | Yes | Yes | Yes | HW | 2160p | Only Google streamer with AV1@4K60 + full HDR/DV. |
| iPhone A17 Pro/A18/A19 (15 Pro, 16, 17) | 2023-2025 | Yes | Yes | No | Yes | **HW** | 2160p | First Apple AV1 HW. No HDR10+ path. |
| iPad Pro/Mac M3/M4/M5 | 2023-2026 | Yes | Yes | No | Yes | **HW** | 2160p | AV1 from M3 (Mac)/M4 (iPad). **M1/M2 = NO AV1 (T1).** |
| Sony Bravia X85x+ (X90J/A95K/X90L/A95L, BRAVIA 7/8/9) | 2021-2024+ | Yes | Yes | **No** | Yes | **HW** | 2160p | AV1 app-decode. **No HDR10+ (Sony never).** Keep HEVC for DV. |
| TCL Google TV (Pentonic: QM8/QM850G/QM851G, Q6/Q7, C805/845/855/955) | 2023-2024+ | Yes | Yes | Yes | Yes | HW | 2160p | Full AV1 + all HDR incl. HDR10+/DV. |
| Hisense Google TV US (U6/U7/U8 K-series, N-series, UX) | 2023-2024+ | Yes | Yes | Yes | Yes | HW | 2160p | Full AV1 + all HDR incl. HDR10+/DV. |

## 2. Landmines (read before grabbing)

- **Samsung = ZERO Dolby Vision, every year, every tier** (even 8K flagships and QD-OLED). Grab
  HDR10/HDR10+ or HEVC10; never DV-only (DV Profile 5 → washed-out/transcode). Also **DTS audio
  unsupported on 2022+ Samsung** → prefer AC3/E-AC3/AAC or you get an audio transcode.
- **Non-4K Roku has NO HEVC decoder at all** (players AND TVs, incl. the 2025 1080p "Streaming
  Stick" Lakeport). Sending HEVC = audio-only/failure. Strict H.264/SDR T0.
- **Roku naming trap:** 2025 1080p "Streaming Stick" (T0, no HEVC) vs 4K "Streaming Stick **Plus**"
  (T2). **Plus/4K** is the only differentiator.
- **Roku TVs (TCL/Hisense) do NOT decode AV1** despite Roku branding — AV1 transcodes to 480p. Only
  standalone RTD131x Roku **players** (2020+) do AV1.
- **No Apple TV — including the 2022 A15 gen3 — has AV1.** AV1 always transcodes on Apple TV. Apple
  AV1 HW only on A17 Pro+/M3+ Mac/M4+ iPad. **M1/M2 are NOT AV1.**
- **All web browsers = T0 for the grab.** Plex web tone-maps HDR→SDR, no DV; even Safari → x264 SDR.
- **No game console has AV1** (Xbox Series X|S, Xbox One S/X, PS5, PS4 Pro). **Xbox has no 4K H.264**
  (4K must be HEVC). **PlayStation never does Dolby Vision** (HDR10 only).
- **LG never supports HDR10+** (through 2025) — but LG is the DV brand. LG **entry LCD** (UQ/UR/UT/UA)
  drop DV (HDR10/HLG only).
- **Chromecast-w-GTV AV1 inversion:** the cheap **HD** model HAS AV1 (1080p); the premium **4K** does NOT.
- **Fire TV is per-variant:** AV1 only on Stick 4K Max (both), Stick 4K 2nd gen (2023), Cube 3rd gen,
  Omni QLED. Cube 1st gen is **HDR10-only**; 1080p sticks cap at 1080p.
- **NVIDIA Shield AV1 is software-only** (even 2019 Pro) — treat all Shields as T1.
- **Vizio = never AV1** (through 2024) — full DV+HDR10+ but hard T1. **Sony = never HDR10+**.
- **DV never rides AV1.** Any DV title needs an HEVC (or H.264) grab regardless of tier.

## 3. Coverage of the named families (Samsung, Roku 4K + non-4K, Fire TV Sticks)

**Yes — all fully covered, no gaps:**
- **Samsung TVs:** pre-2015 → T0; 2015-2021 4K (HEVC10+HDR10/10+, **no DV**) → T1; 2020-8K, 2021 Neo,
  all 2022+ → T2 (AV1, **still no DV**, DTS→AC3 on 2022+).
- **Roku TVs (4K + non-4K):** **non-4K → T0** (no HEVC); **4K → T1** by default (HEVC+HDR10, DV
  panel-dependent). Roku TVs **never get T2** (they transcode AV1) unless a model's AV1 is confirmed.
- **Fire TV Sticks (all variants):** gen1/gen2 → T0; Stick Lite/3rd-gen/HD (1080p) + Stick 4K 1st gen
  (full DV) → T1; Stick 4K 2nd gen, Stick 4K Max 1st/2nd → T2 (AV1 + DV).

## 4. Grab recommendation per audience tier

- **T0** → H.264/AVC 8-bit **SDR** (≤1080p) — the universal fallback that direct-plays everywhere
  (browsers, non-4K Roku/sticks, base PS4, old Apple).
- **T1** → **HEVC Main10 / 10-bit / HDR10** as the safe HDR floor; add **Dolby Vision (P5/P8.1 w/
  HDR10 base)** for LG/Apple/Roku-Ultra/Fire-4K/Sony/Vizio/TCL-Hisense, but switch the *same title* to
  plain HDR10 for **Samsung/PlayStation/Express-4K/HD-Chromecast/Roku-TV** (no DV). Never AV1. Audio
  AC3/E-AC3/AAC for Samsung/PS compatibility.
- **T2** → **AV1 (SDR or HDR10)** as the bandwidth-optimal grab for Samsung 2020-8K+/2022+, LG/Sony/
  TCL/Hisense AV1 TVs, RTD131x Roku players, Fire 4K-Max/Cube-3, HD-Chromecast/Google-TV-Streamer,
  A17 Pro+/M3+/M4+ Apple — **but keep the HEVC+DV copy** for any DV title (DV never rides AV1) and for
  the entire Apple-TV/console/Vizio/Roku-TV population with no AV1.

Net: an **HEVC-10/HDR10 (+DV-where-safe)** master direct-plays the broadest slice; add an **H.264 SDR**
fallback for T0, and an optional **AV1** copy where T2 devices dominate and bandwidth matters.

## Sources & method
9-agent research sweep (one per device family), June 2026. Re-verify AV1/DV rows before each rollout.
- [AV1 supported devices (HD Report)](https://hd-report.com/list-of-devices-that-support-av1-video-coding/) · [AV1 in 2026 (Transcodely)](https://www.transcodely.com/blog/av1-in-2026) · [Apple AV1 (Bitmovin)](https://bitmovin.com/blog/apple-av1-support/) · [AV1 HW adoption (ScientiaMobile)](https://scientiamobile.com/av1-codec-hardware-decode-adoption/)
- [Plex supported media formats](https://support.plex.tv/articles/203810286-what-media-formats-are-supported/) · [State of AV1 playback (Bitmovin)](https://bitmovin.com/blog/av1-playback-support/) · AFTVnews (Fire TV specs), manufacturer spec sheets, AVSForum threads (per-family).

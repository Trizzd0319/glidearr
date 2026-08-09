# Cormorant Garamond — vendored for poster rasterisation

The 118px poster titles in `assets/playlists/*.svg` declare:

    font-family="'Cormorant Garamond', Garamond, Georgia, serif"

On a headless box none of the first three exist, and fontconfig falls through to
**DejaVu Sans** — not even a serif. The titles then render in the wrong
classification and, because DejaVu Sans is much wider, `You Watched` measures
756px against a 664px measure and is clipped by the canvas edge. With Cormorant
present it measures 605px and fits.

Only the **roman** is strictly required (the design uses Cormorant at weight 300
for titles only; the italic why line is Lora). The italic is included so the
family is complete if copy ever needs it.

## Licence

SIL Open Font License 1.1 — see `OFL.txt`, copied verbatim from
`google/fonts/ofl/cormorantgaramond/OFL.txt`.

    Copyright 2015 the Cormorant Project Authors
    (github.com/CatharsisFonts/Cormorant)

Bundling and redistribution inside a larger work is explicitly permitted,
including commercially. Three conditions apply to us:

1. Ship this `OFL.txt` and the copyright notice with the fonts. That is what
   this directory is for — do not separate them.
2. Do not sell the fonts on their own. Shipping them inside glidearr is fine.
3. The fonts stay under OFL; that does not affect glidearr's own licence, and
   it does not affect the posters they render. Per the OFL: *"The requirement
   for fonts to remain under this license does not apply to any document
   created using the Font Software."*

There is **no Reserved Font Name** on the copyright line, so even a modified
derivative could keep the name. We ship the files unmodified regardless.

## Docker

    COPY scripts/support/assets/fonts/cormorant-garamond/*.ttf \
         /usr/share/fonts/truetype/cormorant/
    RUN fc-cache -f

Lora is also required and is OFL too; if the base image does not carry it,
vendor it the same way from `google/fonts/ofl/lora`.

## Verify, don't assume

A missing font does not fail — it silently substitutes. Assert instead:

    fc-match -f '%{family}' 'Cormorant Garamond'   # must not return DejaVu
